"""小区与区域资料库：BGE 向量 + Chroma 索引 + BM25/RRF 融合 + MMR 选片。

SQLite 仍是文档与切片的权威存储（正文、主题、页码、来源、digest）；Chroma 只负责
稠密索引，BM25 负责词法召回。召回流程：双路各取 TopK → RRF 融合 → MMR 在候选池里
选出互不重复的 TopN。区域资料在同一次查询里降权，避免"区域事实"被当成"小区事实"。
"""
from __future__ import annotations
import hashlib,json,sqlite3,threading,uuid
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from .embeddings import HashingEmbedder
from .parsing import read_pages,split_text
from .retrieval import BM25Index,rrf_fuse
from .search_planning import normal,amenity_terms,AMENITIES
from .vector_store import ChromaVectorStore

FIELDS=('chunk_id','title','section','community_key','community_name','region','source','as_of','is_demo','page','ordinal','text','path')
REGION_WEIGHT=0.5

class CommunityKnowledge:
    def __init__(self,path,folder,*,embedder=None,chroma_dir=None,dense_top_k=10,lexical_top_k=10,rrf_k=60,top_k=3,
                 mmr_lambda=0.7,max_per_document=2):
        self.path,self.folder=path,folder;folder.mkdir(parents=True,exist_ok=True)
        self.embedder=embedder or HashingEmbedder()
        self.chroma=ChromaVectorStore(chroma_dir or Path(path).parent/'chroma')
        self.dense_top_k=dense_top_k;self.lexical_top_k=lexical_top_k;self.rrf_k=rrf_k;self.top_k=top_k
        self.mmr_lambda=mmr_lambda;self.max_per_document=max_per_document
        self.lock=threading.RLock();self.bm25=BM25Index();self._bm25_dirty=True
        with self.connect() as con:
            con.executescript('''CREATE TABLE IF NOT EXISTS documents(path TEXT PRIMARY KEY,title TEXT,community_key TEXT,region TEXT,source TEXT,as_of TEXT,is_demo INTEGER,digest TEXT,section TEXT);
            CREATE TABLE IF NOT EXISTS chunks(chunk_id TEXT PRIMARY KEY,path TEXT,page INTEGER,text TEXT,vector BLOB,section TEXT,community_name TEXT,ordinal INTEGER);''')
            self._migrate(con)
    @staticmethod
    def _migrate(con):
        def columns(table):
            return {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
        for table,column,ddl in (('documents','section',"ALTER TABLE documents ADD COLUMN section TEXT DEFAULT ''"),
                                 ('chunks','section',"ALTER TABLE chunks ADD COLUMN section TEXT DEFAULT ''"),
                                 ('chunks','community_name',"ALTER TABLE chunks ADD COLUMN community_name TEXT DEFAULT ''"),
                                 ('chunks','ordinal',"ALTER TABLE chunks ADD COLUMN ordinal INTEGER DEFAULT 0")):
            if column not in columns(table):con.execute(ddl)
    def connect(self):
        @contextmanager
        def open_db():
            con=sqlite3.connect(self.path,timeout=5);con.row_factory=sqlite3.Row
            try:
                with con:yield con
            finally:con.close()
        return open_db()
    def metadata(self):
        path=self.folder/'metadata.json'
        return json.loads(path.read_text('utf-8-sig')) if path.exists() else {}
    # ---------------------------------------------------------------- indexing
    def sync(self):
        with self.lock:
            metadata=self.metadata();counts={'indexed':0,'skipped':0,'unassigned':0,'failed':[]}
            pending=[]
            for path in sorted(self.folder.iterdir()):
                if not path.is_file() or path.suffix.lower() not in {'.pdf','.txt','.md'}:continue
                meta=metadata.get(path.name,{})
                if not meta.get('community_key'):counts['unassigned']+=1;continue
                digest=hashlib.sha256(path.read_bytes()+json.dumps(meta,sort_keys=True).encode()).hexdigest()
                with self.connect() as con:old=con.execute('SELECT digest FROM documents WHERE path=?',(str(path),)).fetchone()
                if old and old[0]==digest:counts['skipped']+=1;continue
                try:
                    chunks=[(page,c) for page,text in read_pages(path) for c in split_text(text)]
                    if not chunks:raise ValueError('无可提取文字，扫描PDF暂不支持OCR')
                    pending.append((path,meta,digest,chunks))
                except Exception as e:counts['failed'].append({'file':path.name,'error':str(e)[:200]})
            # One embed call for the whole batch: per-file embedding dominated
            # start-up time once the corpus grew past a hundred documents.
            if pending:
                vectors=self.embedder.embed([text for _,_,_,chunks in pending for _,text in chunks])
                offset=0;all_ids=[];all_vectors=[];all_meta=[];all_text=[]
                with self.connect() as con:
                    for path,meta,digest,chunks in pending:
                        count=len(chunks);batch=vectors[offset:offset+count];offset+=count
                        ids=[hashlib.sha256((str(path)+str(index)).encode()).hexdigest()[:24] for index in range(count)]
                        section=meta.get('section','资料');community_name=meta.get('community_name') or meta.get('title',path.stem)
                        con.execute('DELETE FROM chunks WHERE path=?',(str(path),))
                        con.execute('INSERT OR REPLACE INTO documents(path,title,community_key,region,source,as_of,is_demo,digest,section) VALUES (?,?,?,?,?,?,?,?,?)',
                            (str(path),meta.get('title',path.stem),meta['community_key'],meta.get('region',''),meta.get('source','来源未标注'),meta.get('as_of','日期未标注'),int(meta.get('is_demo',False)),digest,section))
                        for index,(page,text) in enumerate(chunks):
                            con.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)',(ids[index],str(path),page,text,batch[index].tobytes(),section,community_name,index))
                        all_ids.extend(ids);all_vectors.append(batch);all_meta.append((path,meta,section,community_name,chunks));all_text.extend(text for _,text in chunks)
                        counts['indexed']+=1
                self._index_dense_batch(all_ids,all_vectors,all_meta,all_text)
            if counts['indexed']:self._bm25_dirty=True
            self._ensure_bm25()
            return counts
    def _index_dense(self,ids,vectors,chunks,path,meta,section,community_name):
        if not self.chroma.available:return
        self.chroma.delete_paths([path])
        metadatas=[{'path':path,'page':page,'community_key':meta['community_key'],'region':meta.get('region',''),
            'title':meta.get('title',Path(path).stem),'section':section,'community_name':community_name,
            'as_of':meta.get('as_of','日期未标注'),'is_demo':int(meta.get('is_demo',False))} for page,_ in chunks]
        if self.chroma.upsert(ids,vectors,metadatas,[text for _,text in chunks]):return
        self.chroma.reset();self.chroma.upsert(ids,vectors,metadatas,[text for _,text in chunks])
    def _index_dense_batch(self,ids,vectors,entries,texts):
        """Index every changed document with one delete and one upsert."""
        if not self.chroma.available or not ids:return
        self.chroma.delete_paths([str(path) for path,_,_,_,_ in entries])
        metadatas=[]
        for path,meta,section,community_name,chunks in entries:
            metadatas.extend({'path':path,'page':page,'community_key':meta['community_key'],'region':meta.get('region',''),
                'title':meta.get('title',Path(path).stem),'section':section,'community_name':community_name,
                'as_of':meta.get('as_of','日期未标注'),'is_demo':int(meta.get('is_demo',False))} for page,_ in chunks)
        if self.chroma.upsert(ids,np.vstack(vectors),metadatas,texts):return
        self.chroma.reset();self.chroma.upsert(ids,np.vstack(vectors),metadatas,texts)
    def _ensure_bm25(self):
        if not self._bm25_dirty:return self.bm25
        with self.connect() as con:rows=[(row[0],row[1]) for row in con.execute('SELECT chunk_id,text FROM chunks')]
        self.bm25.build(rows);self._bm25_dirty=False
        return self.bm25
    def close(self):
        self.chroma.close()
    # ------------------------------------------------------------------ reading
    def documents(self):
        with self.connect() as con:return [dict(r) for r in con.execute('SELECT * FROM documents ORDER BY community_key,title')]
    def community_catalog(self,properties,include_demo=True):
        known={p['community_key']:dict(p) for p in properties}
        for d in self.documents():
            key=d['community_key']
            if key.startswith('region:') or key in known or d['is_demo'] and not include_demo:continue
            name=d['title'] or key
            for suffix in (' · 对话资料',' · 概况',' · 交通',' · 教育',' · 医疗',' · 商业',' · 居住体验','小区概况','小区资料','资料'):
                if name.endswith(suffix):name=name[:-len(suffix)];break
            known[key]={'community_key':key,'community_name':name or key,'region':d['region'],'is_demo':bool(d['is_demo'])}
        return list(known.values())
    def _rows(self,keys,include_demo):
        placeholders=','.join('?' for _ in keys)
        sql='SELECT d.*,c.chunk_id,c.page,c.text,c.vector,c.section,c.community_name,c.ordinal FROM documents d JOIN chunks c ON c.path=d.path WHERE d.community_key IN ('+placeholders+')'
        if not include_demo:sql+=' AND d.is_demo=0'
        with self.connect() as con:return [dict(r) for r in con.execute(sql,tuple(keys))]
    def _dense_scores(self,rows,query_vector,limit,where=None):
        if not rows:return {}
        if self.chroma.available and where is not None:
            hits=dict(self.chroma.query(query_vector,limit,where=where))
            allowed={row['chunk_id'] for row in rows}
            filtered={cid:score for cid,score in hits.items() if cid in allowed}
            if filtered:return filtered
        matrix=np.vstack([np.frombuffer(row['vector'],dtype=np.float32) for row in rows])
        scores=matrix@query_vector
        order=np.argsort(-scores)[:limit]
        return {rows[index]['chunk_id']:float(scores[index]) for index in order}
    def _mmr_select(self,rows,fused,limit):
        """Select a relevant but non-redundant set from the fused candidates."""
        by_id={row['chunk_id']:row for row in rows}
        pool=[(chunk_id,score) for chunk_id,score in fused if chunk_id in by_id][:max(self.dense_top_k,limit)]
        if not pool:return []
        top=pool[0][1] or 1.0;weights=dict(pool)
        vectors={chunk_id:np.frombuffer(by_id[chunk_id]['vector'],dtype=np.float32) for chunk_id,_ in pool}
        chosen=[];remaining=[chunk_id for chunk_id,_ in pool];per_path={}
        while remaining and len(chosen)<limit:
            def value(chunk_id):
                relevance=weights[chunk_id]/top
                redundancy=max((float(vectors[chunk_id]@vectors[other]) for other in chosen),default=0.0)
                return self.mmr_lambda*relevance-(1-self.mmr_lambda)*redundancy
            best=max(remaining,key=value)
            path=by_id[best]['path']
            if per_path.get(path,0)>=self.max_per_document:remaining.remove(best);continue
            chosen.append(best);remaining.remove(best);per_path[path]=per_path.get(path,0)+1
        return [(chunk_id,weights[chunk_id]) for chunk_id in chosen]
    def search(self,community_key,region,query,include_demo=True,limit=None):
        limit=limit or self.top_k;keys=[community_key,'region:'+region]
        rows=self._rows(keys,include_demo)
        if not rows:return []
        self._ensure_bm25();by_id={row['chunk_id']:row for row in rows}
        query_vector=self.embedder.embed_query(query)
        dense=self._dense_scores(rows,query_vector,self.dense_top_k,{'community_key':{'$in':keys}})
        lexical=dict(self.bm25.search(query,limit=self.lexical_top_k,allowed=set(by_id)))
        fused=rrf_fuse([sorted(dense.items(),key=lambda item:(-item[1],item[0])),sorted(lexical.items(),key=lambda item:(-item[1],item[0]))],k=self.rrf_k)
        # Region-level material is shared context, so it never outranks the
        # community's own documents.
        fused=[(chunk_id,score*(REGION_WEIGHT if str(by_id.get(chunk_id,{}).get('community_key','')).startswith('region:') else 1.0))
               for chunk_id,score in fused]
        fused.sort(key=lambda item:(-item[1],item[0]))
        return [{key:row[key] for key in FIELDS}|{'score':round(score,6)} for chunk_id,score in self._mmr_select(rows,fused,limit) for row in [by_id[chunk_id]]]
    def discover(self,query,catalog,region=None,limit=6,required_exact=None):
        """Global community retrieval before any listing selection; metadata scopes precede ranking."""
        groups,inferred_exact=amenity_terms(query);exact=list(dict.fromkeys(inferred_exact if required_exact is None else required_exact))
        query_vector=self.embedder.embed_query(query);self._ensure_bm25()
        output=[]
        for community in catalog:
            if region and community['region']!=region:continue
            rows=self._rows([community['community_key']],True)
            if not rows:continue
            text='\n'.join(row['text'] for row in rows);normalized=normal(community['community_name']+' '+text)
            if any(normal(term) not in normalized for term in exact):continue
            covered=[name for name in groups if any(normal(word) in normalized for word in AMENITIES[name])]
            if groups and not covered:continue
            by_id={row['chunk_id']:row for row in rows}
            dense=self._dense_scores(rows,query_vector,self.dense_top_k,{'community_key':community['community_key']})
            similarity=max(dense.values()) if dense else 0.0
            if not groups and not exact and similarity<.1 and normal(community['community_name']) not in normal(query):continue
            ranked=[chunk_id for chunk_id,_ in sorted(dense.items(),key=lambda item:(-item[1],item[0]))][:3] or list(by_id)[:3]
            documents=[{key:by_id[chunk_id][key] for key in FIELDS} for chunk_id in ranked if chunk_id in by_id]
            score=4*(len(covered)/max(1,len(groups)))+similarity
            output.append({**community,'score':round(score,4),'covered_topics':covered,'unverified_topics':[name for name in groups if name not in covered],'exact_terms':exact,'documents':documents})
        return sorted(output,key=lambda row:(-row['score'],row['community_key']))[:limit]
    # ------------------------------------------------------------------ imports
    def import_document(self,path,community_key,region,source,as_of):
        with self.lock:
            path=Path(path)
            if path.suffix.lower() not in {'.pdf','.txt','.md'}:raise ValueError('仅支持PDF、TXT、Markdown资料。')
            import shutil
            name=path.stem+'-'+uuid.uuid4().hex[:6]+path.suffix.lower();shutil.copy2(path,self.folder/name)
            meta=self.metadata();meta[name]=dict(title=path.stem,section='导入资料',community_key=community_key,region=region,source=source or '来源未标注',as_of=as_of or '日期未标注',is_demo=False)
            temp=self.folder/'metadata.json.tmp';temp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),'utf-8');temp.replace(self.folder/'metadata.json')
        return self.sync()
    def import_text(self,entry_id,*,title,community_key,region,source,as_of,text):
        with self.lock:
            if not re_full_id(entry_id):raise ValueError('资料请求标识无效。')
            name='chat-'+entry_id+'.txt';path=self.folder/name
            temp=self.folder/(name+'.'+uuid.uuid4().hex+'.tmp');temp.write_text(text,encoding='utf-8');temp.replace(path)
            meta=self.metadata();meta[name]=dict(title=title,section='导入资料',community_key=community_key,region=region,source=source,as_of=as_of,is_demo=False)
            tmp_meta=self.folder/('metadata-'+uuid.uuid4().hex+'.tmp');tmp_meta.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8');tmp_meta.replace(self.folder/'metadata.json')
        result=self.sync()
        if any(item['file']==name for item in result['failed']):raise ValueError('资料已保留，但索引尚未完成，请重试。')
        return {'path':str(path),'indexed':True}

def re_full_id(value):
    import re
    return bool(re.fullmatch('[a-f0-9]{32}',value))
