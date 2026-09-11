"""远程删除完整链路测试: 动作激活 → 确认对话框 → 后端删除 → 列表刷新.

用 FakeRemote(本地目录伪装的远程后端)复现 UI 链路,
并通过 emit('response') 程序化点击对话框按钮.
运行: .venv/bin/python tests/ui_delete.py
"""
import os
import shutil
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile as _tf

os.environ["FSTRANSFOR_CONFIG_HOME"] = _tf.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import GLib

from fsapp import config
from fsapp.application import Application
from fsapp.backend.local import LocalBackend
from fsapp.connections import ConnectionHub, _Entry

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


class FakeRemote(LocalBackend):
    """本地目录伪装的远程后端: is_local=False, 文件操作真实生效."""

    is_local = False

    def __init__(self, root):
        super().__init__()
        self._root = root

    @property
    def label(self):
        return "fake@remote"

    def home(self):
        return self._root


FAKE_CFG = {"id": "fakeid", "name": "FakeSrv", "host": "hubtest.example",
            "port": 22, "username": "fake", "auth_method": "key", "key_path": None}


def walk(w, out=None):
    out = out if out is not None else []
    out.append(w)
    c = w.get_first_child()
    while c:
        walk(c, out)
        c = c.get_next_sibling()
    return out


def dialogs_in(win):
    """当前树中所有 Adw.AlertDialog(关闭的对话框可能仍留在树里)."""
    from gi.repository import Adw
    return [w for w in walk(win) if isinstance(w, Adw.AlertDialog)]


def find_new_dialog(win, before):
    """返回 before 集合之外新出现的对话框(即刚弹出的那个)."""
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


def click_menu_delete(pane, win):
    """弹出菜单并模拟鼠标点击'删除'时的列表激活信号.

    菜单为原生 Popover+ListBox(pane._popup_menu_at), 在目标面板菜单中找行。
    """
    pane._popup_menu_at(10, 10)
    from gi.repository import Gtk
    for x in walk(pane._menu_popover):
        if isinstance(x, Gtk.Label) and x.get_text() == "删除":
            row = x.get_ancestor(Gtk.ListBoxRow)
            if row is not None:
                pane._menu_popover.get_child().emit("row-activated", row)
                return True
    return False


