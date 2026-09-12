"""集成测试: 模拟拖拽 drop 处理器 + SSH 连接失败流程.

运行: .venv/bin/python tests/ui_integration.py   (窗口会在屏幕上短暂出现)
"""
import json
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
# 隔离配置目录: 不读/不写用户的真实会话与服务器配置
os.environ["FSTRANSFOR_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from fsapp.application import Application

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def walk(widget, out=None):
    out = out if out is not None else []
    out.append(widget)
    child = widget.get_first_child()
    while child:
        walk(child, out)
        child = child.get_next_sibling()
    return out


def drag_source_for(pane, name):
    """查找绑定到指定文件可见单元格上的真实拖动源."""
    for widget in walk(pane.view):
        item = pane._cell_item(widget)
        if item is None or item.entry.name != name:
            continue
        controllers = widget.observe_controllers()
        for i in range(controllers.get_n_items()):
            controller = controllers.get_item(i)
            if isinstance(controller, Gtk.DragSource):
                return controller
    return None


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None, "tmpdir": None}

    def start(a):
        state["win"] = a.props.active_window
        GLib.timeout_add(900, step_drag)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_drag():
        win = state["win"]
        tmpdir = tempfile.mkdtemp(prefix="fstransfer-int-")
        state["tmpdir"] = tmpdir
        # 左面板导航到 tmpdir, 放一个文件
        src = os.path.join(tmpdir, "dragme.txt")
        with open(src, "wb") as f:
            f.write(b"drag-payload" * 1000)
        win.left.navigate(tmpdir)
        GLib.timeout_add(700, step_prepare)
        return GLib.SOURCE_REMOVE

    def step_prepare():
        win = state["win"]
        if win.left.store.get_n_items() == 0:
            check(False, "左面板应列出测试文件")
            return GLib.SOURCE_REMOVE
        # 从真实高亮行拖起，而不是绕过 ColumnView 直接调用外层处理器。
        ds = drag_source_for(win.left, "dragme.txt")
        check(ds is not None, "文件高亮行已绑定左键拖动源")
        if ds is None:
            finish()
            return GLib.SOURCE_REMOVE
        row = ds.get_widget()
        check(row.get_css_name() == "row",
              "拖动源直接绑定到视觉高亮行")
        check(row.get_allocation().width == row.get_parent().get_allocation().width,
              "整条高亮区域均属于拖动区域")
        win.left.selection.unselect_all()
        provider = win.left._on_cell_drag_prepare(ds, 5, 5)
        check(provider is not None, "左侧文件行可拖起")
        check(provider.ref_formats().match(
                  win.right._internal_drop_target.get_formats()),
              "左侧载荷格式可被右侧真实 DropTarget 接受")
        pid = win.left.pane_id
        check(pid in win._drag_payloads, f"拖拽载荷已暂存({pid})")
        paths = win._drag_payloads[pid][1]
        check(paths and paths[0].endswith("dragme.txt"), f"暂存路径正确({paths})")
        state["src_pane_id"] = pid

        # 右面板进入输出目录, 模拟内部 drop
        outdir = os.path.join(state["tmpdir"], "out")
        win.right.navigate(outdir)
        GLib.timeout_add(700, step_drop_internal)
        return GLib.SOURCE_REMOVE

    def step_drop_internal():
        win = state["win"]
        # 先替换冲突策略, 避免 worker 线程抢在替换前弹出真实确认框。
        # 非阻塞确认钩子优先级更高, 需要先停用才会走同步钩子。
        win.manager.ask_conflict_async = None
        win.manager.ask_overwrite = lambda names: "skip"
        value = json.dumps({"pane": state["src_pane_id"]})
        dt = win.right._internal_drop_target
        try:
            ok = win.right._on_drop_internal(dt, value, 0, 0)
            check(ok, "内部 drop 处理成功")
            check(len(win.manager.transfers) == 1, "已入队 1 条传输")
        except Exception as e:
            check(False, f"内部 drop 异常: {e}")
        # 同面板 drop: 应忽略且不入队
        win.left._on_drop_internal(dt, value, 0, 0)
        check(len(win.manager.transfers) == 1, "同面板 drop 被忽略")
        # 外部 FileList drop 到右面板
        fl = Gdk.FileList.new_from_list([Gio.File.new_for_path(
            os.path.join(state["tmpdir"], "dragme.txt"))])
        v2 = GObject.Value()
        v2.init(Gdk.FileList.__gtype__)
        v2.set_boxed(fl)
        dtf = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        try:
            ok = win.right._on_drop_files(dtf, v2, 0, 0)
            check(ok, "外部 FileList drop 处理成功")
            check(len(win.manager.transfers) == 2, "已入队第 2 条传输")
        except Exception as e:
            check(False, f"FileList drop 异常: {e}")
        GLib.timeout_add(400, step_wait_transfers)
        return GLib.SOURCE_REMOVE

    def step_wait_transfers():
        win = state["win"]
        if any(t.running for t in win.manager.transfers):
            return GLib.SOURCE_CONTINUE
        for t in win.manager.transfers:
            check(t.status == "done", f"传输完成({t.status}, err={t.error})")
        out = os.path.join(state["tmpdir"], "out", "dragme.txt")
        check(os.path.exists(out), "左→右拖拽文件已落到目标目录")

        # 再从右侧实际单元格拖回左侧，覆盖相反方向。
        backdir = os.path.join(state["tmpdir"], "back")
        os.makedirs(backdir)
        state["backdir"] = backdir
        state["reverse_deadline"] = GLib.get_monotonic_time() + 5_000_000
        win.left.navigate(backdir)
        GLib.timeout_add(700, step_prepare_reverse)
        return GLib.SOURCE_REMOVE

    def step_prepare_reverse():
        win = state["win"]
        ds = drag_source_for(win.right, "dragme.txt")
        if ds is None and GLib.get_monotonic_time() < state["reverse_deadline"]:
            return GLib.SOURCE_CONTINUE
        check(ds is not None, "右侧目标文件刷新后可从单元格拖起")
        if ds is None:
            finish()
            return GLib.SOURCE_REMOVE
        provider = win.right._on_cell_drag_prepare(ds, 5, 5)
        check(provider is not None, "右侧文件行可拖起")
        check(provider.ref_formats().match(
                  win.left._internal_drop_target.get_formats()),
              "右侧载荷格式可被左侧真实 DropTarget 接受")
        check(win.right.workspace.active_pane is win.right,
              "拖动时右侧成为活动面板")

        value = json.dumps({"pane": win.right.pane_id})
        target = win.left._internal_drop_target
        ok = win.left._on_drop_internal(target, value, 0, 0)
        check(ok, "右→左内部 drop 处理成功")
        GLib.timeout_add(300, step_wait_reverse)
        return GLib.SOURCE_REMOVE

    def step_wait_reverse():
        win = state["win"]
        if any(t.running for t in win.manager.transfers):
            return GLib.SOURCE_CONTINUE
        transfer = win.manager.transfers[-1]
        check(transfer.status == "done",
              f"右→左传输完成({transfer.status}, err={transfer.error})")
        back = os.path.join(state["backdir"], "dragme.txt")
        check(os.path.exists(back), "右→左拖拽文件已落到目标目录")
        step_ssh_fail()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_ssh_fail():
        win = state["win"]
        # 本机 sshd 未运行 → 连接应被拒绝并进入 disconnected 状态
        cfg = {"id": None, "name": "本机测试", "host": "127.0.0.1", "port": 22,
               "username": "nobody", "auth_method": "password", "key_path": None}
        win.left.connect_server_async(cfg, creds={"password": "wrong"})
        GLib.timeout_add(500, step_poll_ssh)
        return GLib.SOURCE_REMOVE

    def step_poll_ssh():
        win = state["win"]
        state_name = win.left.stack.get_visible_child_name()
        if state_name in ("busy", "files"):
            return GLib.SOURCE_CONTINUE
        check(state_name == "disconnected", f"连接失败后面板进入 disconnected({state_name})")
        check(win.left.disc_label.get_text().startswith("连接失败"),
              f"错误信息已显示({win.left.disc_label.get_text()[:40]}…)")
        GLib.timeout_add(300, finish)
        return GLib.SOURCE_REMOVE

    def finish():
        shutil.rmtree(state.get("tmpdir") or "/nonexistent", ignore_errors=True)
        print("\n" + ("集成测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
