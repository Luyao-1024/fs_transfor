"""退出选择、等待确认取消和等待任务完成的真实 GTK 路径。

同名确认走非阻塞回调: 任务 parked 在 _conflict_dialogs 上, 退出时必须以取消收尾。
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_ROOT = tempfile.TemporaryDirectory(prefix="fstransfor-shutdown-ui-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = TEST_ROOT.name

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from fsapp.application import Application
from fsapp.backend.local import LocalBackend
from fsapp.connections import _Entry


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("退出等待超时")


def respond(win, value):
    until(lambda: win._exit_dialog is not None)
    click_response(win._exit_dialog, {"return": "返回应用", "wait": "等待完成后退出",
                                      "cancel": "取消任务并退出"}[value])
    until(lambda: win._exit_dialog is None)


def click_response(dialog, label):
    def find(widget):
        if isinstance(widget, Gtk.Button) and widget.get_label() == label:
            return widget
        child = widget.get_first_child()
        while child is not None:
            found = find(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None
    button = find(dialog)
    check(button is not None, f"找到实际对话框按钮：{label}")
    button.emit("clicked")


def main():
    app = Application(application_id="io.github.fstransfer.ShutdownTest")
    app.register(None)
    app.activate()
    win = app.props.active_window
    root = Path(TEST_ROOT.name)
    gate = threading.Event()
    disconnect_gate = threading.Event()
    try:
        count = len(win.workspaces)
        app.activate()
        check(len(win.workspaces) == count, "重复激活不会重复恢复标签")
        source = root / "source.txt"
        source.write_text("new content")
        target = root / "out"
        target.mkdir()
        (target / source.name).write_text("old content")
        task = win.manager.enqueue(win.local_backend, [str(source)], win.local_backend, str(target))
        until(lambda: task.parked and task.phase == "waiting"
              and bool(win._conflict_dialogs))
        check(not [t for t in win.manager.transfers
                   if t.running and t is not task],
              "等待确认的任务已让出工作线程")
        app.lookup_action("quit").activate(None)
        respond(win, "return")
        check(not win._closed and task.running and win.manager.accepting, "返回应用不取消原任务")
        app.lookup_action("quit").activate(None)
        respond(win, "cancel")
        until(lambda: win._closed)
        check(task.status == "cancelled" and not win._conflict_dialogs
              and not win._dialog_waiters and not win._tick_id,
              "菜单退出取消覆盖等待，并释放任务、对话框和定时器")
        check((target / source.name).read_text() == "old content", "取消退出保留原目标内容")

        app.activate()
        win = app.props.active_window

        class Remote(LocalBackend):
            is_local = False
            host, port, username = "test.invalid", 22, "test"
            dead = False

            def open_read(self, path):
                if not disconnect_gate.wait(8):
                    raise TimeoutError("disconnect test gate")
                return super().open_read(path)

            def disconnect(self):
                self.dead = True

        remote = Remote()
        cfg = {"host": remote.host, "port": remote.port, "username": remote.username}
        entry = _Entry(remote, cfg)
        entry.users.add(win.left)
        win.hub._live[win.hub.key(cfg)] = entry
        win.left.backend, win.left.server_cfg = remote, cfg
        win.left.cwd = str(root)
        transfer = win.manager.enqueue(remote, [str(source)], win.local_backend, str(root / "disconnect"))
        until(lambda: transfer.status == "running")
        win.disconnect_connection(remote)
        dialog = win.get_visible_dialog()
        click_response(dialog, "取消相关任务并断开")
        until(lambda: entry.disconnecting)
        check(transfer.cancel_event.is_set() and not remote.dead,
              "主动断开先取消相关任务，连接保留到清理完成")
        disconnect_gate.set()
        until(lambda: remote.dead)
        check(transfer.status == "cancelled" and win.left.suspended,
              "相关任务清理后断开连接并通知面板")
        win.left.connect_local(str(root))
        until(lambda: win.get_visible_dialog() is None)

        class SlowLocal(LocalBackend):
            def open_read(self, path):
                if not gate.wait(8):
                    raise TimeoutError("test gate")
                return super().open_read(path)

        task = win.manager.enqueue(SlowLocal(), [str(source)], win.local_backend, str(root / "wait"))
        until(lambda: task.status == "running")
        win.close()
        respond(win, "wait")
        check(not win._closed and not win.manager.accepting and task.running,
              "等待退出保留正在执行的任务并停止接收新任务")
        gate.set()
        until(lambda: win._closed)
        check(task.status == "done" and (root / "wait" / source.name).read_text() == "new content",
              "任务完成并保存结果后自动退出")
    finally:
        gate.set()
        disconnect_gate.set()
        win.manager.stop_accepting(cancel=True)
        until(lambda: not win.manager.busy)
        if not win._closed:
            win._finish_close()
            win.destroy()
        app.quit()
        TEST_ROOT.cleanup()
    print("shutdown UI: all passed")


if __name__ == "__main__":
    main()
