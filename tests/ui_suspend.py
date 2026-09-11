"""连接暂停/恢复 UX 测试: 断线不退出文件界面, 暂停按钮 ⇄ 恢复按钮.

用 FakeRemote(本地目录伪装远程)注入连接池, 验证:
- suspend_connection: 文件页保留 + 背景提示 + 列表清空 + cwd 保留
- resume_connection: 重新复用连接并回到原目录
- connection_lost_ui(意外断线): 同样保留文件页并显示"连接已断开"
运行: .venv/bin/python tests/ui_suspend.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile as _tf

os.environ["FSTRANSFOR_CONFIG_HOME"] = _tf.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import GLib

from fsapp.application import Application
from fsapp.backend.local import LocalBackend
from fsapp.connections import ConnectionHub, _Entry

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


class FakeRemote(LocalBackend):
    is_local = False

    def __init__(self, root):
        super().__init__()
        self._root = root

    @property
    def label(self):
        return "fake@remote"

    def home(self):
        return self._root


FAKE_CFG = {"id": "fakeid", "name": "FakeSrv", "host": "susptest.example",
            "port": 22, "username": "fake", "auth_method": "key", "key_path": None}

PLAY = "media-playback-start-symbolic"
PAUSE = "media-playback-pause-symbolic"


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None, "root": None}

    def start(a):
        state["win"] = a.props.active_window
        root = tempfile.mkdtemp(prefix="fstransfer-suspend-")
        state["root"] = root
        os.makedirs(f"{root}/docs")
        with open(f"{root}/docs/a.txt", "wb") as f:
            f.write(b"hello")
        fake = FakeRemote(root)
        win = state["win"]
        win.hub._live[ConnectionHub.key(FAKE_CFG)] = _Entry(fake, FAKE_CFG)
        win.left.connect_server_async(FAKE_CFG)
        GLib.timeout_add(800, step_connected)
        return GLib.SOURCE_REMOVE

    def wait_files(step_next, tries=0):
        win = state["win"]
        pane = win.left
        if pane.stack.get_visible_child_name() == "files" and pane.store.get_n_items() > 0:
            step_next()
            return GLib.SOURCE_REMOVE
        if tries > 50:
            check(False, "等待文件列表加载超时")
            finish()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(200, wait_files, step_next, tries + 1)
        return GLib.SOURCE_REMOVE

    def step_connected():
        state["win"].left.navigate(f"{state['root']}/docs")
        wait_files(step_suspend)
        return GLib.SOURCE_REMOVE

    def step_suspend():
        pane = state["win"].left
        check(pane.stack.get_visible_child_name() == "files", "连接后处于文件界面")
        check(not pane.suspended, "初始为非暂停状态")
        check(pane.disconnect_btn.get_icon_name() == PAUSE, "连接时显示暂停图标")
        cwd_before = pane.cwd

        pane.suspend_connection()
        check(pane.suspended, "暂停后进入挂起状态")
        check(pane.stack.get_visible_child_name() == "files",
              "暂停后仍停留在文件界面(不退出到本地/断开页)")
        check(pane.store.get_n_items() == 0, "暂停后文件列表已清空")
        check(pane.suspend_revealer.get_reveal_child(), "背景提示已显示")
        check(pane.suspend_title.get_text() == "连接已暂停", "提示词为'连接已暂停'")
        check(pane.disconnect_btn.get_icon_name() == PLAY, "按钮切换为播放(恢复)图标")
        check(pane.cwd == cwd_before, "当前路径保留待恢复")
        check(not pane.path_entry.get_sensitive(), "暂停时路径输入不可用")
        state["cwd_before"] = cwd_before

        # 暂停期间连接池中该面板已释放, 但连接本体仍可被其他面板复用
        GLib.timeout_add(300, step_resume)
        return GLib.SOURCE_REMOVE

    def step_resume():
        pane = state["win"].left
        pane.resume_connection()
        wait_files(step_verify_resume)
        return GLib.SOURCE_REMOVE

    def step_verify_resume():
        pane = state["win"].left
        check(pane.stack.get_visible_child_name() == "files", "恢复后回到文件界面")
        check(not pane.suspended, "恢复后退出挂起状态")
        check(pane.cwd == state["cwd_before"], "恢复到暂停前的目录")
        check(pane.store.get_n_items() > 0, "恢复后文件列表已重新加载")
        check(not pane.suspend_revealer.get_reveal_child(), "背景提示已隐藏")
        check(pane.disconnect_btn.get_icon_name() == PAUSE, "恢复后按钮回到暂停图标")
        GLib.timeout_add(200, step_lost)
        return GLib.SOURCE_REMOVE

    def step_lost():
        pane = state["win"].left
        # 模拟意外断线(连接池 mark_dead 的通知路径)
        pane.connection_lost_ui("网络超时")
        check(pane.suspended, "断线后进入挂起状态")
        check(pane.stack.get_visible_child_name() == "files",
              "意外断线仍停留在文件界面")
        check(pane.suspend_revealer.get_reveal_child(), "断线背景提示已显示")
        check("连接已断开" in pane.suspend_title.get_text(), "提示词为'连接已断开'")
        check(pane.disconnect_btn.get_icon_name() == PLAY, "断线后按钮为恢复图标")
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        print("\n" + ("暂停/恢复测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    app.run([])
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
