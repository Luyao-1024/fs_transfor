"""本地删除对话框: 移入回收站 / 直接删除 / 取消 三条路径.

运行: .venv/bin/python tests/ui_delete_perm.py
"""
import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile as _tf

os.environ["FSTRANSFOR_CONFIG_HOME"] = _tf.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import GLib

from fsapp.application import Application

FAILURES = []
TRASH_FILES = os.path.expanduser("~/.local/share/Trash/files")
TRASH_INFO = os.path.expanduser("~/.local/share/Trash/info")


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def walk(w, out=None):
    out = out if out is not None else []
    out.append(w)
    c = w.get_first_child()
    while c:
        walk(c, out)
        c = c.get_next_sibling()
    return out


def dialogs_in(win):
    from gi.repository import Adw
    return [w for w in walk(win) if isinstance(w, Adw.AlertDialog)]


def find_new_dialog(win, before):
    for d in dialogs_in(win):
        if d not in before:
            return d
    return None


def select_item(pane, name):
    for i in range(pane.sort_model.get_n_items()):
        if pane.sort_model.get_item(i).entry.name == name:
            pane.selection.select_item(i, True)
            return True
    return False


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None}

    def start(a):
        state["win"] = a.props.active_window
        lroot = os.path.join(os.path.expanduser("~"),
                             f"fstransfer-perm-{int(time.time() * 1000)}")
        os.makedirs(lroot)
        state["lroot"] = lroot
        for name in ("perm-victim.txt", "trash-victim.txt", "cancel-victim.txt"):
            with open(f"{lroot}/{name}", "wb") as f:
                f.write(b"x")
        state["win"].right.navigate(lroot)
        GLib.timeout_add(700, step_permanent)
        return GLib.SOURCE_REMOVE

    def wait_pane(step_next, tries=0):
        pane = state["win"].right
        if pane.cwd == state["lroot"] and pane.store.get_n_items() == 3:
            step_next()
            return GLib.SOURCE_REMOVE
        if tries > 50:
            check(False, "等待面板加载超时")
            finish()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(200, wait_pane, step_next, tries + 1)
        return GLib.SOURCE_REMOVE

    def do_delete(name, response, step_next):
        """选中 name → 触发删除 → 点击 response 按钮 → 回调 step_next."""
        win = state["win"]
        pane = win.right
        if not select_item(pane, name):
            check(False, f"选中 {name}")
            finish()
            return GLib.SOURCE_REMOVE
        before = dialogs_in(win)
        pane.activate_action(f"{pane.pane_id}.delete", None)
        dlg = find_new_dialog(win, before)
        if dlg is None:
            check(False, f"{name}: 删除对话框弹出")
            finish()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(250, lambda: (dlg.emit("response", response),
                                       GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1000, step_next)
        return GLib.SOURCE_REMOVE

    def step_permanent():
        wait_pane(lambda: do_delete("perm-victim.txt", "permanent", step_verify_perm))
        return GLib.SOURCE_REMOVE

    def step_verify_perm():
        p = f"{state['lroot']}/perm-victim.txt"
        check(not os.path.exists(p), "直接删除: 文件已从磁盘删除")
        in_trash = [n for n in (os.listdir(TRASH_FILES) if os.path.isdir(TRASH_FILES) else [])
                    if n == "perm-victim.txt"]
        check(not in_trash, "直接删除: 未进入回收站")
        do_delete("trash-victim.txt", "delete", step_verify_trash)
        return GLib.SOURCE_REMOVE

    def step_verify_trash():
        check(not os.path.exists(f"{state['lroot']}/trash-victim.txt"),
              "移入回收站: 原位置已移除")
        in_trash = os.path.exists(os.path.join(TRASH_FILES, "trash-victim.txt"))
        check(in_trash, "移入回收站: 已进入回收站")
        # 清理本次回收站条目(唯一命名)
        try:
            os.remove(os.path.join(TRASH_FILES, "trash-victim.txt"))
            os.remove(os.path.join(TRASH_INFO, "trash-victim.txt.trashinfo"))
        except OSError:
            pass
        do_delete("cancel-victim.txt", "cancel", step_verify_cancel)
        return GLib.SOURCE_REMOVE

    def step_verify_cancel():
        check(os.path.exists(f"{state['lroot']}/cancel-victim.txt"),
              "取消: 文件保留")
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        import shutil
        shutil.rmtree(state.get("lroot") or "/nonexistent", ignore_errors=True)
        print("\n" + ("直接删除测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    app.run([])
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
