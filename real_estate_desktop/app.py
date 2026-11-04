from __future__ import annotations
import asyncio,csv,json,logging,re,sys
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from .runtime import Runtime
from .widgets import COLORS as C,RoundedFrame,RoundedButton,AsyncLoopBridge,markdown_segments
from .workflow import preference_text,money,typed_memory_text,constraint_value
from .domain import Property,customer_code
from .catalog import looks_like_intake

FONT='Microsoft YaHei UI'

def label(parent,text,size=10,bold=False,fg=None,**kw):
    return tk.Label(parent,text=text,font=(FONT,size,'bold' if bold else 'normal'),bg=parent.cget('bg'),fg=fg or C['text'],**kw)

def button(parent,text,command,primary=False,**kw):
    return RoundedButton(parent,text=text,command=command,bg=C['accent'] if primary else C['soft'],fg='white' if primary else C['accent'],activebackground=C['accent_hover'] if primary else C['nav_hover'],activeforeground='white',font=(FONT,10,'bold'),pady=9,**kw)

def preference_value(key,text):
    """Convert the edit box back to the field's type; the backend re-validates it."""
    text=(text or '').strip()
    if not text or text in ('不限','未设限制','null','None'):return None
    if key=='max_price_yuan':return constraint_value(key,text)
    if key in ('bedrooms','min_floor','max_floor','max_metro_distance_m'):return int(text)
    if key in ('min_area_sqm','max_area_sqm'):return float(text)
    if key in ('require_elevator','prefer_near_metro','education_priority'):return text in ('1','true','True','是','有','需要')
    if key=='amenity_preferences':return [item.strip() for item in re.split(r'[,，、]',text) if item.strip()]
    return text

def house(parent,size=44,bg=None):
    canvas=tk.Canvas(parent,width=size,height=size,bg=bg or parent.cget('bg'),highlightthickness=0)
    s=size/48
    def points(values):return [n*s for n in values]
    canvas.create_oval(*points([1,1,47,47]),fill='#dcebe1',outline='')
    canvas.create_polygon(*points([10,24,24,12,38,24,34,28,24,19,14,28]),fill=C['accent'],outline='')
    canvas.create_polygon(*points([15,26,24,20,33,26,33,37,15,37]),fill=C['accent'],outline='')
    canvas.create_rectangle(*points([22,29,27,37]),fill='#dcebe1',outline='')
    return canvas

