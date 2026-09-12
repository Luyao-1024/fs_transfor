"""任务中心窗口回归，配置、文件与历史全部隔离。"""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_ROOT = tempfile.TemporaryDirectory(prefix="fstransfor-task-ui-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = TEST_ROOT.name

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from fsapp.application import Application
from fsapp.task_history import TaskHistory


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("UI 等待超时")


def main():
    app = Application(application_id="io.github.fstransfer.TaskCenterTest")
    app.register(None)
    app.activate()
    win = app.props.active_window
    root = Path(TEST_ROOT.name)
    try:
        win.left.navigate(str(root))
        win.right.navigate(str(root))
        source = root / "report.txt"
        source.write_text("task center test")
        tasks = [win.manager.enqueue(win.local_backend, [str(source)], win.local_backend,
                                     str(root / f"copy-{i}")) for i in range(7)]
        until(lambda: not win.manager.busy)
        win.activate_action("win.tasks", None)
        until(lambda: win._task_center is not None and win._task_center.get_mapped())
        center = win._task_center
        check(len(center.rows) == 7, "超过 5 个任务仍全部出现在任务中心")
        check(len(win.transfer_panel._rows) <= 5, "轻量通知仍限制为最多 5 条")
        for task in tasks:
            task.finished_at -= 5
        win.transfer_panel.tick()
        check(not win.transfer_panel._rows and len(win.manager.transfers) == 7,
              "通知自动消失不删除任务记录")
        check(len(TaskHistory(win.manager.history.path).records) == 7, "完成结果已持久保存")
        failed = win.manager.enqueue(win.local_backend, [str(root / "missing.txt")],
                                      win.local_backend, str(root / "failed"))
        until(lambda: not win.manager.busy)
        center.sync()
        row = center.rows[failed.uid]
        row.set_expanded(True)
        check("不存在" in row.detail.get_text(), "失败原因与源路径在明细中可见")
        win.transfer_panel.tick()
        notification = win.transfer_panel._rows[failed.id]
        notification.cancel_btn.emit("clicked")
        win.transfer_panel.tick()
        check(failed in win.manager.transfers and failed.uid in center.rows,
              "关闭失败通知不删除错误记录")
        (root / "missing.txt").write_text("now available")
        center.retry(row.record, failed_only=True)
        until(lambda: not win.manager.busy)
        check((root / "failed" / "missing.txt").read_text() == "now available", "任务中心重试失败项成功")
        center.sync()
        shot = os.environ.get("FSTRANSFOR_TEST_SCREENSHOT")
        if shot:
            until(lambda: row.get_height() > 100)
            rendered_at = time.monotonic()
            until(lambda: time.monotonic() - rendered_at > 0.6)
            paintable = Gtk.WidgetPaintable.new(win)
            snap = Gtk.Snapshot.new()
            paintable.snapshot(snap, float(win.get_width()), float(win.get_height()))
            node = snap.to_node()
            texture = win.get_renderer().render_texture(node, None)
            Path(shot).parent.mkdir(parents=True, exist_ok=True)
            check(texture.save_to_png(shot), "任务中心截图已生成")
        before = len(win.manager._listeners)
        center.close()
        until(lambda: win._task_center is None)
        check(len(win.manager._listeners) == before - 1, "关闭任务中心释放监听器")
        win.manager.clear_finished()
        check(not win.manager.transfers and not TaskHistory(win.manager.history.path).records,
              "清理记录同时清空任务中心与磁盘历史")
        win.close()
        check(win._closed and not win._tick_id, "空闲关闭释放窗口定时器")
    finally:
        win.manager.stop_accepting(cancel=True)
        until(lambda: not win.manager.busy)
        if not win._closed:
            win._finish_close()
            win.destroy()
        app.quit()
        TEST_ROOT.cleanup()
    print("task center UI: all passed")


if __name__ == "__main__":
    main()
