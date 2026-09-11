"""多标签页 + 连接共享 + 剪贴板 + 回收站删除 + 旧配置迁移 测试.

运行: .venv/bin/python tests/ui_tabs.py
"""
import json
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
from fsapp.backend.base import BaseBackend, BackendError
from fsapp.connections import ConnectionHub, _Entry

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


class FakeRemote(BaseBackend):
    """用于验证连接共享的假远程后端(记录 disconnect 次数)."""

    is_local = False

    def __init__(self):
        self.disconnect_calls = 0

    @property
    def label(self):
        return "fake@hubtest"

    def disconnect(self):
        self.disconnect_calls += 1
        self.dead = True

    def home(self):
        return "/fakehome"

    def list_dir(self, path):
        return []

    def stat(self, path):
        return None

    def exists(self, path):
        return False

    def open_read(self, path):
        raise BackendError("fake")

    def open_write(self, path):
        raise BackendError("fake")

    def mkdir(self, path):
        pass

    def delete(self, path):
        pass

    def rename(self, old, new):
        pass

    def parent(self, path):
        return "/"

    def basename(self, path):
        return path.rsplit("/", 1)[-1]

    def join(self, base, name):
        return (base.rstrip("/") + "/" + name) if base and base != "/" else "/" + name

    def normpath(self, path):
        return path or "/"


FAKE_CFG = {"id": "fakeid", "name": "FakeSrv", "host": "hubtest.example",
            "port": 22, "username": "fake", "auth_method": "key", "key_path": None}