class PropertyApp:
    def __init__(self,root,runtime=None):
        self.root=root;self.runtime=runtime or Runtime();self.cid=None;self.sid=None;self.busy=False;self.closing=False;self._draft=None;self._dialogs=[]
        self.bridge=AsyncLoopBridge(root);root.title('房产经纪智能助手');root.geometry('1280x880');root.minsize(1040,740);root.configure(bg=C['page'])
        self.icon=None
        asset=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent.parent))/'assets/property-assistant.png'
        if asset.exists():self.icon=tk.PhotoImage(file=str(asset));root.iconphoto(True,self.icon)
        root.option_add('*Font',(FONT,10));root.option_add('*selectBackground',C['accent']);root.option_add('*selectForeground','white')
        style=ttk.Style(root);style.theme_use('clam');style.configure('Treeview',font=(FONT,10),rowheight=34,background='white',fieldbackground='white',foreground=C['text'],borderwidth=0)
        style.configure('Treeview.Heading',font=(FONT,10,'bold'),background=C['soft'],foreground=C['accent'],padding=9)
        style.map('Treeview',background=[('selected','#dcebe1')],foreground=[('selected',C['text'])]);style.configure('TCombobox',padding=7)
        root.columnconfigure(1,weight=1);root.rowconfigure(0,weight=1)
        self.build_sidebar();self.build_main();root.protocol('WM_DELETE_WINDOW',self.close)
        self.show_landing()

    def build_sidebar(self):
        nav=tk.Frame(self.root,bg=C['nav'],width=244);nav.grid(row=0,column=0,sticky='nsew');nav.grid_propagate(False);nav.columnconfigure(0,weight=1);nav.rowconfigure(7,weight=1)
        brand=tk.Frame(nav,bg=C['nav']);brand.grid(row=0,column=0,sticky='ew',padx=22,pady=(28,6));house(brand).pack(side='left',padx=(0,9))
        block=tk.Frame(brand,bg=C['nav']);block.pack(side='left');label(block,'栖居 · 经纪助手',13,True).pack(anchor='w');label(block,'PROPERTY ASSISTANT',8,fg=C['muted']).pack(anchor='w',pady=(4,0))
        label(nav,'让每一次推荐都有依据',9,fg=C['muted']).grid(row=1,column=0,sticky='w',padx=26,pady=(8,22))
        self.new_button=button(nav,'＋  新建对话',lambda:self.choose_customer(True),True);self.new_button.grid(row=2,column=0,sticky='ew',padx=20,pady=(0,20))
        links=tk.Frame(nav,bg=C['nav']);links.grid(row=3,column=0,sticky='ew',padx=20)
        for text,action in [('对话录入',self.open_catalog),('房源库',self.property_library),('客户档案',self.customer_profile),('推荐与跟进',self.records_view),('小区资料库',self.knowledge_library)]:
            b=button(links,text,action);b.configure(anchor='w');b.pack(fill='x',pady=3)
        label(nav,'当前客户的对话',9,True,fg=C['muted']).grid(row=4,column=0,sticky='w',padx=26,pady=(26,10))
        self.sessions=tk.Listbox(nav,bg=C['nav'],fg=C['text'],selectbackground='#dcebe1',selectforeground=C['accent'],highlightthickness=0,bd=0,font=(FONT,10),height=7,activestyle='none',exportselection=False)
        self.sessions.grid(row=5,column=0,sticky='ew',padx=20);self.sessions.bind('<<ListboxSelect>>',self.select_session);self.session_rows=[]
        foot=RoundedFrame(nav,fill='#e4ece6',padding=12,radius=18,border='#e4ece6');foot.grid(row=8,column=0,sticky='ew',padx=20,pady=22)
        label(foot.body,'本地工作区',10,True).pack(anchor='w');label(foot.body,'按经纪人和客户区分记录\n数据保存在当前电脑',9,fg=C['muted'],justify='left').pack(anchor='w',pady=(5,0))

    def build_main(self):
        main=tk.Frame(self.root,bg=C['page']);main.grid(row=0,column=1,sticky='nsew',padx=30,pady=25);main.columnconfigure(0,weight=1);main.rowconfigure(3,weight=1)
        top=tk.Frame(main,bg=C['page']);top.grid(row=0,column=0,sticky='ew');top.columnconfigure(0,weight=1)
        label(top,'CUSTOMER WORKSPACE',9,True,fg=C['accent']).grid(row=0,column=0,sticky='w')
        label(top,'房产经纪智能助手',23,True).grid(row=1,column=0,sticky='w',pady=(4,5))
        self.switch_button=button(top,'选择 / 新建客户',self.choose_customer);self.switch_button.grid(row=0,column=1,rowspan=2,sticky='e')
        self.customer_line=label(top,'开始前，先确定这次服务哪位客户。',10,fg=C['muted']);self.customer_line.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(2,0))
        summary=RoundedFrame(main,fill='#edf3ee',padding=12,border='#edf3ee');summary.grid(row=1,column=0,sticky='ew',pady=(17,12));summary.body.columnconfigure(0,weight=1)
        self.preference_line=label(summary.body,'偏好会随客户延续，新的对话也能继续使用。',9,fg=C['accent'],anchor='w',justify='left',wraplength=630);self.preference_line.grid(row=0,column=0,sticky='ew')
        self.pending_bar=tk.Frame(main,bg=C['page']);self.pending_bar.grid(row=2,column=0,sticky='ew',pady=(0,8));self.pending_bar.columnconfigure(0,weight=1)
        self.pending_label=label(self.pending_bar,'选择客户后开始',9,fg=C['muted']);self.pending_label.grid(row=0,column=0,sticky='w')
        self.pending_button=button(self.pending_bar,'查看待确认',self.pending_view);self.pending_button.grid(row=0,column=1,sticky='e');self.pending_button.configure(state='disabled')
        card=RoundedFrame(main,padding=16,radius=24);card.grid(row=3,column=0,sticky='nsew');card.body.columnconfigure(0,weight=1);card.body.rowconfigure(0,weight=1)
        self.chat=tk.Text(card.body,wrap='word',bg='white',fg=C['text'],bd=0,highlightthickness=0,padx=8,pady=12,font=(FONT,11),spacing1=5,spacing3=7,cursor='arrow',state='disabled')
        self.chat.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(card.body,command=self.chat.yview);scroll.grid(row=0,column=1,sticky='ns');self.chat.configure(yscrollcommand=scroll.set)
        for name,font,fg in [('heading',(FONT,13,'bold'),C['accent']),('strong',(FONT,11,'bold'),C['text']),('body',(FONT,11),C['text']),('inline_code',('Consolas',10),C['accent']),('code',('Consolas',10),C['text']),('role',(FONT,10,'bold'),C['accent']),('user',(FONT,11),C['text']),('muted',(FONT,10),C['muted'])]:self.chat.tag_configure(name,font=font,foreground=fg,lmargin1=8,lmargin2=8)
        self.chat.tag_configure('role',spacing1=22,spacing3=8);self.chat.tag_configure('heading',spacing1=14,spacing3=8)
        composer=RoundedFrame(main,padding=12,radius=22);composer.grid(row=5,column=0,sticky='ew',pady=(12,0));composer.body.columnconfigure(0,weight=1)
        self.input=tk.Text(composer.body,height=3,wrap='word',font=(FONT,11),bg='white',fg=C['text'],insertbackground=C['accent'],bd=0,highlightthickness=0,padx=6,pady=6);self.input.grid(row=0,column=0,sticky='ew');self.input.bind('<Control-Return>',lambda event:self.send() or 'break')
        self.send_button=button(composer.body,'发送需求  →',self.send,True);self.send_button.grid(row=0,column=1,sticky='se',padx=(12,0))
        self.status=tk.StringVar(value='就绪');label(main,'',9,textvariable=self.status,fg=C['muted'],anchor='w').grid(row=6,column=0,sticky='ew',pady=(9,0))
        main.bind('<Configure>',lambda e:self.preference_line.configure(wraplength=max(300,e.width-300)))

    def append(self,role,text):
        self.chat.configure(state='normal');self.chat.insert('end',{'user':'经纪人','assistant':'栖居助手','system':'工作区提示'}.get(role,role)+'\n','role')
        if role=='user':self.chat.insert('end',text+'\n','user')
        else:
            for part,tag in markdown_segments(text):self.chat.insert('end',part,tag)
        self.chat.insert('end','\n');self.chat.configure(state='disabled');self.chat.see('end')

    def clear_chat(self):self.chat.configure(state='normal');self.chat.delete('1.0','end');self.chat.configure(state='disabled')
    def show_landing(self):
        self.clear_chat();self.append('assistant','# 先认识客户，再推荐房源\n选择已有客户，或为新客户建立档案。每次对话只服务一位客户，偏好和跟进会留在对应档案中。\n\n你可以一起完成：\n- 按预算、户型、楼层筛选房源\n- 查阅候选小区的交通、配套与来源资料\n- 生成推荐报告，确认后保存并更新客户偏好');self.set_busy(False)
    def fill_prompt(self,text):
        if self.busy:return
        self.input.delete('1.0','end');self.input.insert('1.0',text);self.input.focus_set()
        if not self.cid:self.choose_customer()
    def set_busy(self,value,status=None):
        self.busy=value
        for widget in (self.new_button,self.switch_button):widget.configure(state='disabled' if value else 'normal')
        self.send_button.configure(state='disabled' if value else 'normal',text='正在处理…' if value else '发送需求  →')
        self.input.configure(state='disabled' if value else 'normal')
        self.pending_button.configure(state='disabled' if value or not self.cid else 'normal')
        self.status.set(status or ('处理中，客户与会话已锁定…' if value else 'Ctrl + Enter 发送 · 推荐和偏好经确认后保存'))
    def fail(self,error):
        self.set_busy(False);msg=str(error) if isinstance(error,(ValueError,PermissionError)) else '这次处理未完成，请稍后重试。已有草稿和已保存记录可继续查看。'
        logging.getLogger(__name__).error('operation_failed %s',type(error).__name__);messagebox.showerror('暂未完成',msg,parent=self.root);self.refresh_pending()

    def dialog(self,title,size='850x620'):
        win=tk.Toplevel(self.root);win.title(title+' · 房产经纪智能助手');win.geometry(size);win.minsize(660,480);win.configure(bg=C['page']);win.transient(self.root);win.columnconfigure(0,weight=1);win.rowconfigure(1,weight=1)
        if self.icon:win.iconphoto(True,self.icon)
        label(win,title,19,True).grid(row=0,column=0,sticky='w',padx=24,pady=(20,14));self._dialogs.append(win);return win
    def text_view(self,title,content):
        win=self.dialog(title);card=RoundedFrame(win,padding=14);card.grid(row=1,column=0,sticky='nsew',padx=24,pady=(0,20));card.body.rowconfigure(0,weight=1);card.body.columnconfigure(0,weight=1)
        text=tk.Text(card.body,wrap='word',font=(FONT,11),bg='white',fg=C['text'],bd=0,highlightthickness=0,padx=8,pady=8);text.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(card.body,command=text.yview);scroll.grid(row=0,column=1,sticky='ns');text.configure(yscrollcommand=scroll.set)
        text.tag_configure('heading',font=(FONT,13,'bold'),foreground=C['accent'],spacing1=12);text.tag_configure('strong',font=(FONT,11,'bold'))
        for part,tag in markdown_segments(content):text.insert('end',part,tag)
        text.configure(state='disabled');return win
    def tree(self,parent,columns):
        frame=tk.Frame(parent,bg=C['page']);frame.grid(row=1,column=0,sticky='nsew',padx=24,pady=(0,12));frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show='headings',selectmode='browse');tree.grid(row=0,column=0,sticky='nsew')
        for key,title,width in columns:tree.heading(key,text=title);tree.column(key,width=width,minwidth=70,anchor='w')
        scroll=ttk.Scrollbar(frame,command=tree.yview);scroll.grid(row=0,column=1,sticky='ns');tree.configure(yscrollcommand=scroll.set);return tree
    def require_customer(self):
        if self.cid:return True
        self.choose_customer();return False

    def choose_customer(self,new_conversation=False):
        if self.busy:return
        win=self.dialog('这次服务哪位客户？','760x680');win.minsize(700,610)
        rows=self.runtime.customers();tree=self.tree(win,[('name','客户称呼',300),('id','客户编号',220)])
        for row in rows:tree.insert('','end',iid=row['customer_id'],values=(row['name'],customer_code(row['customer_id'])))
        if self.cid and tree.exists(self.cid):tree.selection_set(self.cid)
        footer=tk.Frame(win,bg=C['page']);footer.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,20));footer.columnconfigure(1,weight=1)
        label(footer,'或者新建客户',11,True).grid(row=0,column=0,columnspan=2,sticky='w',pady=(6,12))
        label(footer,'客户称呼').grid(row=1,column=0,sticky='w',padx=(0,14));name=ttk.Entry(footer);name.grid(row=1,column=1,sticky='ew',pady=4)
        label(footer,'备注（可选）').grid(row=2,column=0,sticky='w',padx=(0,14));notes=ttk.Entry(footer);notes.grid(row=2,column=1,sticky='ew',pady=4)
        def existing():
            chosen=tree.selection()
            if chosen:win.destroy();self.bind_customer(chosen[0],new_conversation)
            else:messagebox.showinfo('选择客户','请先点选一位已有客户。',parent=win)
        def create():
            try:c=self.runtime.create_customer(name.get(),notes.get())
            except ValueError as e:messagebox.showerror('请补充信息',str(e),parent=win);return
            win.destroy();self.bind_customer(c['customer_id'],True)
        controls=tk.Frame(footer,bg=C['page']);controls.grid(row=3,column=0,columnspan=2,sticky='e',pady=(18,0));button(controls,'新建客户并开始',create).pack(side='left',padx=8);button(controls,'使用选中的客户',existing,True).pack(side='left');tree.bind('<Double-1>',lambda e:existing())
        win.grab_set()
    def bind_customer(self,cid,new=False,sid=None):
        if self.busy:return
        customer=self.runtime.customer(cid);rows=self.runtime.sessions(cid);self.cid=cid;self.sid=sid or (self.runtime.new_session(cid) if new or not rows else rows[0]['session_id'])
        self.runtime.business.session(self.runtime.broker,cid,self.sid);self._draft=None
        self.customer_line.configure(text=f"当前客户：{customer['name']}  ·  {customer_code(cid)}")
        self.refresh_profile();self.refresh_sessions();self.clear_chat()
        history=self.runtime.history(cid,self.sid)
        if history:
            for item in history:self.append(item['role'],item['content'])
        else:self.append('assistant',f"# 为{customer['name']}开始服务\n请告诉我预算、区域、户型和楼层要求。已有长期偏好会自动带入，本次明确调整的条件优先。\n\n推荐先生成草稿，点击“查看待确认”核对后再保存。")
        self.refresh_pending();self.set_busy(False);self.input.focus_set()
        self.bridge.submit(self.runtime.apply_memory_jobs(cid),lambda result:self.refresh_profile() if self.cid==cid else None,lambda e:None)
    def refresh_profile(self):
        if self.cid:self.preference_line.configure(text=preference_text(self.runtime.profile(self.cid)))
    def refresh_sessions(self):
        self.sessions.delete(0,'end');self.session_rows=self.runtime.sessions(self.cid) if self.cid else []
        for i,row in enumerate(self.session_rows):
            self.sessions.insert('end',row['title'][:16])
            if row['session_id']==self.sid:self.sessions.selection_set(i)
    def select_session(self,event=None):
        selection=self.sessions.curselection()
        if selection and not self.busy:
            sid=self.session_rows[selection[0]]['session_id']
            if sid!=self.sid:self.bind_customer(self.cid,sid=sid)
    def refresh_pending(self):
        count=len(self.runtime.pending(self.cid)) if self.cid else 0
        self.pending_label.configure(text=f'当前客户有 {count} 份待确认草稿，未确认前不会写入推荐与长期偏好。' if count else '查询 → 检索 → 分析 → 经纪人确认 → 保存与更新偏好')
    def send(self):
        if self.busy:return
        text=self.input.get('1.0','end').strip()
        if not text:return
        if looks_like_intake(text):
            self.input.delete('1.0','end');self.open_catalog(text);return
        if not self.require_customer():return
        cid,sid=self.cid,self.sid
        self.input.delete('1.0','end');self.append('user',text);self.set_busy(True,'正在召回客户偏好，按需求检索小区与房源…')
        def done(result):
            self.set_busy(False)
            if (self.cid,self.sid)!=(cid,sid):return
            self.append('assistant',result['report']);self._draft=result.get('draft_id');self.refresh_sessions();self.refresh_pending()
            self.status.set('草稿已就绪，请通过“查看待确认”核对后保存。' if self._draft else '本次查询完成，未保存推荐或更新偏好。')
        self.bridge.submit(self.runtime.chat(text,cid,sid),done,self.fail)

    def open_catalog(self,text=None):
        if self.busy:return
        from .catalog_view import CatalogWindow
        view=getattr(self,'catalog_window',None)
        if view is None or not view.active():view=CatalogWindow(self);self.catalog_window=view
        view.win.deiconify();view.win.lift();view.input.focus_set()
        if text:
            if view.row['messages']:view.create()
            view.input.delete('1.0','end');view.input.insert('1.0',text);view.send()

    def pending_view(self):
        if self.busy or not self.require_customer():return
        cid=self.cid;rows=self.runtime.pending(cid);win=self.dialog('待确认草稿','860x580');tree=self.tree(win,[('time','生成时间',150),('kind','内容',350),('session','原对话',130)])
        byid={r['draft_id']:r for r in rows}
        for r in rows:
            p=json.loads(r['payload']);tree.insert('','end',iid=r['draft_id'],values=(datetime.fromtimestamp(r['created']).strftime('%m-%d %H:%M'),p['source_text'][:34],r['session_id'][:8]))
        controls=tk.Frame(win,bg=C['page']);controls.grid(row=2,column=0,sticky='e',padx=24,pady=(0,20))
        def open_draft():
            chosen=tree.selection()
            if not chosen:return
            row=byid[chosen[0]];win.destroy()
            if self.cid!=cid:return
            self.bind_customer(cid,sid=row['session_id']);self.confirm_view(row)
        button(controls,'核对选中的草稿',open_draft,True).pack();tree.bind('<Double-1>',lambda e:open_draft())
        if rows:tree.selection_set(rows[-1]['draft_id'])
    def confirm_view(self,row):
        cid,sid=self.cid,self.sid;payload=json.loads(row['payload']);win=self.text_view('核对后保存',payload['report']);win.geometry('920x750')
        footer=tk.Frame(win,bg=C['page']);footer.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,18));footer.columnconfigure(0,weight=1)
        label(footer,'保存后：记录进入客户档案；按上方提案更新客户背景与购房情景记忆。',9,fg=C['muted'],wraplength=600,justify='left').grid(row=0,column=0,columnspan=3,sticky='w',pady=(0,10))
        def commit(approved):
            if self.busy or (self.cid,self.sid)!=(cid,sid):return
            save.configure(state='disabled');cancel.configure(state='disabled');self.set_busy(True,'正在保存并同步客户记忆…')
            def done(result):
                if win.winfo_exists():win.destroy()
                self.set_busy(False);self.refresh_profile();self.refresh_pending()
                if result['status']=='saved':
                    suffix='客户记忆同步暂未完成，已保留待重试任务。' if result.get('memory_status')=='pending' else '部分记忆有歧义，未覆盖原值，请补充说明后重新提交。' if result.get('memory_status')=='needs_clarification' else '客户记忆已同步。'
                    self.append('system','推荐与跟进已保存。'+suffix);self.status.set('已保存到当前客户档案。')
                elif result['status']=='superseded':self.append('system','这份草稿已被新版本替代，未写入；请核对最新草稿。')
                elif result['status']=='undone':self.append('system','这次保存已被撤销，不能再次确认。')
                else:self.append('system','已取消这份草稿，未保存推荐或更新偏好。')
            self.bridge.submit(self.runtime.confirm(cid,sid,row['draft_id'],approved),done,self.fail)
        button(footer,'稍后处理',win.destroy).grid(row=1,column=0,sticky='w');cancel=button(footer,'取消这份草稿',lambda:commit(False));cancel.grid(row=1,column=1,padx=10);save=button(footer,'确认并保存',lambda:commit(True),True);save.grid(row=1,column=2)
        win.grab_set()

    def property_library(self):
        win=self.dialog('房源库','1100x640');byid={}
        tree=self.tree(win,[('id','房源ID',100),('community','小区 / 区域',280),('price','总价',100),('rooms','户型 / 面积',150),('floor','楼层',100),('metro','地铁距离',120)])
        def refresh():
            rows=self.runtime.business.properties();byid.clear();byid.update({r['property_id']:r for r in rows})
            for item in tree.get_children():tree.delete(item)
            for r in rows:tree.insert('','end',iid=r['property_id'],values=(r['property_id'],r['community_name']+' / '+r['region'],money(r['price_yuan']),f"{r['bedrooms']}室 / {r['area_sqm']:g}㎡",f"{r['floor']}/{r['total_floors']}",'未知' if r['metro_distance_m'] is None else str(r['metro_distance_m'])+'米'))
        refresh();win.bind('<<RefreshCatalog>>',lambda event:refresh())
        def detail():
            chosen=tree.selection()
            if chosen:
                p=byid[chosen[0]];self.text_view(p['community_name']+' · '+p['property_id'],f"# {p['community_name']}\n房源编号：{p['property_id']}\n区域：{p['region']}\n总价：{money(p['price_yuan'])}\n面积：{p['area_sqm']:g}㎡\n户型：{p['bedrooms']}室\n楼层：{p['floor']}/{p['total_floors']}层\n朝向：{p['orientation']}\n装修：{p['decoration']}\n房龄：{str(p['building_age'])+'年' if p['building_age'] is not None else '未知'}\n电梯：{'有' if p['has_elevator'] else '无'}\n地铁距离：{str(p['metro_distance_m'])+'米' if p['metro_distance_m'] is not None else '待核对'}\n\n## 房源描述\n{p['intro']}")
        def import_data():
            if self.busy:return
            path=filedialog.askopenfilename(parent=win,title='导入房源数据',filetypes=[('JSON或CSV','*.json *.csv')])
            if not path:return
            try:
                items=read_properties(Path(path))
                self.runtime.business.put_properties(items)
            except Exception as e:messagebox.showerror('导入未完成',str(e)[:700],parent=win);return
            win.destroy();self.property_library();self.status.set(f'已导入 {len(items)} 套房源。查询将使用用户录入数据。')
        controls=tk.Frame(win,bg=C['page']);controls.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,20));button(controls,'对话添加',self.open_catalog,True).pack(side='right',padx=(8,0));button(controls,'导入 JSON / CSV',import_data).pack(side='right');tree.bind('<Double-1>',lambda e:detail())

    def customer_profile(self):
        if not self.require_customer():return
        c=self.runtime.customer(self.cid);versions=self.runtime.preference_versions(self.cid)
        text=f"# {c['name']}\n客户编号：{customer_code(self.cid)}\n备注：{c['notes'] or '暂无'}\n\n## 当前已确认偏好\n{preference_text(self.runtime.profile(self.cid))}\n\n"+typed_memory_text(self.runtime.memory_records(self.cid))+'\n\n## 偏好版本与来源\n'
        for v in versions:text+=f"\n### {v['key']} · v{v['version']} · {'当前' if v['active'] else '历史'}\n值：{json.loads(v['value'])}\n来源：{v['source']}\n时间：{datetime.fromtimestamp(v['created']).strftime('%Y-%m-%d %H:%M')}\n"
        history=self.runtime.memory_records(self.cid,history=True)
        history={key:[record for record in records if record['status']!='active'] for key,records in history.items()}
        if any(history.values()):text+='\n\n## 长期记忆历史版本\n'+typed_memory_text(history,history=True)
        win=self.text_view('客户档案',text);cid=self.cid
        def retry():
            self.set_busy(True,'正在补同步已确认的客户偏好…')
            def done(result):self.set_busy(False);self.refresh_profile();messagebox.showinfo('同步结果',f"检查 {result['retried']} 项，仍待重试 {result['pending']} 项。",parent=win)
            self.bridge.submit(self.runtime.apply_memory_jobs(cid),done,self.fail)
        button(win,'管理客户记忆',self.memory_manager,True).grid(row=2,column=0,sticky='w',padx=24,pady=(0,16))
        button(win,'重试待同步偏好',retry).grid(row=2,column=0,sticky='e',padx=24,pady=(0,16))

    def memory_manager(self):
        """List, edit, delete or clear this customer's structured and typed memory."""
        if not self.require_customer():return
        cid=self.cid;win=self.dialog('客户记忆','1000x640');rows={}
        tree=self.tree(win,[('kind','类型',110),('content','内容',620),('status','状态',110)])
        def refresh():
            for item in tree.get_children():tree.delete(item)
            rows.clear()
            for version in self.runtime.preference_versions(cid):
                value=json.loads(version['value']);iid='pref:'+version['memory_id']
                rows[iid]={'kind':'pref','key':version['key'],'id':version['memory_id'],'value':value}
                tree.insert('','end',iid=iid,values=('结构化偏好',preference_text({version['key']:value}),'当前' if version['active'] else '历史'))
            memory=self.runtime.memory_records(cid,history=True)
            for kind,name in [('semantic','语义记忆'),('episodic','情景记忆')]:
                for record in memory.get(kind,[]):
                    if record.get('source',{}).get('conversation_id')=='legacy-preferences':continue
                    iid=kind+':'+record['id'];rows[iid]={'kind':kind,'id':record['id'],'record':record}
                    tree.insert('','end',iid=iid,values=(name,record['content'][:180],'当前' if record['status']=='active' else '历史'))
            if self.cid==cid:self.refresh_profile()
        def selected():
            chosen=tree.selection()
            if not chosen:messagebox.showinfo('选择记忆','请先点选一条记忆。',parent=win);return None
            return rows.get(chosen[0])
        def edit():
            row=selected()
            if not row:return
            if row['kind']!='pref':
                record=row['record'];form=self.dialog('修改语义/情景记忆','640x380')
                label(form,'类型').grid(row=0,column=0,sticky='w',padx=24,pady=(0,8));label(form,'语义记忆' if row['kind']=='semantic' else '情景记忆',11,True).grid(row=0,column=1,sticky='w',pady=(0,8))
                label(form,'内容').grid(row=1,column=0,sticky='nw',padx=24);content=ttk.Entry(form);content.grid(row=1,column=1,sticky='ew',padx=(0,24));content.insert(0,record.get('content') or '');form.columnconfigure(1,weight=1)
                label(form,'事实值').grid(row=2,column=0,sticky='w',padx=24,pady=(8,0));value_entry=ttk.Entry(form);value_entry.grid(row=2,column=1,sticky='ew',padx=(0,24),pady=(8,0));value_entry.insert(0,record.get('value') or '')
                def save():
                    try:self.runtime.update_memory_record(cid,row['id'],content=content.get(),value=value_entry.get())
                    except Exception as error:messagebox.showerror('未修改',str(error)[:400],parent=form);return
                    form.destroy();refresh()
                controls=tk.Frame(form,bg=C['page']);controls.grid(row=3,column=0,columnspan=2,sticky='e',padx=24,pady=(18,20));button(controls,'取消',form.destroy).pack(side='left',padx=8);button(controls,'保存修改',save,True).pack(side='left')
                form.grab_set();return
            value=row['value'];shown='不限' if value is None else '是' if value is True else '否' if value is False else '、'.join(value) if isinstance(value,list) else money(value) if row['key']=='max_price_yuan' else str(value)
            form=self.dialog('修改客户偏好','560x300');shown_key=preference_text({row['key']:row['value']}).split('：',1)[0]
            label(form,'字段').grid(row=0,column=0,sticky='w',padx=24,pady=(0,6));label(form,shown_key,11,True).grid(row=0,column=1,sticky='w',pady=(0,6))
            label(form,'新值').grid(row=1,column=0,sticky='w',padx=24);entry=ttk.Entry(form);entry.grid(row=1,column=1,sticky='ew',padx=(0,24));entry.insert(0,shown);form.columnconfigure(1,weight=1)
            def save():
                try:new=preference_value(row['key'],entry.get())
                except (ValueError,TypeError):messagebox.showerror('值不正确','请检查这个字段的格式后重试。',parent=form);return
                try:self.runtime.update_preference(cid,row['key'],new)
                except ValueError as error:messagebox.showerror('未修改',str(error),parent=form);return
                form.destroy();refresh()
            controls=tk.Frame(form,bg=C['page']);controls.grid(row=2,column=0,columnspan=2,sticky='e',padx=24,pady=(18,20));button(controls,'取消',form.destroy).pack(side='left',padx=8);button(controls,'保存修改',save,True).pack(side='left')
            form.grab_set()
        def delete():
            row=selected()
            if not row:return
            if not messagebox.askyesno('删除记忆','删除后这条记忆不再参与召回，确定删除吗？',parent=win):return
            if not self.runtime.delete_memory_record(cid,row['id']):messagebox.showinfo('未删除','这条记忆已不存在或已被删除。',parent=win)
            refresh()
        def clear():
            if not messagebox.askyesno('清除全部记忆',f'将删除这位客户的全部结构化偏好、语义记忆和情景记忆，且不可恢复。确定继续吗？',parent=win):return
            result=self.runtime.clear_memory(cid);refresh()
            self.status.set(f"已清除 {result['preferences']} 条偏好记录、{result['typed']} 条语义/情景记忆。")
        controls=tk.Frame(win,bg=C['page']);controls.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,18))
        button(controls,'修改选中',edit,True).pack(side='left');button(controls,'删除选中',delete).pack(side='left',padx=8)
        button(controls,'变更日志',lambda:self.memory_changes_view(cid)).pack(side='left',padx=8);button(controls,'全部清除',clear).pack(side='right')
        refresh();win.grab_set()

    def memory_changes_view(self,cid=None):
        cid=cid or self.cid
        if not cid:return
        rows=self.runtime.memory_changes(cid);win=self.dialog('记忆变更日志','1040x620')
        tree=self.tree(win,[('time','时间',145),('type','类型',90),('key','字段',150),('action','动作',80),('old','旧值',170),('new','新值',170),('source','来源/原文',200)])
        names={'ADD':'新增','UPDATE':'更新','NOOP':'未变','DELETE':'删除','UNDO':'撤销','CLEAR':'清空','PENDING':'待澄清'}
        types={'preference':'结构化偏好','semantic':'语义记忆','episodic':'情景记忆'}
        def shown(value):
            if value is None:return '—'
            try:parsed=json.loads(value)
            except (TypeError,ValueError):return str(value)[:80]
            if parsed is None:return '不限'
            if isinstance(parsed,list):return '、'.join(map(str,parsed))[:80]
            if isinstance(parsed,bool):return '是' if parsed else '否'
            return str(parsed)[:80]
        for row in rows:
            tree.insert('','end',values=(datetime.fromtimestamp(row['created']).strftime('%Y-%m-%d %H:%M'),
                types.get(row['memory_type'],row['memory_type']),row['key'],names.get(row['action'],row['action']),
                shown(row['old_value']),shown(row['new_value']),(row['source'] or '')[:80]))
        if not rows:tree.insert('','end',values=('暂无变更记录','','','','','',''))

    def records_view(self):
        if not self.require_customer():return
        cid=self.cid;rows=self.runtime.records(cid);followups=self.runtime.followups(cid);win=self.dialog('推荐与跟进');byid={r['record_id']:r for r in rows};tree=self.tree(win,[('time','保存时间',170),('kind','类型',140),('summary','原始需求',400)])
        for r in rows:
            p=json.loads(r['payload']);tree.insert('','end',iid=r['record_id'],values=(datetime.fromtimestamp(r['created']).strftime('%m-%d %H:%M'),{'recommend':'房源推荐','memory':'客户记忆','follow_up':'客户跟进'}.get(p['kind'],p['kind']),p['source_text'][:45]))
        def detail():
            chosen=tree.selection()
            if chosen:
                r=byid[chosen[0]];p=json.loads(r['payload']);content=p['report'].replace('状态：草稿，尚未确认保存。','状态：经纪人已确认保存。');content+='\n\n## 已保存跟进\n'+'\n'.join(f['content'] for f in followups if f['record_id']==r['record_id']);self.text_view('已保存记录',content)
        def undo():
            if not messagebox.askyesno('撤销最近写入',f'将撤销这位客户最近一次确认保存的推荐与偏好更新，且不可恢复。确定继续吗？',parent=win):return
            win.destroy();self.set_busy(True,'正在撤销最近一次保存…')
            def done(result):
                self.set_busy(False);self.refresh_profile();self.refresh_pending()
                self.append('system',f"已撤销最近一次保存：删除 1 条推荐与跟进，回滚 {result['keys']} 项偏好、{result.get('removed',0)+result.get('restored',0)} 条语义/情景记忆。")
                if self.cid==cid:self.records_view()
            self.bridge.submit(self.runtime.undo_last_write(cid),done,self.fail)
        controls=tk.Frame(win,bg=C['page']);controls.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,18))
        button(controls,'查看选中的记录',detail,True).pack(side='right');button(controls,'撤销最近写入',undo).pack(side='left')
        tree.bind('<Double-1>',lambda e:detail())

    def knowledge_library(self):
        win=self.dialog('小区与区域资料库','1000x660');rows=[];tree=self.tree(win,[('title','资料标题',380),('key','关联小区 / 区域',240),('date','资料日期',160)])
        def refresh():
            rows[:]=self.runtime.knowledge.documents()
            for item in tree.get_children():tree.delete(item)
            for i,r in enumerate(rows):tree.insert('','end',iid=str(i),values=(r['title'],r['community_key'],r['as_of']))
        refresh();win.bind('<<RefreshCatalog>>',lambda event:refresh())
        def detail():
            chosen=tree.selection()
            if chosen:
                r=rows[int(chosen[0])]
                with self.runtime.knowledge.connect() as con:chunks=con.execute('SELECT page,text FROM chunks WHERE path=? ORDER BY rowid',(r['path'],)).fetchall()
                self.text_view(r['title'],f"日期：{r['as_of']}\n关联：{r['community_key']}\n\n"+'\n\n'.join(f"第{c['page']}页\n{c['text']}" for c in chunks))
        controls=tk.Frame(win,bg=C['page']);controls.grid(row=2,column=0,sticky='ew',padx=24,pady=(0,18));button(controls,'对话添加',self.open_catalog,True).pack(side='right',padx=(8,0));button(controls,'导入文件',lambda:self.import_material(win)).pack(side='right');tree.bind('<Double-1>',lambda e:detail())
    def import_material(self,owner):
        if self.busy:return
        path=filedialog.askopenfilename(parent=owner,title='选择小区或区域资料',filetypes=[('文字资料','*.pdf *.txt *.md')])
        if not path:return
        properties=self.runtime.business.properties();mapping={f"小区 · {p['community_name']} [{p['community_key']}]":(p['community_key'],p['region']) for p in properties}
        mapping.update({f"区域 · {p['region']}":('region:'+p['region'],p['region']) for p in properties})
        if not mapping:messagebox.showinfo('先添加房源','请先导入房源，再关联其小区或区域资料。',parent=owner);return
        win=self.dialog('关联小区资料','760x520');form=tk.Frame(win,bg=C['page']);form.grid(row=1,column=0,sticky='nsew',padx=24,pady=12);form.columnconfigure(1,weight=1)
        label(form,Path(path).name,11,True).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,22));label(form,'关联小区 / 区域').grid(row=1,column=0,sticky='w',padx=(0,16));combo=ttk.Combobox(form,values=list(mapping),state='readonly');combo.grid(row=1,column=1,sticky='ew',pady=8);combo.current(0)
        label(form,'资料日期').grid(row=2,column=0,sticky='w');date=ttk.Entry(form);date.grid(row=2,column=1,sticky='ew',pady=8);date.insert(0,datetime.now().strftime('%Y-%m-%d'))
        def save():
            if self.busy:return
            key,region=mapping[combo.get()];self.set_busy(True,'正在提取并建立资料索引…')
            def done(result):
                self.set_busy(False);win.destroy()
                if owner.winfo_exists():owner.destroy()
                self.knowledge_library()
                if result['failed']:messagebox.showwarning('部分资料未完成',json.dumps(result['failed'],ensure_ascii=False),parent=self.root)
            self.bridge.submit(self.runtime.tools.sync(self.runtime.knowledge.import_document,path,key,region,'',date.get().strip()),done,self.fail)
        button(win,'导入并建立索引',save,True).grid(row=2,column=0,sticky='e',padx=24,pady=(0,20))
    def refresh_catalog_views(self):
        for win in self._dialogs:
            if win.winfo_exists():win.event_generate('<<RefreshCatalog>>',when='tail')
    def close(self):
        if self.closing:return
        self.closing=True;self.bridge.shutdown(self.runtime.close());self.root.destroy()

