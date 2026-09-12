"""Adw.Application 入口."""
from gi.repository import Adw, Gio

from .window import MainWindow


class Application(Adw.Application):
    def __init__(self, application_id: str = "io.github.fstransfer.FsTransfor"):
        # 测试时传入独立 ID, 避免触发单实例转发(激活已运行的正式实例)
        super().__init__(application_id=application_id)

    def do_startup(self):
        Adw.Application.do_startup(self)
        if self.lookup_action("quit") is None:
            action = Gio.SimpleAction.new("quit", None)
            action.connect("activate", self._request_quit)
            self.add_action(action)

    def _request_quit(self, *args):
        win = self.props.active_window
        if win is not None:
            win.request_exit()
        else:
            self.quit()

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = MainWindow(self)
        win.present()
        win.restore_session()
