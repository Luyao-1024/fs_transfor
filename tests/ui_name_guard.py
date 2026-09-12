"""新建/重命名的名称校验与覆盖策略: 面板动作 → 对话框 → 真实文件系统全链路。

覆盖: 拒绝 '../' 与 'a/b' 等会逃出当前目录的名称、同名新建必须报错、
重命名到已有名称要显式确认替换(默认取消), 取消后源与目标都不变。
运行: .venv/bin/python tests/ui_name_guard.py   (窗口会短暂出现)
"""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_ROOT = tempfile.TemporaryDirectory(prefix="fstransfor-name-guard-ui-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = TEST_ROOT.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from fsapp.application import Application  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


def until(predicate, timeout=10, what="UI 状态"):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待{what}超时")


def widgets(root, out=None):
    out = out if out is not None else []
    out.append(root)
    child = root.get_first_child()
    while child is not None:
        widgets(child, out)
        child = child.get_next_sibling()
    return out


def alert_named(win, needle):
    """按标题文本找到当前弹出的 Adw.AlertDialog."""
    return next((w for w in widgets(win)
                 if isinstance(w, Adw.AlertDialog) and needle in w.get_heading()),
                None)


def click(dialog, label):
    button = next((w for w in widgets(dialog)
                   if isinstance(w, Gtk.Button) and w.get_label() == label), None)
    check(button is not None, f"找到对话框按钮：{label}")
    button.emit("clicked")


def entry_named(pane, name):
    for index in range(pane.sort_model.get_n_items()):
        item = pane.sort_model.get_item(index)
        if item.entry.name == name:
            return item.entry
    raise AssertionError(f"列表中没有条目 {name}")


def main():
    app = Application(application_id="io.github.fstransfer.NameGuardTest")
    app.register(None)
    app.activate()
    win = app.props.active_window
    pane = win.left
    root = Path(TEST_ROOT.name)
    workspace = root / "workspace"
    outside = root / "outside"
    (workspace / "folder").mkdir(parents=True)
    outside.mkdir()
    (workspace / "taken.txt").write_text("已有内容", encoding="utf-8")
    victim = workspace / "victim.txt"
    victim.write_text("源内容", encoding="utf-8")

    errors = []
    original_toast = win.toast

    def record(message, error=False):
        if error:
            errors.append(message)
        return original_toast(message, error)

    win.toast = record
    pane.connect_local(str(workspace))
    until(lambda: pane.cwd == str(workspace), what="面板载入")

    try:
        # ---- 非法名称: 一律拒绝, 且不在文件系统上留下痕迹 ----
        snapshot = sorted(entry.name for entry in workspace.iterdir())
        for bad in ("../outside/planted", "nested/deep", "..", "./x", "a/../../b"):
            errors.clear()
            pane._do_mkdir(bad)
            until(lambda: bool(errors), what="错误提示")
            check(any("路径分隔符" in message or "不能是" in message for message in errors),
                  f"新建 {bad!r} 被拒绝：{errors}")
            check(sorted(entry.name for entry in workspace.iterdir()) == snapshot,
                  f"非法名称 {bad!r} 没有在当前目录创建任何东西")
            check(list(outside.iterdir()) == [], f"非法名称 {bad!r} 没有写到目录之外")
        check(not (root / "planted").exists(), "被拒绝的名称不产生越界文件")

        # ---- 合法名称正常创建 ----
        pane._do_mkdir("正常 新建")
        until(lambda: (workspace / "正常 新建").is_dir(), what="目录创建")
        check((workspace / "正常 新建").is_dir(), "合法名称正常创建目录")

        # ---- 同名新建必须明确失败, 不能静默当成成功 ----
        errors.clear()
        pane._do_mkdir("正常 新建")
        until(lambda: bool(errors), what="同名提示")
        check(any("同名项目已存在" in message for message in errors),
              f"同名新建被明确拒绝：{errors}")

        # ---- 重命名: 非法名称拒绝, 源文件不动 ----
        errors.clear()
        pane._do_rename(entry_named(pane, "victim.txt"), "../outside/moved")
        until(lambda: bool(errors), what="重命名错误提示")
        check(any("路径分隔符" in message for message in errors),
              f"重命名到上级目录被拒绝：{errors}")
        check(victim.is_file() and not (outside / "moved").exists(),
              "被拒绝的重命名没有移动文件")

        # ---- 重命名覆盖已有项目: 必须显式确认, 默认不替换 ----
        errors.clear()
        pane._do_rename(entry_named(pane, "victim.txt"), "taken.txt")
        until(lambda: alert_named(win, "已存在") is not None, what="替换确认框")
        dialog = alert_named(win, "已存在")
        check("taken.txt" in dialog.get_heading(),
              f"确认框点名将被覆盖的项目：{dialog.get_heading()}")
        click(dialog, "取消")
        until(lambda: alert_named(win, "已存在") is None, what="确认框关闭")
        until(lambda: victim.is_file(), what="源文件保留")
        check((workspace / "taken.txt").read_text(encoding="utf-8") == "已有内容"
              and victim.read_text(encoding="utf-8") == "源内容",
              "取消替换后源与目标内容都保持原样")

        pane._do_rename(entry_named(pane, "victim.txt"), "taken.txt")
        until(lambda: alert_named(win, "已存在") is not None, what="替换确认框")
        click(alert_named(win, "已存在"), "替换")
        until(lambda: not victim.exists(), what="替换完成")
        check((workspace / "taken.txt").read_text(encoding="utf-8") == "源内容",
              "确认替换后目标内容被源内容更新")
        check(not errors, f"替换路径没有额外报错({errors})")

        # ---- 改名成当前名字: 无操作, 不需要确认也不删除文件 ----
        pane._do_rename(entry_named(pane, "taken.txt"), "taken.txt")
        GLib.usleep(300_000)
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        check(alert_named(win, "已存在") is None
              and (workspace / "taken.txt").read_text(encoding="utf-8") == "源内容",
              "同名重命名不弹确认也不删除文件")
    finally:
        win.toast = original_toast
        win.manager.stop_accepting(cancel=True)
        until(lambda: not win.manager.busy, what="任务清理")
        win._finish_close()
        win.destroy()
        app.quit()
        TEST_ROOT.cleanup()
    print("name guard UI: all passed")


if __name__ == "__main__":
    main()
