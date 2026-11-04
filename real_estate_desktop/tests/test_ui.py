from real_estate_desktop.test_support import ScriptedModel
import gc,tempfile,time,tkinter as tk,unittest
from pathlib import Path
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.config import Settings
from real_estate_desktop.app import PropertyApp
from real_estate_desktop.widgets import RoundedButton

class UI(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp=tempfile.TemporaryDirectory();self.root=tk.Tk();self.root.withdraw();self.r=Runtime(Settings(Path(self.temp.name)),model=ScriptedModel());self.app=PropertyApp(self.root,self.r)
    def tearDown(self):
        self.app.close();self.app=None;self.root=None;self.r=None
        # Tcl interpreters and their cyclic widget references must be collected on the UI thread.
        gc.collect();self.temp.cleanup()
    def settle(self,timeout=6):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            self.root.update()
            if not self.app.busy:return
            time.sleep(.02)
        self.fail('UI operation timed out')
    def bind(self):
        c=self.r.create_customer('界面测试客户');self.app.bind_customer(c['customer_id'],True);self.root.update();return c
    def test_landing_requires_customer(self):
        self.assertIsNone(self.app.cid);self.assertEqual(self.app.send_button.cget('state'),'normal');self.assertIn('房产经纪',self.root.title());self.assertNotIn('实验',self.app.chat.get('1.0','end'))
    def test_main_chat_can_intake_without_a_customer_and_confirm(self):
        from real_estate_desktop.tests.test_catalog import LISTING
        self.app.input.insert('1.0',LISTING);self.app.send();self.settle();view=self.app.catalog_window
        self.assertIsNone(self.app.cid);self.assertEqual(view.row['status'],'pending');self.assertEqual(len(self.r.business.properties()),200)
        view.save.invoke();self.settle();self.assertEqual(len(self.r.business.properties()),201)
    def test_intake_controls_fit_small_window(self):
        self.root.deiconify();self.app.open_catalog();view=self.app.catalog_window;view.win.geometry('850x710');self.root.update()
        for widget in (view.save,view.send_button,view.cancel,view.reparse):
            self.assertGreater(widget.winfo_width(),25)
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),view.win.winfo_rootx()+view.win.winfo_width()+2)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),view.win.winfo_rooty()+view.win.winfo_height()+2)
    def test_customer_switch_never_reuses_previous_conversation(self):
        a=self.bind();sid=self.app.sid;b=self.r.create_customer('客户乙');self.app.bind_customer(b['customer_id'],True)
        self.assertNotEqual(self.app.sid,sid);self.assertEqual(self.r.history(b['customer_id'],self.app.sid),[])
    def test_chat_and_approval_buttons(self):
        self.bind();self.app.fill_prompt('预算350万，三室，不要一楼');self.app.send();self.settle();self.assertIsNotNone(self.app._draft);self.assertEqual(self.r.records(self.app.cid),[])
        row=self.r.pending(self.app.cid)[0];self.app.confirm_view(row);self.root.update();win=self.app._dialogs[-1]
        def all_widgets(w):
            for child in w.winfo_children():yield child;yield from all_widgets(child)
        save=next(w for w in all_widgets(win) if isinstance(w,RoundedButton) and w.cget('text')=='确认并保存');save.invoke();self.settle();self.assertEqual(len(self.r.records(self.app.cid)),1);self.assertIn('已保存',self.app.chat.get('1.0','end'))
    def test_data_views_are_populated(self):
        self.bind();self.app.property_library();self.app.knowledge_library();self.app.customer_profile();self.app.records_view();self.root.update()
        self.assertEqual(len(self.app._dialogs),4)
    def test_primary_controls_inside_small_window(self):
        self.root.deiconify();self.root.geometry('1040x740');self.bind();self.root.update()
        for widget in (self.app.send_button,self.app.switch_button,self.app.pending_button,self.app.input):
            self.assertGreater(widget.winfo_width(),30);self.assertGreater(widget.winfo_height(),15)
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width()+2)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height()+2)

if __name__=='__main__':unittest.main()

