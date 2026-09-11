"""带 UI 的自动化冒烟测试: 真实窗口 + 面板浏览 + 本地传输 + 会话保存.

不测 SSH 与真实拖拽(需要交互), 但覆盖全部控件代码路径.
运行: .venv/bin/python tests/ui_smoke.py   (窗口会在屏幕上短暂出现)
"""
import os
import shutil
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
# 隔离配置目录: 不读/不写用户的真实会话与服务器配置
os.environ["FSTRANSFOR_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import GLib

from fsapp import config
from fsapp.application import Application

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def main():
    had_config = os.path.exists(str(config.CONFIG_DIR))

    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))

    state = {"win": None, "src": None, "transfer": None}

    def start(a):
        win = a.props.active_window
        state["win"] = win
        GLib.timeout_add(900, step_listing)
        return GLib.SOURCE_REMOVE

    def step_listing():
        win = state["win"]
        check(win.left.stack.get_visible_child_name() == "files", "左面板进入 files 状态")
        check(win.right.stack.get_visible_child_name() == "files", "右面板进入 files 状态")
        check(win.left.store.get_n_items() > 0, f"左面板已列出主目录({win.left.store.get_n_items()} 项)")
        check(win.right.store.get_n_items() > 0, "右面板已列出主目录")
        # 导航到 /tmp
        win.left.navigate("/tmp")
        GLib.timeout_add(700, step_navigated)
        return GLib.SOURCE_REMOVE

    def step_navigated():
        win = state["win"]
        check(win.left.cwd == "/tmp", f"左面板导航到 /tmp (cwd={win.left.cwd})")
        check(win.left.path_entry.get_text() == "/tmp", "路径栏显示 /tmp")

        # 准备一个源文件并传输到右面板的临时目录
        tmpdir = tempfile.mkdtemp(prefix="fstransfer-ui-")
        state["tmpdir"] = tmpdir
        src = os.path.join(tmpdir, "smoke.txt")
        with open(src, "wb") as f:
            f.write(os.urandom(256 * 1024))
        state["src"] = src
        win.right.navigate(tmpdir)
        GLib.timeout_add(700, step_transfer)
        return GLib.SOURCE_REMOVE

    def step_transfer():
        win = state["win"]
        dst_dir = os.path.join(state["tmpdir"], "out")
        win.manager.enqueue(win.local_backend, [state["src"]], win.local_backend, dst_dir)
        GLib.timeout_add(300, step_transfer_poll, dst_dir)
        return GLib.SOURCE_REMOVE

    def step_transfer_poll(dst_dir):
        win = state["win"]
        ts = win.manager.transfers
        running = any(t.running for t in ts)
        if running:
            # 传输中: 面板应可见且有速度/进度
            check(win.transfer_panel.get_reveal_child(), "传输面板已展开")
            t = ts[-1]
            if t.done_bytes > 0 and t.speed <= 0:
                check(False, f"传输中速度应为正(done={t.done_bytes})")
            return GLib.SOURCE_CONTINUE
        t = ts[-1] if ts else None
        check(t is not None and t.status == "done", f"传输完成(status={t and t.status})")
        out = os.path.join(dst_dir, "smoke.txt")
        check(os.path.exists(out) and os.path.getsize(out) == 256 * 1024, "文件已复制且大小一致")
        GLib.timeout_add(600, step_settings)
        return GLib.SOURCE_REMOVE

    def step_settings():
        # 会话记忆: 标签页数组中第一个标签的左面板应为 /tmp
        s = config.load_settings()
        tab0 = (s.get("tabs") or [{}])[0]
        check(tab0.get("left", {}).get("path") == "/tmp",
              f"会话记忆标签0左面板路径={tab0.get('left', {}).get('path')}")
        check(tab0.get("right", {}).get("type") == "local", "会话记忆标签0右面板=local")
        # 收尾
        shutil.rmtree(state.get("tmpdir", ""), ignore_errors=True)
        if not had_config:
            shutil.rmtree(str(config.CONFIG_DIR), ignore_errors=True)
        print("\n" + ("UI 冒烟测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
