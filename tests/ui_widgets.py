"""控件级测试: 连接弹出菜单 / 各类对话框构造 / 大文件实时速度 / 传输面板.

运行: .venv/bin/python tests/ui_widgets.py   (窗口会在屏幕上短暂出现)
"""
import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
# 隔离配置目录: 不读/不写用户的真实会话与服务器配置
os.environ["FSTRANSFOR_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import Gdk, GLib, GObject, Gtk

from fsapp.application import Application
from fsapp.connect_dialog import (AuthDialog, ConnectDialog, HostKeyDialog,
                                  TextPromptDialog, ask_delete, ask_overwrite)
from fsapp.transfer import Transfer

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None}

    def start(a):
        state["win"] = a.props.active_window
        # 隔离: 读取系统剪贴板原文本, 供写入测试后恢复(不阻塞主流程;
        # 读取失败或非文本时跳过恢复, 此时无法还原原剪贴板).
        clip = state["win"].left.get_display().get_clipboard()
        state["clip_text"] = None

        def read_done(cb, result):
            try:
                state["clip_text"] = cb.read_text_finish(result)
            except Exception:
                pass

        try:
            clip.read_text_async(read_done)
        except Exception:
            pass
        GLib.timeout_add(900, step_popover)
        return GLib.SOURCE_REMOVE

    def step_popover():
        win = state["win"]
        # 连接弹出菜单(真实 ~/.ssh/config 解析路径)
        win.left._rebuild_connect_popover()
        popover = win.left.connect_btn.get_popover()
        child = popover.get_child()
        check(child is not None, "连接弹出菜单已构建")
        GLib.timeout_add(100, step_payload)
        return GLib.SOURCE_REMOVE

    def step_payload():
        win = state["win"]
        # 拖拽 payload + 图标查找
        try:
            value = GObject.Value()
            value.init(str)
            value.set_string('{"pane":"left"}')
            cp = Gdk.ContentProvider.new_for_value(value)
            check(cp.ref_formats().match(
                      win.right._internal_drop_target.get_formats()),
                  "拖拽 ContentProvider 与目标格式匹配")
        except Exception as e:
            check(False, f"ContentProvider 构造失败: {e}")
        try:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            paint = theme.lookup_icon("folder", None, 40, 1,
                                      Gtk.TextDirection.LTR, 0)
            check(paint is not None, "拖拽图标 lookup_icon 成功")
        except Exception as e:
            check(False, f"lookup_icon 失败: {e}")
        # 剪贴板(写入后立即恢复原内容; 原内容为非文本时无法恢复, 记录限制)
        try:
            v = GObject.Value()
            v.init(str)
            v.set_string("/tmp/x")
            win.left.get_display().get_clipboard().set(v)
            check(True, "剪贴板写入成功")
        except Exception as e:
            check(False, f"剪贴板失败: {e}")
        finally:
            orig = state.get("clip_text")
            if orig is not None:
                try:
                    rv = GObject.Value()
                    rv.init(str)
                    rv.set_string(orig)
                    win.left.get_display().get_clipboard().set(rv)
                except Exception:
                    pass
        step_dialogs()
        return GLib.SOURCE_REMOVE

    def step_dialogs():
        win = state["win"]
        # Adw.Dialog 同一时间只能 present 一个: 串行逐个验证构造
        chain = [
            lambda: ConnectDialog(win, lambda cfg, creds: check(cfg is None, "ConnectDialog 取消回调")),
            lambda: AuthDialog(win, lambda r: check(r is None, "AuthDialog 取消回调"),
                               "both", "测试消息"),
            lambda: HostKeyDialog(win, lambda ok: check(ok is False, "HostKeyDialog 取消回调"),
                                  "example.com", "ed25519", "aa:bb:cc"),
            lambda: TextPromptDialog(win, lambda t: check(t is None, "TextPromptDialog 取消回调"),
                                     "新建文件夹"),
            lambda: ask_overwrite(win, ["a.txt"], lambda r: check(r is None, "OverwriteDialog 关闭=取消")),
            lambda: ask_delete(win, ["a.txt"], lambda ok: check(ok is False, "DeleteDialog 关闭=取消")),
        ]

        def run_next(dlg=None):
            if dlg is not None:
                try:
                    dlg.close()
                except Exception:
                    pass
            if chain:
                d = chain.pop(0)()
                GLib.timeout_add(450, run_next, d)
            else:
                GLib.timeout_add(200, step_big_transfer)
            return GLib.SOURCE_REMOVE

        run_next()
        return GLib.SOURCE_REMOVE

    def step_big_transfer():
        win = state["win"]
        tmpdir = tempfile.mkdtemp(prefix="fstransfer-ui2-")
        src = os.path.join(tmpdir, "big.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(300 * 1024 * 1024))  # 300 MB
        win.manager.enqueue(win.local_backend, [src], win.local_backend,
                            os.path.join(tmpdir, "out"))
        state["observed_speed"] = False
        state["observed_row"] = False
        state["max_panel_height"] = 0
        GLib.timeout_add(120, step_poll_big, tmpdir)
        return GLib.SOURCE_REMOVE

    def step_poll_big(tmpdir):
        win = state["win"]
        ts = win.manager.transfers
        running = any(t.running for t in ts)
        if running:
            if win.transfer_panel.get_reveal_child():
                state["observed_row"] = True
                state["max_panel_height"] = max(
                    state["max_panel_height"], win.transfer_panel.get_height())
            for t in ts:
                if t.status == "running" and t.speed > 0:
                    state["observed_speed"] = True
            return GLib.SOURCE_CONTINUE
        check(state["observed_row"], "传输面板在传输期间展开")
        check(state["observed_speed"], "传输期间速度计算为正")
        t = ts[-1]
        check(t.status == "done" and t.total_bytes == 300 * 1024 * 1024,
              f"300MB 传输完成({t.status}, {t.total_bytes})")
        check(len(win.transfer_panel._rows) >= 1, "传输行控件已创建")
        check(state["max_panel_height"] <= 100,
              f"单任务传输面板保持紧凑({state['max_panel_height']}px)")
        check(win.transfer_panel.get_parent() is win.content_overlay,
              "传输面板作为浮层挂载，不占主布局高度")
        check(win.transfer_panel.get_halign() == Gtk.Align.END and
              win.transfer_panel.get_valign() == Gtk.Align.END,
              "传输通知栈固定在内容区右下角")
        check(win.transfer_panel.get_width() <= 440 and
              win.transfer_panel.get_width() < win.content_overlay.get_width(),
              f"传输浮层不横跨整个窗口({win.transfer_panel.get_width()}px)")
        state["done_seen_at"] = GLib.get_monotonic_time()
        GLib.timeout_add(100, step_wait_autohide, tmpdir)
        return GLib.SOURCE_REMOVE

    def step_wait_autohide(tmpdir):
        win = state["win"]
        elapsed = (GLib.get_monotonic_time() - state["done_seen_at"]) / 1_000_000
        if win.manager.transfers and elapsed < 4.0:
            return GLib.SOURCE_CONTINUE
        check(not win.manager.transfers,
              f"成功任务约 3 秒后自动移除({elapsed:.1f}s)")
        check(not win.transfer_panel._rows,
              "完成任务移除后不残留传输行")
        check(not win.transfer_panel.get_reveal_child(),
              "没有待显示任务时传输面板自动收起")
        GLib.idle_add(step_notification_limit, tmpdir)
        return GLib.SOURCE_REMOVE

    def step_notification_limit(tmpdir):
        win = state["win"]
        backend = win.local_backend
        now = time.monotonic()
        notices = []
        for i in range(6):
            t = Transfer(backend, backend, [f"/tmp/notice-{i}.bin"],
                         "/tmp", "copy")
            if i == 0:
                t.status = "running"
            else:
                t.status = "done"
                t.finished_at = now
            win.manager.transfers.append(t)
            notices.append(t)
            win.transfer_panel.tick()

        visible = win.transfer_panel._visible_order
        check(len(visible) == 5 and visible == [t.id for t in notices[1:]],
              "第 6 条通知加入时最上方旧通知立即消失")
        check(notices[0] in win.manager.transfers and notices[0].running and
              not notices[0].cancel_event.is_set(),
              "挤出运行中通知不会取消后台任务")

        for t in notices:
            t.status = "cancelled"
            t.finished_at = now
        win.manager.clear_finished()
        win.transfer_panel.tick()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        GLib.timeout_add(300, finish)
        return GLib.SOURCE_REMOVE

    def finish():
        print("\n" + ("控件测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
