"""Persistent, broker-scoped intake drafts and an idempotent indexing outbox."""
from __future__ import annotations
import json,time,uuid
from .domain import Property

class CatalogStore:
    def __init__(self,business):
        self.business=business
        with business.connection() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS catalog_drafts(
              entry_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,status TEXT NOT NULL,
              revision INTEGER NOT NULL,messages TEXT NOT NULL,payload TEXT NOT NULL,
              preview TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS catalog_index_jobs(
              entry_id TEXT PRIMARY KEY,broker_id TEXT NOT NULL,payload TEXT NOT NULL,
              status TEXT NOT NULL,last_error TEXT NOT NULL,created REAL NOT NULL);
            ''')
    def create(self,broker):
        eid=uuid.uuid4().hex;now=time.time()
        with self.business.connection() as con:con.execute('INSERT INTO catalog_drafts VALUES (?,?,?,?,?,?,?,?,?,?)',(eid,broker,'collecting',0,'[]','{}','',now,now,now+86400))
        return self.get(broker,eid)
    def get(self,broker,eid):
        with self.business.connection() as con:row=con.execute('SELECT * FROM catalog_drafts WHERE entry_id=? AND broker_id=?',(eid,broker)).fetchone()
        if row is None:raise PermissionError('这份录入草稿不属于当前工作区。')
        result=dict(row);result['messages']=json.loads(result['messages']);result['payload']=json.loads(result['payload']);return result
    def pending(self,broker):
        with self.business.connection() as con:rows=con.execute("SELECT entry_id FROM catalog_drafts WHERE broker_id=? AND status IN ('collecting','pending') AND expires>? ORDER BY updated DESC",(broker,time.time())).fetchall()
        return [self.get(broker,r[0]) for r in rows]
    def append(self,broker,eid,text):
        with self.business.connection() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM catalog_drafts WHERE entry_id=? AND broker_id=?',(eid,broker)).fetchone()
            if row is None:raise PermissionError('这份录入草稿不属于当前工作区。')
            if row['status'] not in ('collecting','pending') or row['expires']<time.time():raise ValueError('该草稿已完成、取消或过期，请新建录入。')
            messages=json.loads(row['messages'])
            if sum(len(m['content']) for m in messages)+len(text)>30000:raise ValueError('这条录入内容过长，请按不同房源或小区分开录入。')
            messages.append({'role':'user','content':text});rev=row['revision']+1
            # Editing immediately invalidates the previous preview, even if the model fails.
            con.execute("UPDATE catalog_drafts SET revision=?,messages=?,status='collecting',payload='{}',preview='',updated=? WHERE entry_id=?",(rev,json.dumps(messages,ensure_ascii=False),time.time(),eid))
        return self.get(broker,eid)
    def stage(self,broker,eid,revision,payload,reply,ready):
        with self.business.connection() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM catalog_drafts WHERE entry_id=? AND broker_id=?',(eid,broker)).fetchone()
            if not row or row['revision']!=revision or row['status']!='collecting':raise ValueError('录入内容已变化，请查看最新草稿。')
            messages=json.loads(row['messages']);messages.append({'role':'assistant','content':reply})
            con.execute('UPDATE catalog_drafts SET status=?,payload=?,preview=?,messages=?,updated=? WHERE entry_id=?',('pending' if ready else 'collecting',json.dumps(payload,ensure_ascii=False),reply,json.dumps(messages,ensure_ascii=False),time.time(),eid))
        return self.get(broker,eid)
    def confirm(self,broker,eid,revision,approved):
        if type(approved) is not bool:raise ValueError('请明确确认或取消。')
        with self.business.connection() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM catalog_drafts WHERE entry_id=? AND broker_id=?',(eid,broker)).fetchone()
            if row is None:raise PermissionError('这份录入草稿不属于当前工作区。')
            if row['revision']!=revision:raise ValueError('草稿已修改，不能保存旧预览，请重新核对。')
            payload=json.loads(row['payload'])
            if row['status']=='approved':return {'status':'saved','replayed':True,'property_id':payload.get('property',{}).get('property_id'),'has_material':bool(payload.get('material'))}
            if row['status']=='cancelled':return {'status':'cancelled','replayed':True}
            if row['expires']<time.time():raise ValueError('录入草稿已过期，请重新录入。')
            if not approved:
                con.execute("UPDATE catalog_drafts SET status='cancelled',updated=? WHERE entry_id=?",(time.time(),eid));return {'status':'cancelled','replayed':False}
            if row['status']!='pending':raise ValueError('必要信息尚未补全，不能入库。')
            property_id=None
            if payload.get('property'):
                p=Property.model_validate(payload['property']);property_id=p.property_id
                if p.floor>p.total_floors:raise ValueError('房源楼层不能高于总楼层。')
                # Intake adds new records; a model cannot choose an existing ID and overwrite it.
                con.execute('INSERT INTO properties VALUES (?,?,?,?,?,?,?,?,?)',(p.property_id,p.community_key,p.region,p.price_yuan,p.bedrooms,p.floor,int(p.has_elevator),p.metro_distance_m,p.model_dump_json()))
            if payload.get('material'):
                con.execute('INSERT INTO catalog_index_jobs VALUES (?,?,?,?,?,?)',(eid,broker,json.dumps(payload['material'],ensure_ascii=False),'pending','',time.time()))
            con.execute("UPDATE catalog_drafts SET status='approved',updated=? WHERE entry_id=?",(time.time(),eid))
            return {'status':'saved','replayed':False,'property_id':property_id,'has_material':bool(payload.get('material'))}
    def jobs(self,broker):
        with self.business.connection() as con:return [dict(r) for r in con.execute("SELECT * FROM catalog_index_jobs WHERE broker_id=? AND status='pending' ORDER BY created",(broker,))]
    def finish(self,broker,eid,error=''):
        with self.business.connection() as con:con.execute('UPDATE catalog_index_jobs SET status=?,last_error=? WHERE entry_id=? AND broker_id=?',('pending' if error else 'done',error[:100],eid,broker))