def pane_names(pane):
    return [pane.sort_model.get_item(i).entry.name
            for i in range(pane.sort_model.get_n_items())]


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None, "root": None}

    def start(a):
        state["win"] = a.props.active_window
        root = tempfile.mkdtemp(prefix="fstransfer-del-")
        state["root"] = root
        with open(f"{root}/victim.txt", "wb") as f:
            f.write(b"delete-me")
        os.makedirs(f"{root}/victimdir")
        with open(f"{root}/victimdir/inner.txt", "wb") as f:
            f.write(b"x")
        with open(f"{root}/keeper.txt", "wb") as f:
            f.write(b"keep-me")
        fake = FakeRemote(root)
        win = state["win"]
        win.hub._live[ConnectionHub.key(FAKE_CFG)] = _Entry(fake, FAKE_CFG)
        win.left.connect_server_async(FAKE_CFG)
        GLib.timeout_add(800, step_delete_file)
        return GLib.SOURCE_REMOVE

    def step_delete_file():
        win = state["win"]
        root = state["root"]
        pane = win.left
        if pane.stack.get_visible_child_name() != "files" or pane.store.get_n_items() == 0:
            GLib.timeout_add(300, step_delete_file)
            return GLib.SOURCE_CONTINUE
        check(select_item(pane, "victim.txt"), "选中 victim.txt")
        # 触发删除动作(等价于右键菜单/快捷键激活)
        before = dialogs_in(win)
        pane.activate_action(f"{pane.pane_id}.delete", None)
        dlg = find_new_dialog(win, before)
        if dlg is None:
            check(False, "确认对话框已弹出")
            return GLib.SOURCE_REMOVE
        check(True, "确认对话框已弹出")
        GLib.timeout_add(300, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1200, step_verify_file)
        return GLib.SOURCE_REMOVE

    def step_verify_file():
        win = state["win"]
        root = state["root"]
        check(not os.path.exists(f"{root}/victim.txt"), "文件已从磁盘删除")
        names = pane_names(win.left)
        check("victim.txt" not in names, f"列表已刷新, 不再显示 victim.txt({names})")
        check("keeper.txt" in names, "其他文件不受影响")
        GLib.timeout_add(300, step_delete_dir)
        return GLib.SOURCE_REMOVE

    def step_delete_dir():
        win = state["win"]
        pane = win.left
        check(select_item(pane, "victimdir"), "选中 victimdir")
        before = dialogs_in(win)
        pane.activate_action(f"{pane.pane_id}.delete", None)
        dlg = find_new_dialog(win, before)
        if dlg is None:
            check(False, "目录删除对话框已弹出")
            return GLib.SOURCE_REMOVE
        check(True, "目录删除对话框已弹出")
        GLib.timeout_add(300, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1200, step_verify_dir)
        return GLib.SOURCE_REMOVE

    def step_verify_dir():
        win = state["win"]
        root = state["root"]
        check(not os.path.exists(f"{root}/victimdir"), "目录已递归删除")
        check("victimdir" not in pane_names(win.left), "列表已刷新")
        # 取消路径: 不应删除
        check(select_item(win.left, "keeper.txt"), "选中 keeper.txt")
        before = dialogs_in(win)
        win.left.activate_action(f"{win.left.pane_id}.delete", None)
        dlg = find_new_dialog(win, before)
        if dlg is not None:
            check(True, "取消路径对话框已弹出")
            GLib.timeout_add(300, lambda: (dlg.emit("response", "cancel"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1200, step_verify_cancel)
        return GLib.SOURCE_REMOVE

    def step_verify_cancel():
        win = state["win"]
        root = state["root"]
        check(os.path.exists(f"{root}/keeper.txt"), "取消删除: 文件保留")
        check(win.left.selection.get_selection().get_size() == 1,
              "取消删除后左侧暂时保留原选区")
        right_root = os.path.join(root, "right-pane")
        os.makedirs(right_root)
        with open(os.path.join(right_root, "right.txt"), "wb") as f:
            f.write(b"keep-right")
        state["right_root"] = right_root
        win.right.navigate(right_root)
        GLib.timeout_add(700, step_cross_pane_selection)
        return GLib.SOURCE_REMOVE

    def step_cross_pane_selection():
        win = state["win"]
        pane = win.right
        if pane.cwd != state["right_root"] or pane.store.get_n_items() == 0:
            GLib.timeout_add(300, step_cross_pane_selection)
            return GLib.SOURCE_CONTINUE
        check(select_item(pane, "right.txt"), "取消左侧删除后选中右侧文件")
        check(win.left.selection.get_selection().get_size() == 0,
              "选择右侧文件会清除左侧残留选区")
        check(pane.workspace.active_pane is pane,
              "Delete 等快捷键的目标切换到右侧面板")

        before = dialogs_in(win)
        win.left.activate_action(f"{win.left.pane_id}.delete", None)
        check(find_new_dialog(win, before) is None,
              "左侧已无选区，不会再提示删除左侧文件")

        before = dialogs_in(win)
        pane.workspace.activate_action(f"{pane.workspace.wid}.delete", None)
        dlg = find_new_dialog(win, before)
        check(dlg is not None, "右侧删除只提示当前右侧文件")
        if dlg is not None:
            dlg.emit("response", "cancel")
        GLib.timeout_add(200, step_menu_remote)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # 真实菜单点击路径(先远程后本地, 复现用户序列)
    # ------------------------------------------------------------------
    def step_menu_remote():
        win = state["win"]
        pane = win.left
        check(select_item(pane, "keeper.txt"), "菜单路径: 选中远程 keeper.txt")
        state["before"] = dialogs_in(win)
        ok = click_menu_delete(pane, win)
        check(ok, "菜单路径: 点击了'删除'菜单项")
        GLib.timeout_add(400, step_menu_remote_dlg)
        return GLib.SOURCE_REMOVE

    def step_menu_remote_dlg():
        win = state["win"]
        dlg = find_new_dialog(win, state["before"])
        check(dlg is not None, "菜单点击'删除'后确认对话框弹出")
        if dlg is None:
            finish()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(200, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1100, step_menu_remote_done)
        return GLib.SOURCE_REMOVE

    def step_menu_remote_done():
        root = state["root"]
        check(not os.path.exists(f"{root}/keeper.txt"), "菜单路径: 远程文件已删除")
        # 本地面板(家目录, 支持回收站): 单对话框路径
        win = state["win"]
        lroot = os.path.join(os.path.expanduser("~"),
                             f"fstransfer-del-local-{int(time.time() * 1000)}")
        os.makedirs(lroot)
        state["lroot"] = lroot
        # 唯一命名: 避免误动用户回收站中的同名文件, 清理时只删本次条目
        victim = f"homefile-{int(time.time() * 1000)}.txt"
        state["home_victim"] = victim
        with open(f"{lroot}/{victim}", "wb") as f:
            f.write(b"home-victim")
        win.right.navigate(lroot)
        GLib.timeout_add(700, step_menu_local)
        return GLib.SOURCE_REMOVE

    def step_menu_local():
        win = state["win"]
        pane = win.right
        if pane.cwd != state["lroot"] or pane.store.get_n_items() == 0:
            GLib.timeout_add(300, step_menu_local)
            return GLib.SOURCE_CONTINUE
        check(select_item(pane, state["home_victim"]),
              "菜单路径: 选中本地临时文件")
        state["before"] = dialogs_in(win)
        ok = click_menu_delete(pane, win)
        check(ok, "菜单路径: 本地面板点击'删除'菜单项")
        GLib.timeout_add(400, step_menu_local_dlg)
        return GLib.SOURCE_REMOVE

    def step_menu_local_dlg():
        win = state["win"]
        dlg = find_new_dialog(win, state["before"])
        check(dlg is not None, "远程删除后本地删除: 确认对话框弹出(用户报告的场景)")
        if dlg is None:
            finish()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(200, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(1100, step_menu_local_done)
        return GLib.SOURCE_REMOVE

    def step_menu_local_done():
        lroot = state["lroot"]
        victim = state["home_victim"]
        gone = not os.path.exists(f"{lroot}/{victim}")
        check(gone, "本地文件已删除(移入回收站)")
        # 仅清理本次创建的唯一命名回收站条目, 不按固定名猜测删除
        try:
            tf = os.path.expanduser(f"~/.local/share/Trash/files/{victim}")
            ti = os.path.expanduser(f"~/.local/share/Trash/info/{victim}.trashinfo")
            if os.path.exists(tf):
                os.remove(tf)
            if os.path.exists(ti):
                os.remove(ti)
        except OSError:
            pass
        shutil.rmtree(lroot, ignore_errors=True)
        # /tmp 路径: 回收站不支持 → 二级永久删除确认对话框
        win = state["win"]
        troot = tempfile.mkdtemp(prefix="fstransfer-del-tmp-")
        state["troot"] = troot
        with open(f"{troot}/tmpfile.txt", "wb") as f:
            f.write(b"tmp-victim")
        win.right.navigate(troot)
        GLib.timeout_add(700, step_menu_tmp)
        return GLib.SOURCE_REMOVE

    def step_menu_tmp():
        win = state["win"]
        pane = win.right
        if pane.cwd != state["troot"] or pane.store.get_n_items() == 0:
            GLib.timeout_add(300, step_menu_tmp)
            return GLib.SOURCE_CONTINUE
        select_item(pane, "tmpfile.txt")
        state["before"] = dialogs_in(win)
        click_menu_delete(pane, win)
        GLib.timeout_add(400, step_menu_tmp_dlg1)
        return GLib.SOURCE_REMOVE

    def step_menu_tmp_dlg1():
        win = state["win"]
        dlg = find_new_dialog(win, state["before"])
        check(dlg is not None, "/tmp 路径: 第一级对话框弹出")
        if dlg is None:
            finish()
            return GLib.SOURCE_REMOVE
        # 必须在确认第一级之前记录；二级对话框可能在下个检查前就已出现。
        state["before2"] = dialogs_in(win)
        state["tmp_dialog_deadline"] = time.monotonic() + 5
        GLib.timeout_add(200, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(600, step_menu_tmp_dlg2)
        return GLib.SOURCE_REMOVE

    def step_menu_tmp_dlg2():
        win = state["win"]
        dlg = find_new_dialog(win, state["before2"])
        if dlg is None:
            # 二级对话框尚未出现则继续等
            if os.path.exists(f"{state['troot']}/tmpfile.txt"):
                if time.monotonic() > state["tmp_dialog_deadline"]:
                    check(False, "/tmp 路径: 等待二级确认超时")
                    return step_finish_tmp()
                return GLib.SOURCE_CONTINUE
            check(True, "/tmp 路径: 文件已处理")
            return step_finish_tmp()
        check(True, "/tmp 路径: 二级永久删除确认弹出")
        GLib.timeout_add(200, lambda: (dlg.emit("response", "delete"), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(900, step_finish_tmp)
        return GLib.SOURCE_REMOVE

    def step_finish_tmp():
        troot = state["troot"]
        check(not os.path.exists(f"{troot}/tmpfile.txt"), "/tmp 文件已永久删除(回退确认后)")
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        shutil.rmtree(state.get("root") or "/nonexistent", ignore_errors=True)
        shutil.rmtree(state.get("troot") or "/nonexistent", ignore_errors=True)
        shutil.rmtree(state.get("lroot") or "/nonexistent", ignore_errors=True)
        print("\n" + ("删除链路测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
