from __future__ import annotations
import hashlib,json,sqlite3,threading,time,uuid
from contextlib import closing
import numpy as np
from pydantic import ValidationError
from agent_memory import MemoryStore,MemoryAdmin
from .domain import Preferences
from .typed_memory import CustomerLongMemory
from .embeddings import HashingEmbedder, load_embedder

# Older builds used the hashing features for both RAG and memory; the name is
# kept so existing imports keep working while the default becomes BGE.
LocalEmbedder = HashingEmbedder

class CustomerMemory:
    """Existing MemoryStore/MMR plus explicit, versioned customer preference facts."""
    def __init__(self,path,embedder=None):
        self.path=path;self.lock=threading.RLock()
        self.embedder=embedder or HashingEmbedder()
        try:self.store=MemoryStore(path,embedder=self.embedder)
        except ValueError as error:
            raise ValueError('长期记忆库由其他向量模型建立，与当前嵌入后端不一致。请在备份后重建 data/customer_memory.db。') from error
        with closing(sqlite3.connect(path)) as con,con:
            con.execute('''CREATE TABLE IF NOT EXISTS customer_preferences(
                broker_id TEXT,customer_id TEXT,key TEXT,version INTEGER,value TEXT,source TEXT,
                request_id TEXT,memory_id TEXT,active INTEGER,created REAL,
                PRIMARY KEY(broker_id,customer_id,key,version),UNIQUE(broker_id,customer_id,key,request_id))''')
            con.execute('''CREATE TABLE IF NOT EXISTS memory_changes(
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,key TEXT NOT NULL,action TEXT NOT NULL,old_value TEXT,new_value TEXT,
                source TEXT,request_id TEXT,created REAL NOT NULL)''')
            con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS memory_changes_unique
                ON memory_changes(broker_id,customer_id,request_id,memory_type,key,action)''')
        self.long_memory=CustomerLongMemory(self.store)
        self.admin=MemoryAdmin(self.long_memory.plugin)
        self.on_change=None
    def namespace(self,broker,cid):return 'customer:'+hashlib.sha256(json.dumps([broker,cid]).encode()).hexdigest()
    def versions(self,broker,cid):
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory=sqlite3.Row
            return [dict(r) for r in con.execute('SELECT * FROM customer_preferences WHERE broker_id=? AND customer_id=? ORDER BY created DESC,version DESC',(broker,cid))]
    def profile(self,broker,cid):return {r['key']:json.loads(r['value']) for r in self.versions(broker,cid) if r['active'] and json.loads(r['value']) is not None}
    def active_versions(self,broker,cid):return {r['key']:{'version':r['version'],'created':r['created']} for r in self.versions(broker,cid) if r['active']}
    def apply(self,broker,cid,preferences,source,request_id):
        prefs=Preferences.model_validate(preferences).model_dump(exclude_unset=True)
        with self.lock:
            for key,value in prefs.items():
                with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                    con.execute('BEGIN IMMEDIATE')
                    old=con.execute('SELECT memory_id,version FROM customer_preferences WHERE broker_id=? AND customer_id=? AND key=? AND request_id=?',(broker,cid,key,request_id)).fetchone()
                    if old:memory_id,version=old
                    else:
                        active=con.execute('SELECT memory_id,version,value FROM customer_preferences WHERE broker_id=? AND customer_id=? AND key=? AND active=1',(broker,cid,key)).fetchone()
                        if active and json.loads(active[2])==value:
                            self._log_change(con,broker,cid,'preference',key,'NOOP',active[2],json.dumps(value,ensure_ascii=False),source,request_id)
                            continue
                        current=con.execute('SELECT COALESCE(MAX(version),0) FROM customer_preferences WHERE broker_id=? AND customer_id=? AND key=?',(broker,cid,key)).fetchone()[0]
                        version=current+1;memory_id='pref-'+hashlib.sha256(json.dumps([broker,cid,key,request_id]).encode()).hexdigest()
                        con.execute('UPDATE customer_preferences SET active=0 WHERE broker_id=? AND customer_id=? AND key=?',(broker,cid,key))
                        con.execute('INSERT INTO customer_preferences VALUES (?,?,?,?,?,?,?,?,?,?)',(broker,cid,key,version,json.dumps(value,ensure_ascii=False),source,request_id,memory_id,1,time.time()))
                        self._log_change(con,broker,cid,'preference',key,'UPDATE' if active else 'ADD',active[2] if active else None,json.dumps(value,ensure_ascii=False),source,request_id)
                if self.store.get(memory_id,namespace=self.namespace(broker,cid)) is None:
                    self.store.add(f'客户偏好 {key}：{value}',namespace=self.namespace(broker,cid),id=memory_id,
                        metadata={'broker_id':broker,'customer_id':cid,'key':key,'version':version,'source':source})
        self._changed(broker,cid)
        return self.profile(broker,cid)
    @staticmethod
    def _log_change(con,broker,cid,memory_type,key,action,old,new,source,request_id):
        con.execute('INSERT OR IGNORE INTO memory_changes(broker_id,customer_id,memory_type,key,action,old_value,new_value,source,request_id,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (broker,cid,memory_type,key,action,old,new,(source or '')[:500],request_id,time.time()))
    def changes(self,broker,cid,limit=200):
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory=sqlite3.Row
            return [dict(row) for row in con.execute('SELECT * FROM memory_changes WHERE broker_id=? AND customer_id=? ORDER BY created DESC,change_id DESC LIMIT ?',(broker,cid,limit))]
    def _changed(self,broker,cid):
        if self.on_change:
            try:self.on_change(broker,cid)
            except Exception:pass
    def update_preference(self,broker,cid,key,value):
        """A broker edit becomes a new version; the previous version stays as history."""
        if key not in Preferences.model_fields:raise ValueError('未知的客户偏好字段。')
        try:values=Preferences.model_validate({key:value}).model_dump(exclude_unset=True)
        except ValidationError as error:raise ValueError('这个偏好值不符合字段要求，请检查后重试。') from error
        return self.apply(broker,cid,values,'经纪人手动修改','manual-'+uuid.uuid4().hex)
    def update_record(self,broker,cid,record_id,*,content=None,value=None):
        """Edit one semantic/episodic record through the component's versioned admin."""
        scope=self.long_memory.scope(broker,cid)
        record=self.long_memory.plugin.get_record(record_id,scope,include_history=True)
        if record is None:raise ValueError('找不到这条记忆。')
        changes={}
        if content is not None and content.strip() and content.strip()!=record.get('content'):changes['content']=content.strip()
        if value is not None and value.strip() and value.strip()!=record.get('value'):changes['value']=value.strip()
        if not changes:raise ValueError('内容没有变化。')
        changed=self.admin.edit(record_id,scope,**changes)
        updated=self.long_memory.plugin.get_record(changed.memory_id,scope,include_history=True)
        with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
            self._log_change(con,broker,cid,record.get('memory_type','memory'),record.get('attribute',''),'UPDATE',
                record.get('content'),updated.get('content') if updated else None,'经纪人手动修改','manual-edit-'+uuid.uuid4().hex)
        self._changed(broker,cid)
        return updated

    def delete_record(self,broker,cid,record_id):
        """Forget one structured preference or typed memory record for this customer."""
        record_id=(record_id or '').strip()
        if not record_id:raise ValueError('缺少要删除的记忆记录。')
        is_preference=False;delete_id='manual-delete-'+uuid.uuid4().hex
        with self.lock,closing(sqlite3.connect(self.path,timeout=5)) as con,con:
            row=con.execute('SELECT key,value FROM customer_preferences WHERE broker_id=? AND customer_id=? AND memory_id=?',(broker,cid,record_id)).fetchone()
            if row:
                con.execute('UPDATE customer_preferences SET active=0 WHERE broker_id=? AND customer_id=? AND memory_id=?',(broker,cid,record_id))
                self._log_change(con,broker,cid,'preference',row[0],'DELETE',row[1],None,'经纪人删除',delete_id)
                is_preference=True
        if is_preference:
            self.store.delete(record_id,namespace=self.namespace(broker,cid));self._changed(broker,cid);return True
        scope=self.long_memory.scope(broker,cid)
        record=self.long_memory.plugin.get_record(record_id,scope,include_history=True)
        if record is None:return False
        # Imported pre-integration preferences have a structured row behind them.
        if record.get('source',{}).get('conversation_id')=='legacy-preferences':
            with self.lock,closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                con.execute('UPDATE customer_preferences SET active=0 WHERE broker_id=? AND customer_id=? AND key=?',(broker,cid,record.get('attribute','')))
        deleted=self.long_memory.plugin.delete(record_id,scope)
        if deleted:
            with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                self._log_change(con,broker,cid,record.get('memory_type','memory'),record.get('attribute',''),'DELETE',record.get('value'),None,'经纪人删除',delete_id)
            self._changed(broker,cid)
        return deleted
    def undo_batch(self,broker,cid,batch_id):
        """Roll back structured preferences written by one confirmation batch."""
        memory_ids=[];changed=0
        with self.lock:
            with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                rows=con.execute('SELECT key,version,memory_id FROM customer_preferences WHERE broker_id=? AND customer_id=? AND request_id=? ORDER BY key,version',(broker,cid,batch_id)).fetchall()
                grouped={}
                for key,version,memory_id in rows:grouped.setdefault(key,[]).append((version,memory_id))
                for key,items in grouped.items():
                    first=min(version for version,_ in items)
                    batch_value=con.execute('SELECT value FROM customer_preferences WHERE broker_id=? AND customer_id=? AND key=? AND request_id=? ORDER BY version DESC LIMIT 1',(broker,cid,key,batch_id)).fetchone()
                    con.execute('UPDATE customer_preferences SET active=0 WHERE broker_id=? AND customer_id=? AND key=? AND request_id=?',(broker,cid,key,batch_id))
                    previous=con.execute('SELECT memory_id,value FROM customer_preferences WHERE broker_id=? AND customer_id=? AND key=? AND version<? ORDER BY version DESC LIMIT 1',(broker,cid,key,first)).fetchone()
                    if previous:con.execute('UPDATE customer_preferences SET active=1 WHERE memory_id=?',(previous[0],))
                    self._log_change(con,broker,cid,'preference',key,'UNDO',batch_value[0] if batch_value else None,previous[1] if previous else None,'撤销最近写入',batch_id)
                    memory_ids.extend(memory_id for _,memory_id in items);changed+=1
            for memory_id in memory_ids:self.store.delete(memory_id,namespace=self.namespace(broker,cid))
        if changed:self._changed(broker,cid)
        return {'keys':changed,'records':len(memory_ids)}
    def clear(self,broker,cid):
        """Clear every remembered preference, semantic and episodic record of a customer."""
        with self.lock:
            with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                preferences=con.execute('SELECT COUNT(*) FROM customer_preferences WHERE broker_id=? AND customer_id=?',(broker,cid)).fetchone()[0]
                con.execute('DELETE FROM customer_preferences WHERE broker_id=? AND customer_id=?',(broker,cid))
                if preferences:self._log_change(con,broker,cid,'*','*','CLEAR',json.dumps({'preferences':preferences}),None,'经纪人清空','manual-clear-'+uuid.uuid4().hex)
            self.store.clear(namespace=self.namespace(broker,cid))
        rows=self.long_memory.context(broker,cid,history=True)
        ids={record['id'] for records in rows.values() for record in records}
        scope=self.long_memory.scope(broker,cid)
        deleted=sum(1 for record_id in ids if self.long_memory.plugin.delete(record_id,scope))
        if deleted:
            with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                self._log_change(con,broker,cid,'*','*','CLEAR',json.dumps({'typed':deleted}),None,'经纪人清空','manual-clear-'+uuid.uuid4().hex)
        self._changed(broker,cid)
        return {'preferences':preferences,'typed':deleted}
    def undo_typed(self,broker,cid,batch_id,sid):
        result=self.long_memory.undo(broker,cid,batch_id,sid)
        if result['removed'] or result['restored']:
            with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
                self._log_change(con,broker,cid,'memory','*','UNDO',json.dumps(result),None,'撤销最近写入',batch_id)
            self._changed(broker,cid)
        return result
    def has_episodic(self,broker,cid):return self.long_memory.has_episodic(broker,cid)
    def recall_light(self,broker,cid):
        """Structured preferences plus semantic background; no episodic retrieval yet."""
        typed=self.typed_context(broker,cid,episodic=False)
        versions=self.versions(broker,cid)
        return dict(preferences=self.profile(broker,cid),
            preference_versions={r['key']:{'version':r['version'],'created':r['created']} for r in versions if r['active']},
            **typed,evidence=[],retrieval_status='light')
    def recall(self,broker,cid,query):
        typed=self.typed_context(broker,cid,query=query)
        versions=self.versions(broker,cid);active={r['memory_id'] for r in versions if r['active']}
        hits=self.store.search(query,namespace=self.namespace(broker,cid),limit=6,min_score=0,diversity=0.35,memory_filter=lambda m:m.id in active)
        return dict(preferences=self.profile(broker,cid),preference_versions={r['key']:{'version':r['version'],'created':r['created']} for r in versions if r['active']},**typed,evidence=[{'content':h.memory.content,'version':h.memory.metadata.get('version'),'score':round(h.score,4)} for h in hits if h.memory.id in active][:6]+self.long_memory.search(broker,cid,query))
    def typed_context(self,broker,cid,*,history=False,query=None,episodic=True):
        self.long_memory.import_preferences(broker,cid,self.versions(broker,cid))
        return self.long_memory.context(broker,cid,history=history,query=query,episodic=episodic)
    def prepare_typed(self,broker,cid,sid,source,response,history,chat):
        self.typed_context(broker,cid)
        from .search_planning import customer_preferences
        existing=self.profile(broker,cid);explicit=customer_preferences(source['content'])
        required={key:value for key,value in explicit.items() if key not in existing or existing[key]!=value}
        return self.long_memory.prepare(broker,cid,sid,source,response,history,chat,required)
    def commit_typed(self,broker,cid,plan,job_id,chat):
        result=self.long_memory.commit(broker,cid,plan,job_id,chat)
        try:self._log_typed_commit(broker,cid,plan,result,job_id)
        except Exception:pass
        return result
    def _log_typed_commit(self,broker,cid,plan,result,job_id):
        decisions=result.get('decisions') or []
        if not decisions:return
        scope=self.long_memory.scope(broker,cid,plan.get('sid','customer-profile'));previous={}
        for decision in decisions:
            target=decision.get('target_memory_id')
            if target:
                record=self.long_memory.plugin.get_record(target,scope,include_history=True)
                if record:previous[decision.get('candidate_index')]=record.get('value')
        with closing(sqlite3.connect(self.path,timeout=5)) as con,con:
            for decision in decisions:
                index=decision.get('candidate_index');candidates=plan.get('candidates') or []
                if index is None or index>=len(candidates):continue
                candidate=candidates[index];action=decision.get('action')
                if action not in ('ADD','UPDATE','NOOP','PENDING'):continue
                self._log_change(con,broker,cid,candidate.get('memory_type','memory'),candidate.get('attribute',''),action,
                    previous.get(index),candidate.get('value'),candidate.get('source',{}).get('evidence',''),job_id)
    def close(self):self.store.close()