def wait_transfer(win, timeout=20):
    start = time.monotonic()
    while any(t.running for t in win.manager.transfers):
        if time.monotonic() - start > timeout:
            return False
        time.sleep(0.05)
    return True


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None, "tmpdir": None}

    def start(a):
        state["win"] = a.props.active_window
        GLib.timeout_add(900, step_tabs)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_tabs():
        win = state["win"]
        check(len(win.workspaces) == 1, "启动时 1 个工作区")
        check(win.tab_view.get_n_pages() == 1, "1 个标签页")
        ws2 = win.new_workspace()
        check(win.tab_view.get_n_pages() == 2, "新建标签后 2 页")
        check(win.tab_view.get_selected_page().get_child() is ws2, "新标签被选中")
        ids = {p.pane_id for ws in win.workspaces for p in (ws.left, ws.right)}
        check(len(ids) == 4, f"面板 ID 全局唯一({len(ids)})")
        GLib.timeout_add(400, step_share)
        return GLib.SOURCE_REMOVE

    def step_share():
        win = state["win"]
        ws1, ws2 = win.workspaces
        # 预置一条"已连接"的假后端
        fake = FakeRemote()
        win.hub._live[ConnectionHub.key(FAKE_CFG)] = _Entry(fake, FAKE_CFG)
        # 两个不同标签的面板连接同一服务器 → 复用同一条连接
        ws1.left.connect_server_async(FAKE_CFG)
        check(ws1.left.backend is fake, "标签1连接复用现有后端")
        check(ws1.left.stack.get_visible_child_name() == "files", "立即进入 files 状态(无需重连)")
        ws2.left.connect_server_async(FAKE_CFG)
        check(ws2.left.backend is fake, "标签2连接同一后端实例")
        check(fake.disconnect_calls == 0, "复用期间未断开")
        # 释放: 第一个断开不应真正断线
        ws1.left.disconnect_remote()
        check(fake.disconnect_calls == 0, "仍有使用方时未断开")
        check(ws1.left.server_cfg is None, "标签1回到本地")
        check(ws2.left.server_cfg is not None, "标签2仍连接")
        # 最后一个释放 → 真正断开
        ws2.left.disconnect_remote()
        check(fake.disconnect_calls == 1, "最后一个使用方释放后断开")
        GLib.timeout_add(300, step_mark_dead)
        return GLib.SOURCE_REMOVE

    def step_mark_dead():
        win = state["win"]
        ws1, ws2 = win.workspaces
        fake = FakeRemote()
        win.hub._live[ConnectionHub.key(FAKE_CFG)] = _Entry(fake, FAKE_CFG)
        ws1.left.connect_server_async(FAKE_CFG)
        ws2.left.connect_server_async(FAKE_CFG)
        # 连接死亡 → 所有使用它的面板同时进入断开状态
        win.hub.mark_dead(fake, "网络中断")
        # 断线通知: 面板保留文件界面并进入挂起提示(不再切到 disconnected 页)
        check(ws1.left.suspended and ws1.left.stack.get_visible_child_name() == "files",
              "标签1收到断开通知(保留文件界面)")
        check(ws2.left.suspended and ws2.left.suspend_revealer.get_reveal_child(),
              "标签2收到断开通知并显示背景提示")
        check(fake.disconnect_calls == 1, "死亡连接被清理")
        GLib.timeout_add(300, step_clipboard)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_clipboard():
        win = state["win"]
        tmpdir = tempfile.mkdtemp(prefix="fstransfer-tab-")
        state["tmpdir"] = tmpdir
        os.makedirs(f"{tmpdir}/out", exist_ok=True)
        with open(f"{tmpdir}/a.txt", "wb") as f:
            f.write(b"copy-me")
        with open(f"{tmpdir}/b.txt", "wb") as f:
            f.write(b"cut-me")
        ws1 = win.workspaces[0]
        ws1.left.connect_local()  # 从断开状态回到本地
        GLib.timeout_add(500, lambda: (ws1.left.navigate(tmpdir),
                                       GLib.timeout_add(700, step_copy)))
        return GLib.SOURCE_REMOVE

    def step_copy():
        win = state["win"]
        ws1 = win.workspaces[0]
        tmpdir = state["tmpdir"]
        pane = ws1.left
        # 选中 a.txt
        pos = -1
        for i in range(pane.sort_model.get_n_items()):
            if pane.sort_model.get_item(i).entry.name == "a.txt":
                pos = i
                break
        if pos < 0:
            check(False, "找不到 a.txt")
            return GLib.SOURCE_REMOVE
        pane.selection.select_item(pos, True)
        pane._action_copy()
        clip = win.get_clipboard()
        check(clip is not None and clip[3] == "copy" and clip[2] == [f"{tmpdir}/a.txt"],
              "Ctrl+C: 剪贴板记录源后端与路径")
        ws1.right.navigate(f"{tmpdir}/out")
        GLib.timeout_add(700, step_paste_copy)
        return GLib.SOURCE_REMOVE

    def step_paste_copy():
        win = state["win"]
        tmpdir = state["tmpdir"]
        pane = win.workspaces[0].right
        pane._action_paste()
        GLib.timeout_add(300, step_poll_copy)
        return GLib.SOURCE_REMOVE

    def step_poll_copy():
        win = state["win"]
        tmpdir = state["tmpdir"]
        if not wait_transfer(win):
            check(False, "复制粘贴传输超时")
            return GLib.SOURCE_REMOVE
        check(os.path.exists(f"{tmpdir}/out/a.txt"), "Ctrl+V 粘贴复制完成")
        check(os.path.exists(f"{tmpdir}/a.txt"), "复制后源保留")
        # 剪切粘贴: 源应被删除
        pane = win.workspaces[0].left
        pos = -1
        for i in range(pane.sort_model.get_n_items()):
            if pane.sort_model.get_item(i).entry.name == "b.txt":
                pos = i
                break
        pane.selection.select_item(pos, True)
        pane._action_cut()
        win.workspaces[0].right._action_paste()
        GLib.timeout_add(300, step_poll_cut)
        return GLib.SOURCE_REMOVE

    def step_poll_cut():
        win = state["win"]
        tmpdir = state["tmpdir"]
        if not wait_transfer(win):
            check(False, "剪切粘贴传输超时")
            return GLib.SOURCE_REMOVE
        check(os.path.exists(f"{tmpdir}/out/b.txt"), "剪切粘贴: 目标就位")
        check(not os.path.exists(f"{tmpdir}/b.txt"), "剪切粘贴: 源已删除(移动语义)")
        # 同目录剪切粘贴 = 无操作
        pane = win.workspaces[0].left
        pos = -1
        for i in range(pane.sort_model.get_n_items()):
            if pane.sort_model.get_item(i).entry.name == "a.txt":
                pos = i
                break
        pane.selection.select_item(pos, True)
        pane._action_cut()
        n_before = len(win.manager.transfers)
        pane._action_paste()
        check(len(win.manager.transfers) == n_before, "同目录剪切粘贴: 不产生传输")
        check(win.get_clipboard() is None, "同目录粘贴后剪贴板清空")
        GLib.timeout_add(300, step_trash)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_trash():
        win = state["win"]
        from fsapp.backend.local import LocalBackend
        be = LocalBackend()
        tmpdir = state["tmpdir"]

        # /tmp(系统内部挂载)不支持回收站 → 应抛出明确错误
        p_tmp = os.path.join(tmpdir, f"no-trash-{int(time.time())}.txt")
        with open(p_tmp, "wb") as f:
            f.write(b"x")
        try:
            be.delete_to_trash(p_tmp)
            check(False, "/tmp 应不支持回收站(本机环境假设不成立)")
        except BackendError as e:
            check("回收站" in str(e), f"/tmp 回收站不可用有明确错误信息({e})")

        # 家目录文件 → 真实进入回收站
        trash_name = f"fstransfer-trash-{int(time.time() * 1000)}.txt"
        p_home = os.path.join(os.path.expanduser("~"), trash_name)
        with open(p_home, "wb") as f:
            f.write(b"trash-me")
        be.delete_to_trash(p_home)
        check(not os.path.exists(p_home), "本地删除: 原位置已移除")
        trash_files = os.path.expanduser("~/.local/share/Trash/files")
        trashed = os.path.join(trash_files, trash_name)
        check(os.path.exists(trashed), "本地删除: 文件确实进入回收站")
        # 清理测试痕迹(回收站中的文件与 trashinfo)
        for q in (trashed,
                  os.path.expanduser(f"~/.local/share/Trash/info/{trash_name}.trashinfo")):
            try:
                os.remove(q)
            except OSError:
                pass
        # 远程后端不支持回收站
        try:
            FakeRemote().delete_to_trash("/x")
            check(False, "远程后端应不支持回收站")
        except BackendError:
            check(True, "远程后端不支持回收站(抛 BackendError)")
        GLib.timeout_add(200, step_migration)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_migration():
        # 旧单标签格式 → 迁移为 tabs
        old = {"auto_connect": False, "show_hidden": True,
               "left": {"type": "local", "path": "/tmp/oldL"},
               "right": {"type": "server", "server_id": "abc", "path": "/remote"}}
        with open(config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(old, f)
        s = config.load_settings()
        tab0 = (s.get("tabs") or [{}])[0]
        check(tab0.get("left", {}).get("path") == "/tmp/oldL", "旧配置迁移: left 保留")
        check(tab0.get("right", {}).get("server_id") == "abc", "旧配置迁移: right 保留")
        check(s.get("show_hidden") is True and s.get("auto_connect") is False,
              "旧配置迁移: 其他设置保留")
        GLib.timeout_add(200, step_close)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def step_close():
        win = state["win"]
        pages = win.tab_view.get_n_pages()
        win._on_close_tab()  # 关闭当前标签
        GLib.timeout_add(300, step_close_poll, pages)
        return GLib.SOURCE_REMOVE

    def step_close_poll(pages):
        win = state["win"]
        check(win.tab_view.get_n_pages() == pages - 1, "关闭标签后页数减一")
        check(len(win.workspaces) == pages - 1, "工作区列表同步")
        # 关闭最后一个标签: 应用应自动补一个新标签(始终至少一个)
        win._on_close_tab()
        GLib.timeout_add(300, step_close_last)
        return GLib.SOURCE_REMOVE

    def step_close_last():
        win = state["win"]
        check(win.tab_view.get_n_pages() == 1 and len(win.workspaces) == 1,
              "关闭最后一个标签后自动新建一个")
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        shutil.rmtree(state.get("tmpdir") or "/nonexistent", ignore_errors=True)
        # 清掉测试产生的回收站文件
        print("\n" + ("标签页测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
