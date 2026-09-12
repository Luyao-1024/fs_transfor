"""连接租约与过期请求回归；使用本地文件模拟远端，不连接 SSH。"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib

from fsapp.backend.local import LocalBackend
from fsapp.connections import ConnectionHub, _Entry
from fsapp.pane import FilePane
from fsapp.transfer import Transfer, TransferManager


CFG = {"host": "test.invalid", "port": 22, "username": "test"}


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


class Remote(LocalBackend):
    is_local = False

    def __init__(self):
        super().__init__()
        self.dead = False
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1
        self.dead = True

    def same_as(self, other):
        return self is other


class Pane:
    def __init__(self):
        self.workspace = SimpleNamespace(closed=False)
        self.lost = []

    def connection_lost_ui(self, message):
        self.lost.append(message)


def drain():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def wait_done(task):
    deadline = time.monotonic() + 5
    while task.running and time.monotonic() < deadline:
        drain()
        time.sleep(0.005)
    drain()
    check(not task.running, "任务在超时前结束")


def transfer_cases(root):
    source = root / "source.txt"
    source.write_bytes(b"lease test")
    for scenario in ("done", "cancel", "dead", "error"):
        hub = ConnectionHub(None)
        remote = Remote()
        pane = Pane()
        entry = _Entry(remote, CFG)
        entry.users.add(pane)
        hub._live[hub.key(CFG)] = entry
        manager = TransferManager(hub)
        manager._pool.shutdown()
        manager._pool = ThreadPoolExecutor(max_workers=1)
        gate = threading.Event()
        blocker = manager._pool.submit(gate.wait, 5)
        try:
            paths = [str(source if scenario != "error" else root / "missing")]
            task = manager.enqueue(remote, paths, LocalBackend(), str(root / scenario))
            check(task in entry.tasks, f"{scenario}: 排队任务持有连接")
            hub.release(pane, remote)
            check(remote.disconnects == 0, "最后一个面板关闭仍保留任务连接")
            if scenario == "cancel":
                manager.cancel(task.id)
            elif scenario == "dead":
                hub.mark_dead(remote, "模拟断网")
            elif scenario == "error":
                # 缺失文件由 stat 返回 None；注入明确读错误覆盖异常清理。
                task.src_paths = [str(source)]
                remote.open_read = lambda path: (_ for _ in ()).throw(OSError("read failed"))
            gate.set()
            blocker.result(timeout=5)
            wait_done(task)
            check(task.status == {"cancel": "cancelled", "dead": "error"}.get(
                scenario, scenario), f"{scenario}: 准确终态 {task.status}")
            check(remote.disconnects == 1 and not hub._live, "任务结束后连接仅释放一次")
            if scenario == "done":
                check((root / scenario / source.name).read_bytes() == source.read_bytes(),
                      "面板关闭后传输内容完整")
            if scenario == "dead":
                check("模拟断网" in task.error, "断线显示真实原因而非用户取消")
            stale = manager.enqueue(remote, [str(source)], LocalBackend(), str(root / "stale"))
            check(stale.status == "error" and "重新连接" in stale.error,
                  "失效剪贴板后端给出重新连接提示")
        finally:
            gate.set()
            manager._pool.shutdown(wait=True)
            drain()

    hub = ConnectionHub(None)
    remote = Remote()
    entry = _Entry(remote, CFG)
    hub._live[hub.key(CFG)] = entry
    task = Transfer(remote, remote, [], "/", "copy")
    hub.acquire_task(task)
    check(len(entry.tasks) == 1, "同一远端作源和目标仅持有一个租约")
    hub.release_task(task)
    check(remote.disconnects == 1, "双向共享租约只释放一次")

    remote = Remote()
    pane = Pane()
    entry = _Entry(remote, CFG)
    entry.users.add(pane)
    hub._live[hub.key(CFG)] = entry
    manager = TransferManager(hub)
    try:
        invalid = manager.enqueue(remote, [str(source)], remote, str(root))
        check(invalid.status == "error" and not entry.tasks,
              "危险计划拒绝后不遗留任务租约")
        with patch.object(manager._pool, "submit", side_effect=RuntimeError("pool closed")):
            rejected = manager.enqueue(remote, [str(source)], LocalBackend(), str(root / "rejected"))
        check(rejected.status == "error" and not entry.tasks and not remote.dead,
              "线程池提交失败释放任务引用并保留面板连接")
        hub.release(pane, remote)
        check(remote.disconnects == 1, "提交失败后最后面板可正常释放连接")
    finally:
        manager._pool.shutdown(wait=True)
        drain()


def request_cases():
    hub = ConnectionHub(None)
    pane = Pane()
    received = []
    callback = lambda *args: received.append(args)
    key = hub.key(CFG)
    with patch("fsapp.connections.threading.Thread"):
        hub.request_connect(pane, CFG, {}, "/old", callback)
        old = hub._pending[key]
        hub.cancel_pending(pane)
        hub.request_connect(pane, CFG, {}, "/new", callback)
        new = hub._pending[key]
        obsolete = Remote()
        hub._dispatch_ok(CFG, obsolete, old)
        hub._dispatch_fail_ui(key, "旧请求失败", old)
        check(not received and hub._pending[key] is new, "旧成功和失败回调均不消费新请求")
        check(obsolete.disconnects == 1, "过期连接被释放")
        current = Remote()
        hub._dispatch_ok(CFG, current, new)
        check(received == [(current, "/new", None)], "新请求收到正确路径和连接")
        hub.release(pane, current)

        received.clear()
        other = Pane()
        hub.request_connect(pane, CFG, {}, "/one", callback)
        shared = hub._pending[key]
        hub.request_connect(other, CFG, {}, "/two", callback)
        hub.cancel_pending(pane)
        check(hub._pending[key] is shared, "取消单个等待者保留共享请求身份")
        current = Remote()
        hub._dispatch_ok(CFG, current, shared)
        check(received == [(current, "/two", None)], "只通知仍等待的面板")
        hub.release(other, current)

        hub.request_connect(pane, CFG, {}, "/closed", callback)
        request = hub._pending[key]
        pane.workspace.closed = True
        hub.window = SimpleNamespace(blocking_dialog=lambda build: build(lambda value: None))
        built = []
        hub._request_dialog(CFG, request, lambda done: built.append(True))
        check(not built, "已关闭标签不弹出新的认证对话框")

        pane.workspace.closed = False
        completed, closed = [], []
        hub.window = SimpleNamespace(blocking_dialog=lambda build: build(completed.append))
        hub._request_dialog(CFG, request,
                            lambda done: SimpleNamespace(close=lambda: closed.append(True)))
        hub.cancel_pending(pane)
        check(completed == [None] and closed == [True] and not hub._dialogs,
              "最后等待者取消时关闭认证框并立即释放工作线程等待")


def running_case(root):
    hub = ConnectionHub(None)
    remote = Remote()
    pane = Pane()
    entry = _Entry(remote, CFG)
    entry.users.add(pane)
    hub._live[hub.key(CFG)] = entry
    manager = TransferManager(hub)
    started, resume = threading.Event(), threading.Event()
    original_read = remote.open_read

    def open_read(path):
        started.set()
        if not resume.wait(5):
            raise TimeoutError("test read gate")
        check(not remote.dead, "运行中的任务关闭面板后仍可读取")
        return original_read(path)

    remote.open_read = open_read
    try:
        task = manager.enqueue(remote, [str(root / "source.txt")], LocalBackend(),
                               str(root / "running"))
        check(started.wait(5), "任务已进入读文件阶段")
        hub.release(pane, remote)
        check(not remote.dead, "运行期间关闭最后面板不提前断线")
        resume.set()
        wait_done(task)
        check(task.status == "done" and remote.disconnects == 1,
              "运行任务完成后释放最后引用")
    finally:
        resume.set()
        manager._pool.shutdown(wait=True)
        drain()


def operation_context_cases():
    calls, answers = [], []
    original = SimpleNamespace(
        is_local=True, join=lambda path, name: path + "/" + name,
        mkdir=lambda path: calls.append(("mkdir", path)),
        rename=lambda src, dst: calls.append(("rename", src, dst)),
        normpath=lambda path: path,
        exists=lambda path: False,      # 新建/重命名目标均不存在
        delete=lambda path: calls.append(("delete", path)))
    entry = SimpleNamespace(name="file", path="/original/file")
    pane = SimpleNamespace(backend=original, cwd="/original", window=None,
                           _selected_items=lambda: [SimpleNamespace(entry=entry)],
                           _run_op=lambda op: op())
    pane._do_mkdir = lambda *args: FilePane._do_mkdir(pane, *args)
    pane._do_rename = lambda *args: FilePane._do_rename(pane, *args)
    pane._do_delete = lambda *args: FilePane._do_delete(pane, *args)
    with patch("fsapp.pane.TextPromptDialog", side_effect=lambda win, cb, *a, **k: answers.append(cb)), \
            patch("fsapp.pane.ask_delete_local", side_effect=lambda win, names, cb: answers.append(cb)):
        FilePane._action_mkdir(pane)
        FilePane._action_rename(pane)
        FilePane._action_delete(pane)
    pane.backend = SimpleNamespace()  # 切换到另一个后端，不能在这里调用原操作。
    pane.cwd = "/elsewhere"
    for callback, answer in zip(answers, ("folder", "renamed", "permanent")):
        callback(answer)
    check(calls == [("mkdir", "/original/folder"),
                    ("rename", "/original/file", "/original/renamed"),
                    ("delete", "/original/file")],
          "确认期间切换位置，新建、重命名和删除仍使用原后端和路径")


def main():
    with tempfile.TemporaryDirectory(prefix="fstransfor-lifecycle-") as root:
        transfer_cases(Path(root))
        running_case(Path(root))
        request_cases()
        operation_context_cases()
    print("connection lifecycle: all passed")


if __name__ == "__main__":
    main()
