from __future__ import annotations
import hashlib,json,sqlite3,time,uuid
from contextlib import contextmanager
from .domain import Property,ListingQuery

class BusinessStore:
    def __init__(self,path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS customers(customer_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,name TEXT NOT NULL,notes TEXT NOT NULL,is_demo INTEGER NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS properties(property_id TEXT PRIMARY KEY,community_key TEXT NOT NULL,region TEXT NOT NULL,price_yuan INTEGER NOT NULL,bedrooms INTEGER NOT NULL,floor INTEGER NOT NULL,has_elevator INTEGER NOT NULL,metro_distance_m INTEGER,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,title TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(message_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts(draft_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,session_id TEXT NOT NULL,request_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL,UNIQUE(broker_id,customer_id,request_id));
            CREATE TABLE IF NOT EXISTS recommendations(record_id TEXT PRIMARY KEY,draft_id TEXT UNIQUE NOT NULL,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,session_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS followups(followup_id TEXT PRIMARY KEY,draft_id TEXT UNIQUE NOT NULL,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,record_id TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_jobs(job_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,customer_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,last_error TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS property_filter ON properties(price_yuan,bedrooms,floor);
            CREATE INDEX IF NOT EXISTS customer_sessions ON sessions(broker_id,customer_id,updated);
            ''')
    @contextmanager
    def connection(self):
        con=sqlite3.connect(self.path,timeout=5);con.row_factory=sqlite3.Row
        con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA busy_timeout=5000')
        try:
            with con:yield con
        finally:con.close()
    def create_customer(self,broker,name,notes='',is_demo=False,customer_id=None):
        name=name.strip()
        if not name or len(name)>80:raise ValueError('请填写1至80字的客户称呼。')
        cid=customer_id or uuid.uuid4().hex
        with self.connection() as con:
            con.execute('INSERT INTO customers VALUES (?,?,?,?,?,?)',(cid,broker,name,notes[:1000],int(is_demo),time.time()))
        return self.customer(broker,cid)
    def customers(self,broker):
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT * FROM customers WHERE broker_id=? ORDER BY created DESC',(broker,))]
    def customer(self,broker,cid):
        with self.connection() as con:r=con.execute('SELECT * FROM customers WHERE broker_id=? AND customer_id=?',(broker,cid)).fetchone()
        if not r:raise PermissionError('当前经纪人无法访问这位客户。')
        return dict(r)
    def create_session(self,broker,cid):
        self.customer(broker,cid);sid=uuid.uuid4().hex;now=time.time()
        with self.connection() as con:con.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',(sid,broker,cid,'新对话',now,now))
        return sid
    def session(self,broker,cid,sid):
        self.customer(broker,cid)
        with self.connection() as con:r=con.execute('SELECT * FROM sessions WHERE broker_id=? AND customer_id=? AND session_id=?',(broker,cid,sid)).fetchone()
        if not r:raise PermissionError('这个对话不属于当前客户。请为当前客户新建对话。')
        return dict(r)
    def sessions(self,broker,cid):
        self.customer(broker,cid)
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT * FROM sessions WHERE broker_id=? AND customer_id=? ORDER BY updated DESC',(broker,cid))]
    def append(self,broker,cid,sid,role,text,message_id):
        self.session(broker,cid,sid)
        with self.connection() as con:
            con.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',(message_id,sid,broker,cid,role,text,time.time()))
            if role=='user':con.execute('UPDATE sessions SET title=CASE WHEN title=? THEN ? ELSE title END,updated=? WHERE session_id=?',('新对话',text[:28],time.time(),sid))
    def history(self,broker,cid,sid):
        self.session(broker,cid,sid)
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT role,content FROM messages WHERE broker_id=? AND customer_id=? AND session_id=? ORDER BY created,rowid',(broker,cid,sid))]
    def preference_history(self,broker,cid,sid):
        self.session(broker,cid,sid)
        with self.connection() as con:return [dict(r) for r in con.execute("SELECT content,created FROM messages WHERE broker_id=? AND customer_id=? AND session_id=? AND role='user' ORDER BY created,rowid",(broker,cid,sid))]
    def message(self,broker,cid,sid,message_id):
        self.session(broker,cid,sid)
        with self.connection() as con:row=con.execute("SELECT message_id,content,created FROM messages WHERE broker_id=? AND customer_id=? AND session_id=? AND message_id=? AND role='user'",(broker,cid,sid,message_id)).fetchone()
        if not row:raise PermissionError('找不到当前客户的原始消息。')
        return dict(row)
    def put_property(self,payload):
        p=Property.model_validate(payload)
        if p.floor>p.total_floors:raise ValueError('房源楼层不能高于总楼层。')
        with self.connection() as con:con.execute('INSERT OR REPLACE INTO properties VALUES (?,?,?,?,?,?,?,?,?)',
            (p.property_id,p.community_key,p.region,p.price_yuan,p.bedrooms,p.floor,int(p.has_elevator),p.metro_distance_m,p.model_dump_json()))
    def properties(self):
        with self.connection() as con:return [json.loads(r[0]) for r in con.execute('SELECT payload FROM properties ORDER BY property_id')]
    def put_properties(self,items):
        # Validate the entire batch before a single transaction; reject demo-ID collisions.
        values=[Property.model_validate(p) for p in items]
        if any(p.floor>p.total_floors for p in values):raise ValueError('房源楼层不能高于总楼层。')
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            for p in values:
                old=con.execute('SELECT payload FROM properties WHERE property_id=?',(p.property_id,)).fetchone()
                if old and json.loads(old[0])['is_demo'] and not p.is_demo:raise ValueError('房源ID与内置样例数据冲突，请使用独立ID（例如R开头）。')
                con.execute('INSERT OR REPLACE INTO properties VALUES (?,?,?,?,?,?,?,?,?)',(p.property_id,p.community_key,p.region,p.price_yuan,p.bedrooms,p.floor,int(p.has_elevator),p.metro_distance_m,p.model_dump_json()))
    def property(self,id):
        with self.connection() as con:r=con.execute('SELECT payload FROM properties WHERE property_id=?',(id,)).fetchone()
        return json.loads(r[0]) if r else None
    def delete_property(self,id):
        with self.connection() as con:con.execute('DELETE FROM properties WHERE property_id=?',(id,))
    def search(self,query):
        q=ListingQuery.model_validate(query);sql='SELECT payload FROM properties WHERE 1=1';params=[]
        with self.connection() as con:has_real=con.execute("SELECT 1 FROM properties WHERE json_extract(payload,'$.is_demo')=0 LIMIT 1").fetchone()
        if has_real and q.community_keys is None and q.property_ids is None:sql+=" AND json_extract(payload,'$.is_demo')=0"
        for field,column in [('community_keys','community_key'),('property_ids','property_id')]:
            values=getattr(q,field)
            if values is not None:
                if not values:return []
                sql+=' AND '+column+' IN ('+','.join('?' for _ in values)+')';params.extend(values)
        for key,op in [('max_price_yuan','<='),('bedrooms','='),('min_floor','>='),('max_floor','<=')]:
            value=getattr(q,key)
            if value is not None:
                column={'max_price_yuan':'price_yuan','min_floor':'floor','max_floor':'floor'}.get(key,key)
                sql+=' AND '+column+op+'?';params.append(value)
        if q.region:sql+=' AND region=?';params.append(q.region)
        if q.min_area_sqm is not None:sql+=" AND json_extract(payload,'$.area_sqm')>=?";params.append(q.min_area_sqm)
        if q.max_area_sqm is not None:sql+=" AND json_extract(payload,'$.area_sqm')<=?";params.append(q.max_area_sqm)
        if q.orientation:sql+=" AND json_extract(payload,'$.orientation') LIKE ?";params.append('%'+q.orientation+'%')
        if q.decoration:sql+=" AND json_extract(payload,'$.decoration')=?";params.append(q.decoration)
        if q.require_elevator is not None:sql+=' AND has_elevator=?';params.append(int(q.require_elevator))
        if q.max_metro_distance_m is not None:sql+=' AND metro_distance_m IS NOT NULL AND metro_distance_m<=?';params.append(q.max_metro_distance_m)
        sql+=' ORDER BY '+('COALESCE(metro_distance_m,100000),price_yuan' if q.prefer_near_metro else 'price_yuan,property_id')+' LIMIT ?';params.append(q.limit)
        with self.connection() as con:return [json.loads(r[0]) for r in con.execute(sql,params)]
    def property_details(self,id):
        with self.connection() as con:row=con.execute('SELECT payload FROM properties WHERE property_id=? COLLATE NOCASE',(id,)).fetchone()
        return json.loads(row[0]) if row else None
    def community_catalog(self):
        properties=self.properties();has_real=any(not p['is_demo'] for p in properties);rows={}
        for p in properties:
            if has_real and p['is_demo']:continue
            rows[p['community_key']]={k:p[k] for k in ('community_key','community_name','region','is_demo')}
        return list(rows.values())
    def has_real_properties(self):
        with self.connection() as con:return bool(con.execute("SELECT 1 FROM properties WHERE json_extract(payload,'$.is_demo')=0 LIMIT 1").fetchone())
    def prepare(self,broker,cid,sid,request_id,payload):
        self.session(broker,cid,sid);text=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            old=con.execute('SELECT * FROM drafts WHERE broker_id=? AND customer_id=? AND request_id=?',(broker,cid,request_id)).fetchone()
            if old:
                if old['payload']!=text:raise ValueError('同一请求不能产生不同的确认内容。')
                return dict(old)
            # A revised report replaces the previous pending report in this session;
            # only the newest draft may be confirmed. Follow-up drafts are separate.
            if payload.get('kind') in ('recommend','report'):
                con.execute("UPDATE drafts SET status='superseded' WHERE broker_id=? AND customer_id=? AND session_id=? AND status='pending' AND json_extract(payload,'$.kind') IN ('recommend','report')",(broker,cid,sid))
            id=uuid.uuid4().hex;now=time.time()
            con.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?)',(id,broker,cid,sid,request_id,text,'pending',now+86400,now))
        return self.draft(broker,cid,sid,id)
    def draft(self,broker,cid,sid,id):
        self.session(broker,cid,sid)
        with self.connection() as con:r=con.execute('SELECT * FROM drafts WHERE draft_id=? AND broker_id=? AND customer_id=? AND session_id=?',(id,broker,cid,sid)).fetchone()
        if not r:raise PermissionError('不能确认其他客户或其他对话的草稿。')
        return dict(r)
    def pending(self,broker,cid,sid=None):
        self.customer(broker,cid)
        sql='SELECT * FROM drafts WHERE broker_id=? AND customer_id=? AND status=? AND expires>?';params=[broker,cid,'pending',time.time()]
        if sid: self.session(broker,cid,sid);sql+=' AND session_id=?';params.append(sid)
        with self.connection() as con:return [dict(r) for r in con.execute(sql+' ORDER BY created',params)]
    def commit(self,broker,cid,sid,id,approved):
        if type(approved) is not bool:raise ValueError('确认结果必须是明确的是或否。')
        self.draft(broker,cid,sid,id)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM drafts WHERE draft_id=?',(id,)).fetchone()
            if row['status']=='cancelled':return dict(status='cancelled',replayed=True)
            if row['status']=='superseded':return dict(status='superseded',replayed=False)
            if row['status']=='undone':return dict(status='undone',replayed=False)
            if row['status']=='approved':
                record=con.execute('SELECT record_id FROM recommendations WHERE draft_id=?',(id,)).fetchone()
                return dict(status='saved',record_id=record[0],replayed=True)
            if row['expires']<time.time():raise ValueError('草稿已过期，请重新生成。')
            if not approved:
                con.execute('UPDATE drafts SET status=? WHERE draft_id=?',('cancelled',id));return dict(status='cancelled',replayed=False)
            payload=json.loads(row['payload']);rid=uuid.uuid4().hex;now=time.time()
            con.execute('INSERT INTO recommendations VALUES (?,?,?,?,?,?,?)',(rid,id,broker,cid,sid,row['payload'],now))
            content=payload.get('follow_up') or '推荐已保存，待与客户沟通后补充跟进。'
            con.execute('INSERT INTO followups VALUES (?,?,?,?,?,?,?)',(uuid.uuid4().hex,id,broker,cid,rid,content,now))
            job=dict(preferences=payload.get('preference_updates',{}),source=payload.get('preference_source') or payload.get('source_text',''))
            if payload.get('memory_plan') is not None:job['memory_plan']=payload['memory_plan']
            con.execute('INSERT INTO memory_jobs VALUES (?,?,?,?,?,?)',(id,broker,cid,json.dumps(job,ensure_ascii=False),'pending' if job['preferences'] or job.get('memory_plan') is not None else 'done',''))
            con.execute('UPDATE drafts SET status=? WHERE draft_id=?',('approved',id))
            return dict(status='saved',record_id=rid,replayed=False)
    def undo_last(self,broker,cid):
        """Undo the customer's latest confirmed write; preference rollback is separate."""
        self.customer(broker,cid)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            record=con.execute('SELECT * FROM recommendations WHERE broker_id=? AND customer_id=? ORDER BY created DESC,rowid DESC LIMIT 1',(broker,cid)).fetchone()
            if not record:raise ValueError('这位客户还没有可撤销的已保存推荐。')
            draft=con.execute('SELECT * FROM drafts WHERE draft_id=?',(record['draft_id'],)).fetchone()
            con.execute('DELETE FROM followups WHERE record_id=?',(record['record_id'],))
            con.execute('DELETE FROM recommendations WHERE record_id=?',(record['record_id'],))
            con.execute('DELETE FROM memory_jobs WHERE job_id=?',(record['draft_id'],))
            if draft:con.execute("UPDATE drafts SET status='undone' WHERE draft_id=?",(record['draft_id'],))
            return {'record_id':record['record_id'],'draft_id':record['draft_id'],'session_id':record['session_id'],
                'request_id':draft['request_id'] if draft else record['draft_id'],'created':record['created']}
    def memory_jobs(self,broker,cid):
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT j.* FROM memory_jobs j JOIN recommendations r ON r.draft_id=j.job_id WHERE j.broker_id=? AND j.customer_id=? AND j.status=? ORDER BY r.created,r.rowid',(broker,cid,'pending'))]
    def finish_memory(self,id,error=''):
        with self.connection() as con:con.execute('UPDATE memory_jobs SET status=?,last_error=? WHERE job_id=?',('pending' if error else 'done',error[:100],id))
    def records(self,broker,cid):
        self.customer(broker,cid)
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT * FROM recommendations WHERE broker_id=? AND customer_id=? ORDER BY created DESC',(broker,cid))]
    def followups(self,broker,cid):
        self.customer(broker,cid)
        with self.connection() as con:return [dict(r) for r in con.execute('SELECT * FROM followups WHERE broker_id=? AND customer_id=? ORDER BY created DESC',(broker,cid))]
