from __future__ import annotations
import tkinter as tk
from tkinter import ttk,messagebox
from datetime import datetime
from .widgets import COLORS as C,RoundedFrame,markdown_segments
from .app import label,button,FONT

class CatalogWindow:
    def __init__(self,app):
        self.app=app;self.service=app.runtime.catalog;self.busy=False;self.row=None
        self.win=app.dialog('对话录入 · 房源与小区资料','1000x840');self.win.minsize(850,710)
        body=tk.Frame(self.win,bg=C['page']);body.grid(row=1,column=0,sticky='nsew',padx=24,pady=(0,20));body.columnconfigure(0,weight=1);body.rowconfigure(2,weight=1)
        toolbar=tk.Frame(body,bg=C['page']);toolbar.grid(row=0,column=0,sticky='ew',pady=(0,10));toolbar.columnconfigure(0,weight=1)
        self.selector=ttk.Combobox(toolbar,state='readonly',width=48);self.selector.grid(row=0,column=0,sticky='ew',padx=(0,10));self.selector.bind('<<ComboboxSelected>>',self.select)
        self.new=button(toolbar,'新一条资料',self.create);self.new.grid(row=0,column=1,padx=(0,10))
        label(body,'直接粘贴房源描述或小区介绍，补充信息后核对入库。这里的内容独立于客户对话。',9,fg=C['muted'],anchor='w').grid(row=1,column=0,sticky='ew',pady=(0,10))
        card=RoundedFrame(body,padding=14,radius=22);card.grid(row=2,column=0,sticky='nsew');card.body.columnconfigure(0,weight=1);card.body.rowconfigure(0,weight=1)
        self.chat=tk.Text(card.body,wrap='word',bg='white',fg=C['text'],font=(FONT,11),bd=0,highlightthickness=0,padx=6,pady=8,state='disabled',spacing3=6);self.chat.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(card.body,command=self.chat.yview);scroll.grid(row=0,column=1,sticky='ns');self.chat.configure(yscrollcommand=scroll.set)
        self.chat.tag_configure('role',font=(FONT,10,'bold'),foreground=C['accent'],spacing1=16,spacing3=7);self.chat.tag_configure('heading',font=(FONT,13,'bold'),foreground=C['accent'],spacing1=12);self.chat.tag_configure('strong',font=(FONT,11,'bold'))
        self.status=tk.StringVar();label(body,'',9,textvariable=self.status,fg=C['muted'],anchor='w').grid(row=3,column=0,sticky='ew',pady=(10,8))
        composer=RoundedFrame(body,padding=12);composer.grid(row=4,column=0,sticky='ew');composer.body.columnconfigure(0,weight=1)
        self.input=tk.Text(composer.body,height=3,wrap='word',font=(FONT,11),bg='white',fg=C['text'],bd=0,highlightthickness=0);self.input.grid(row=0,column=0,sticky='ew');self.input.bind('<Control-Return>',lambda event:self.send() or 'break')
        self.send_button=button(composer.body,'发送信息',self.send,True);self.send_button.grid(row=0,column=1,sticky='se',padx=(12,0))
        actions=tk.Frame(body,bg=C['page']);actions.grid(row=5,column=0,sticky='ew',pady=(12,0));actions.columnconfigure(2,weight=1)
        self.reparse=button(actions,'重新整理',self.retry);self.reparse.grid(row=0,column=0,padx=(0,8))
        self.reindex=button(actions,'重试资料索引',self.retry_index);self.reindex.grid(row=0,column=1)
        self.cancel=button(actions,'取消这条录入',lambda:self.confirm(False));self.cancel.grid(row=0,column=3,padx=(8,8))
        self.save=button(actions,'确认入库',lambda:self.confirm(True),True);self.save.grid(row=0,column=4)
        pending=self.service.pending();self.display(pending[0] if pending else self.service.create())
    def active(self):return bool(self.win.winfo_exists())
    def refresh_list(self):
        self.rows=self.service.pending();values=[]
        for row in self.rows:
            text=next((m['content'] for m in row['messages'] if m['role']=='user'),'新资料')
            values.append(datetime.fromtimestamp(row['updated']).strftime('%m-%d %H:%M')+' · '+text[:26])
        self.selector.configure(values=values)
        for i,row in enumerate(self.rows):
            if self.row and row['entry_id']==self.row['entry_id']:self.selector.current(i);break
        else:self.selector.set('已保存 / 已取消，可新建下一条')
    def append(self,role,text):
        self.chat.configure(state='normal');self.chat.insert('end',('你' if role=='user' else '录入助手')+'\n','role')
        for part,tag in markdown_segments(text):self.chat.insert('end',part,tag)
        self.chat.insert('end','\n');self.chat.configure(state='disabled');self.chat.see('end')
    def display(self,row):
        self.row=row;self.chat.configure(state='normal');self.chat.delete('1.0','end');self.chat.configure(state='disabled')
        if row['messages']:
            for m in row['messages']:self.append(m['role'],m['content'])
        else:self.append('assistant','直接发送房源或小区资料即可。我会整理已知信息，缺少必要字段时继续追问；确认前只保留草稿。\n\n房源编号自动生成，小区名称与区域用于关联资料。')
        self.refresh_list();self.set_busy(False)
    def set_busy(self,value):
        self.busy=value
        if not self.active():return
        self.selector.configure(state='disabled' if value else 'readonly')
        for w in (self.new,self.send_button,self.reindex):w.configure(state='disabled' if value else 'normal')
        self.input.configure(state='disabled' if value else 'normal')
        status=self.row['status'] if self.row else 'collecting'
        self.save.configure(state='normal' if not value and status=='pending' else 'disabled');self.cancel.configure(state='normal' if not value and status in ('pending','collecting') else 'disabled')
        self.reparse.configure(state='normal' if not value and status=='collecting' and bool(self.row['messages']) else 'disabled')
        self.status.set('正在整理，请稍候…' if value else '草稿已就绪，可以继续发消息更正，或确认入库。' if status=='pending' else '尚未入库 · Ctrl + Enter 发送信息' if status=='collecting' else '这条录入已结束，可以新建下一条。')
    def select(self,event=None):
        i=self.selector.current()
        if not self.busy and 0<=i<len(self.rows):self.display(self.service.get(self.rows[i]['entry_id']))
    def create(self):
        if not self.busy:self.display(self.service.create());self.input.delete('1.0','end')
    def handle_error(self,error):
        self.app.set_busy(False)
        if not self.active():return
        self.display(self.service.get(self.row['entry_id']))
        msg=str(error) if isinstance(error,(ValueError,PermissionError)) else '这次整理未完成，原文草稿已保留，请稍后重试。'
        self.status.set(msg);messagebox.showerror('录入未完成',msg,parent=self.win)
    def done(self,row):
        self.app.set_busy(False)
        if self.active():self.display(row)
    def run(self,coroutine):
        self.set_busy(True);self.app.set_busy(True,'正在整理房源或小区资料…');self.app.bridge.submit(coroutine,self.done,self.handle_error)
    def send(self):
        if self.busy:return
        text=self.input.get('1.0','end').strip()
        if not text:return
        if self.row['status'] in ('approved','cancelled'):self.create()
        self.input.delete('1.0','end')
        self.run(self.service.chat(self.row['entry_id'],text))
    def retry(self):
        if not self.busy:self.run(self.service.reprocess(self.row['entry_id']))
    def confirm(self,approved):
        if self.busy:return
        eid,revision=self.row['entry_id'],self.row['revision'];self.set_busy(True);self.app.set_busy(True,'正在写入房源与资料库…')
        def finish(result):
            self.app.set_busy(False)
            self.app.refresh_catalog_views()
            if not self.active():return
            self.display(self.service.get(eid))
            if result['status']=='cancelled':self.append('assistant','已取消，房源与小区资料均未入库。');return
            text='已确认保存。'
            if result.get('property_id'):text+='房源编号：'+result['property_id']+'。'
            if result.get('index_pending'):text+='小区资料已保留，检索索引尚未完成，请点击“重试资料索引”。'
            elif result.get('has_material'):text+='小区资料已入库并建立检索索引。'
            self.append('assistant',text);self.status.set(text)
        self.app.bridge.submit(self.service.confirm(eid,revision,approved),finish,self.handle_error)
    def retry_index(self):
        if self.busy:return
        self.set_busy(True);self.app.set_busy(True,'正在补建资料索引…')
        def done(count):
            self.app.set_busy(False)
            self.app.refresh_catalog_views()
            if self.active():self.set_busy(False);self.status.set(f'索引补建完成，仍待处理 {count} 份资料。')
        self.app.bridge.submit(self.service.retry_index(),done,self.handle_error)
