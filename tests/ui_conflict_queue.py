"""同名确认的非阻塞路径: 等待确认不占并发额度, 覆盖/跳过/取消三种答复的落地结果。

覆盖真实窗口链路: TransferManager.ask_conflict_async → ask_overwrite 对话框
→ resolve_conflict → 任务续跑或取消; 并验证停在确认框上的任务不会让排队
任务饿死(旧实现会阻塞住仅有的两个 worker 并在 10 分钟后静默取消)。
运行: .venv/bin/python tests/ui_conflict_queue.py   (窗口会短暂出现)
"""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_ROOT = tempfile.TemporaryDirectory(prefix="fstransfor-conflict-ui-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = TEST_ROOT.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from fsapp.application import Application  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok: {message}", flush=True)


def until(predicate, timeout=15, what="任务状态"):
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


def dialog_for(win, task):
    """取该任务自己的确认框: 每个等待中的任务都必须有独立对话框."""
    entry = win._conflict_dialogs.get(task.id)
    check(entry is not None and entry[0] is not None, "任务已登记自己的确认框")
    dialog = entry[0]
    check("覆盖已存在的项目" in dialog.get_heading(), "确认框标题正确")
    return dialog


def click(dialog, label):
    button = next((w for w in widgets(dialog)
                   if isinstance(w, Gtk.Button) and w.get_label() == label), None)
    check(button is not None, f"找到对话框按钮：{label}")
    button.emit("clicked")


def open_dialogs(win):
    """当前挂在窗口上的同名确认框数量."""
    return len(win._conflict_dialogs)


def main():
    app = Application(application_id="io.github.fstransfer.ConflictQueueTest")
    app.register(None)
    app.activate()
    win = app.props.active_window
    root = Path(TEST_ROOT.name)
    target = root / "target"
    target.mkdir()
    local = win.local_backend

    def source(name, text):
        path = root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    try:
        # ---- 两条冲突任务停在确认框上 ----
        parked = []
        for index in (1, 2):
            name = f"job{index}.txt"
            (target / name).write_text(f"OLD{index}", encoding="utf-8")
            parked.append(win.manager.enqueue(local, [source(name, f"NEW{index}")],
                                             local, str(target)))
        until(lambda: all(t.parked for t in parked) and open_dialogs(win) == 2,
              what="两个确认框")
        check(all(t.phase == "waiting" and t.status == "running" for t in parked),
              "等待确认的任务状态准确(仍在运行, 阶段为等待确认)")
        check(dialog_for(win, parked[0]) is not dialog_for(win, parked[1]),
              "两个等待中的任务各自拥有独立确认框(不会串答)")
        labels = {w.get_label() for w in widgets(dialog_for(win, parked[0]))
                  if isinstance(w, Gtk.Button) and w.get_label()}
        check(labels == {"取消", "跳过", "覆盖"},
              f"确认框提供取消/跳过/覆盖三个出口({sorted(labels)})")
        detail = " ".join(w.get_text() for w in widgets(dialog_for(win, parked[0]))
                          if isinstance(w, Gtk.Label))
        check("job1.txt" in detail, f"确认框列出该任务自己的冲突项({detail[:60]})")

        # ---- 并发额度没被等待确认占满: 第三条任务照常完成 ----
        quiet = win.manager.enqueue(local, [source("quiet.txt", "QUIET")], local,
                                    str(target))
        until(lambda: quiet.status == "done", what="无冲突任务完成")
        check((target / "quiet.txt").read_text(encoding="utf-8") == "QUIET",
              "等待确认期间新任务仍能传输(工作线程未被占用)")
        check(all(t.parked for t in parked) and open_dialogs(win) == 2,
              "已完成任务不影响仍在等待确认的任务")

        # ---- 覆盖: 原子替换目标内容 ----
        click(dialog_for(win, parked[0]), "覆盖")
        until(lambda: not parked[0].parked, what="第一个任务续跑")
        until(lambda: parked[0].status == "done" and parked[0].ended_at is not None,
              what="第一个任务完成")
        check((target / "job1.txt").read_text(encoding="utf-8") == "NEW1",
              "选择覆盖后目标内容被替换")

        # ---- 取消: 任务立即结束且目标不变 ----
        click(dialog_for(win, parked[1]), "取消")
        until(lambda: parked[1].status == "cancelled", what="第二个任务取消")
        check((target / "job2.txt").read_text(encoding="utf-8") == "OLD2",
              "取消确认后已有目标内容不变")
        check(not parked[1].parked and open_dialogs(win) == 0,
              "取消后确认框登记被清理")
        second_answer = win.manager.resolve_conflict(parked[1].id, "overwrite")
        check(not second_answer and (target / "job2.txt").read_text(encoding="utf-8") == "OLD2",
              "已结束任务不接受迟到答复，不会被复活")

        # ---- 跳过: 移动语义保留源, 目标保持原样 ----
        (target / "job3.txt").write_text("OLD3", encoding="utf-8")
        moved = win.manager.enqueue(local, [source("job3.txt", "NEW3")], local,
                                    str(target), move=True)
        until(lambda: moved.parked, what="移动任务确认框")
        click(dialog_for(win, moved), "跳过")
        until(lambda: not moved.running, what="移动任务结束")
        check(moved.item_status.get("job3.txt") == "skipped"
              and (root / "job3.txt").is_file()
              and (target / "job3.txt").read_text(encoding="utf-8") == "OLD3",
              f"跳过后源保留、目标不变({moved.status}/{moved.note})")

        # ---- 退出时未答复的确认框必须收尾 ----
        (target / "job4.txt").write_text("OLD4", encoding="utf-8")
        pending_exit = win.manager.enqueue(local, [source("job4.txt", "NEW4")], local,
                                           str(target))
        until(lambda: pending_exit.parked, what="退出前确认框")
        win._dismiss_dialogs()
        until(lambda: pending_exit.status == "cancelled", what="确认框退出收尾")
        check(open_dialogs(win) == 0
              and (target / "job4.txt").read_text(encoding="utf-8") == "OLD4",
              "退出清理会让等待确认的任务按取消收尾")
        check(not any(name.startswith(".fstransfer-tmp-")
                      for name in os.listdir(target)), "确认流程不残留临时文件")
    finally:
        win.manager.stop_accepting(cancel=True)
        until(lambda: not win.manager.busy, what="任务清理")
        win._finish_close()
        win.destroy()
        app.quit()
        TEST_ROOT.cleanup()
    print("conflict queue UI: all passed")


if __name__ == "__main__":
    main()