def read_properties(path):
    if path.suffix.lower()=='.json':
        data=json.loads(path.read_text('utf-8-sig'));data=data['properties'] if isinstance(data,dict) else data
    else:
        with path.open(encoding='utf-8-sig',newline='') as f:data=list(csv.DictReader(f))
        for row in data:
            for key in ('price_yuan','bedrooms','floor','total_floors','building_age','metro_distance_m'):
                if key in row:row[key]=int(row[key]) if row[key].strip() else None
            row['area_sqm']=float(row['area_sqm'])
            value=row.get('has_elevator','').lower()
            if value not in ('true','false','1','0','是','否'):raise ValueError('has_elevator应为true/false或1/0。')
            row['has_elevator']=value in ('true','1','是');row.pop('is_demo',None)
    if not isinstance(data,list) or not data or len(data)>10000:raise ValueError('请提供1至10000套房源的数组。')
    output=[];ids=set()
    for row in data:
        p=Property.model_validate({**row,'is_demo':False})
        if p.floor>p.total_floors:raise ValueError('房源楼层不能高于总楼层。')
        if p.property_id in ids:raise ValueError('导入文件内房源ID重复。')
        ids.add(p.property_id);output.append(p.model_dump())
    return output

def main():
    root=tk.Tk()
    try:PropertyApp(root)
    except Exception as e:
        messagebox.showerror('启动未完成',str(e) if isinstance(e,ValueError) else '本地数据初始化未完成，请检查数据目录是否可写。',parent=root);root.destroy();raise
    root.mainloop()
