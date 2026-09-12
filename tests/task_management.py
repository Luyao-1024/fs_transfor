"""任务取消、部分结果、显式重试和历史边界，无 GUI / SSH 依赖。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib

from fsapp.backend.base import BackendError
from fsapp.backend.local import LocalBackend
from fsapp.task_history import MAX_HISTORY, TaskHistory, retry_record, snapshot
from fsapp.transfer import Transfer, TransferManager


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


def drain():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def wait(manager):
    deadline = time.monotonic() + 10
    while manager.busy and time.monotonic() < deadline:
        drain()
        time.sleep(0.005)
    drain()
    check(not manager.busy, "任务及清理在超时前结束")


class FailSecond(LocalBackend):
    def open_read(self, path):
        if self.basename(path) == "b.txt":
            raise BackendError("模拟读取失败")
        return super().open_read(path)


def main():
    with tempfile.TemporaryDirectory(prefix="fstransfor-tasks-") as temp:
        root = Path(temp)
        paths = []
        for name in ("a.txt", "b.txt", "c.txt"):
            path = root / name
            path.write_text(name, encoding="utf-8")
            paths.append(str(path))
        manager = TransferManager()
        manager.history = TaskHistory(root / "history.json")
        try:
            task = manager.enqueue(FailSecond(), paths, LocalBackend(), str(root / "out"))
            wait(manager)
            check(task.status == "partial" and task.item_status == {
                "a.txt": "committed", "b.txt": "error", "c.txt": "committed"},
                "单项失败只影响该项，同任务其余文件继续传输")
            check(task.done_files == 2 and task.file_count == 3 and task.total_known,
                  "文件数和已提交数量准确")
            check(not (root / "out" / "b.txt").exists() and (root / "out" / "c.txt").exists(),
                  "失败项不产出目标文件，成功项内容已提交")
            retry = retry_record(manager, snapshot(task), failed_only=True)
            wait(manager)
            check(retry.src_paths == paths[1:2] and retry.status == "done", "仅重试失败和未完成项")
            check(all((root / "out" / Path(p).name).read_text() == Path(p).name for p in paths),
                  "重试后全部文件内容正确")
            answers = []
            manager.ask_overwrite = lambda names: answers.append(names) or "skip"
            single = retry_record(manager, snapshot(task), names={"a.txt"})
            wait(manager)
            check(answers == [["a.txt"]] and single.item_status["a.txt"] == "skipped",
                  "单项重试重新检查冲突")
            manager.ask_overwrite = None
            moved = manager.enqueue(LocalBackend(), [paths[0]], LocalBackend(), str(root / "moved"), move=True)
            wait(manager)
            try:
                retry_record(manager, snapshot(moved))
            except BackendError:
                check(not Path(paths[0]).exists(), "已完成移动不能被自动重放")
            else:
                raise AssertionError("重放了已完成移动")

            missing = manager.enqueue(LocalBackend(), [paths[0]], LocalBackend(), str(root / "missing"))
            wait(manager)
            check(missing.status == "error" and "不存在" in missing.error,
                  "源已消失不能显示成功")
            loaded = TaskHistory(root / "history.json")
            check(len(loaded.records) == len(manager.history.records), "历史重启后可读取，未提交新任务")

            manager._pool.shutdown()
            manager._pool = ThreadPoolExecutor(max_workers=1)
            gate = threading.Event()
            blocker = manager._pool.submit(gate.wait, 5)
            try:
                queued = manager.enqueue(LocalBackend(), [paths[1]], LocalBackend(), str(root / "queued"))
                manager.cancel(queued.id)
                check(queued.status == "cancelled" and queued.future.cancelled() and queued.finalized,
                      "排队任务立即取消并完成清理，无需等待线程空闲")
                check(not (root / "queued").exists(), "排队取消不写目标目录")
            finally:
                gate.set()
                blocker.result(timeout=5)

            deleting, finish_delete = threading.Event(), threading.Event()

            class SlowDelete(LocalBackend):
                def delete(self, path):
                    deleting.set()
                    if not finish_delete.wait(5):
                        raise TimeoutError("delete test gate")
                    super().delete(path)

            move_source = root / "slow_move.txt"
            move_source.write_text("move")
            moving = manager.enqueue(SlowDelete(), [str(move_source)], LocalBackend(),
                                      str(root / "slow_move_out"), move=True)
            try:
                check(deleting.wait(5), "移动已进入删除源阶段")
                manager.clear_finished()
                check(moving.running and manager.busy and moving in manager.transfers,
                      "移动源清理完成前仍是活动任务，不能被清理记录移除")
                check(not manager.remove_finished(moving.id), "单条移除也不会删除正在清理的任务")
            finally:
                finish_delete.set()
            wait(manager)
            check(moving.status == "done", "移动源清理结束后才报告完成")
            manager.stop_accepting()
            rejected = manager.enqueue(LocalBackend(), [paths[1]], LocalBackend(), str(root / "rejected"))
            check(rejected.status == "error" and not (root / "rejected").exists(), "退出阶段拒绝新任务")
            wait(manager)

            class Remote(LocalBackend):
                is_local = False
                host, port, username = "test.invalid", 22, "test"
                password, passphrase = "DO_NOT_SAVE_PASSWORD", "DO_NOT_SAVE_PASSPHRASE"

            history = TaskHistory(root / "bounded.json")
            for i in range(MAX_HISTORY + 3):
                record = Transfer(Remote(), LocalBackend(), [paths[1]], str(root / "out"), "download")
                record.status = "done"
                history.record(record)
            text = history.path.read_text()
            check(len(history.records) == MAX_HISTORY, "历史按数量限制为 200 条")
            check("DO_NOT_SAVE" not in text and "password" not in text and "passphrase" not in text,
                  "历史白名单不包含连接凭据")
            check(len(TaskHistory(history.path).records) == MAX_HISTORY, "有界历史可恢复")
            warnings = []
            broken = root / "broken.json"
            broken.write_text("{broken")
            check(not TaskHistory(broken, warnings.append).records and broken.read_text() == "{broken",
                  "损坏历史恢复为空并保留原文件")
            bad = TaskHistory(root / "absent.json", warnings.append)
            bad.path = root  # 不能将文件替换到目录上，模拟保存失败。
            bad.save()
            check(len(warnings) == 2, "读取和写入错误均提供提示")
            manager.clear_finished()
            check(not manager.transfers and json.loads((root / "history.json").read_text()) == [],
                  "清理已结束记录同时更新持久历史")
        finally:
            manager._pool.shutdown(wait=True)
            drain()
    print("task management: all passed")


if __name__ == "__main__":
    main()
