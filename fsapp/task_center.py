"""完整任务中心：活动任务、有限历史、结果明细与显式重试。"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from .backend.base import BackendError
from .task_history import MAX_HISTORY, endpoint_label, retry_record, snapshot
from .util import fmt_size

STATUS_LABELS = {"pending": "排队", "scanning": "扫描目录", "waiting": "等待确认",
                 "transferring": "传输中", "committing": "提交文件",
                 "deleting_source": "清理移动源",
                 "done": "完成", "partial": "部分完成", "cancelled": "已取消",
                 "error": "失败", "running": "传输中"}
ITEM_LABELS = {"pending": "未完成", "committed": "已复制", "moved": "已移动",
               "skipped": "已跳过", "error": "失败", "cancelled": "未完成",
               "source_delete_failed": "复制成功，源删除失败"}


class TaskRow(Adw.ExpanderRow):
    def __init__(self, center, record, task=None):
        super().__init__()
        self.center = center
        self.task = task
        self.record = record
        self.set_use_markup(False)
        self.set_title("、".join(p.rstrip("/").rsplit("/", 1)[-1]
                                for p in record["paths"][:3]))
        self.set_subtitle(f'{endpoint_label(record["src"])} → '
                          f'{endpoint_label(record["dst"])} · {record["dst_dir"]}')
        self.state = Gtk.Label()
        self.add_suffix(self.state)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for setter in (body.set_margin_top, body.set_margin_bottom,
                       body.set_margin_start, body.set_margin_end):
            setter(12)
        self.progress = Gtk.ProgressBar()
        body.append(self.progress)
        self.detail = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.append(self.detail)
        actions = Gtk.Box(spacing=8)
        self.cancel = Gtk.Button(label="取消任务")
        self.cancel.connect("clicked", lambda *_: center.manager.cancel(self.task.id))
        actions.append(self.cancel)
        self.retry = Gtk.Button(label="重新执行可重试项")
        self.retry.connect("clicked", lambda *_: center.retry(self.record))
        actions.append(self.retry)
        self.retry_failed = Gtk.Button(label="仅重试失败／未完成项")
        self.retry_failed.connect("clicked", lambda *_: center.retry(self.record, failed_only=True))
        actions.append(self.retry_failed)
        body.append(actions)
        self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self.items)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(body)
        self.add_row(row)
        self._items_signature = None
        self.update()

    def update(self):
        if self.task is not None:
            self.record = snapshot(self.task)
        r = self.record
        active = self.task is not None and self.task.running
        phase = r["phase"] if active else r["status"]
        self.state.set_text(STATUS_LABELS.get(phase, phase))
        self.cancel.set_visible(active)
        self.retry.set_visible(not active)
        self.retry_failed.set_visible(not active)
        self.progress.set_visible(active)
        if active and self.task.frac is None:
            self.progress.pulse()
        elif active:
            self.progress.set_fraction(self.task.frac)
        total = fmt_size(r["total"]) if r["known"] else "总量未知"
        text = (f'源：' + "\n".join(r["paths"]) + f'\n目标：{r["dst_dir"]}\n'
                f'{"移动" if r["move"] else "复制"} · {r["done_files"]}/{r["files"]} 个文件 · '
                f'{fmt_size(r["bytes"])} / {total}')
        if r["note"] or r["error"]:
            text += "\n" + "\n".join(v for v in (r["note"], r["error"]) if v)
        text += "\n重试会重新扫描并确认冲突；目录按整个所选目录重试。"
        self.detail.set_text(text)
        signature = (active, tuple(r["items"].items()), tuple(r["item_errors"].items()))
        if signature == self._items_signature:
            return
        self._items_signature = signature
        while self.items.get_first_child() is not None:
            self.items.remove(self.items.get_first_child())
        for path in r["paths"]:
            name = path.rstrip("/").rsplit("/", 1)[-1]
            status = r["items"].get(name, "pending")
            box = Gtk.Box(spacing=8)
            label = Gtk.Label(label=f'{name}：{ITEM_LABELS.get(status, status)}',
                              xalign=0, hexpand=True, wrap=True)
            if name in r["item_errors"]:
                label.set_tooltip_text(r["item_errors"][name])
            box.append(label)
            if not active and status not in ("moved", "source_delete_failed"):
                button = Gtk.Button(label="重试此项")
                button.connect("clicked", lambda *_, n=name: self.center.retry(self.record, names={n}))
                box.append(button)
            self.items.append(box)


class TaskCenter(Adw.Dialog):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.manager = window.manager
        self.rows = {}
        self.set_title("任务中心")
        self.set_content_width(820)
        self.set_content_height(600)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        clear = Gtk.Button(label="清理已结束记录")
        clear.connect("clicked", lambda *_: self.manager.clear_finished())
        header.pack_end(clear)
        toolbar.add_top_bar(header)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in (root.set_margin_top, root.set_margin_bottom,
                       root.set_margin_start, root.set_margin_end):
            setter(12)
        self.summary = Gtk.Label(xalign=0)
        root.append(self.summary)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        root.append(self.listbox)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(root)
        toolbar.set_content(scroll)
        self.set_child(toolbar)
        self.manager.add_listener(self.sync)
        self.connect("closed", lambda *_: self.manager.remove_listener(self.sync))
        self.sync()

    def sync(self):
        current = {t.uid: t for t in self.manager.transfers}
        records = {r["uid"]: r for r in self.manager.history.records}
        records.update({uid: snapshot(t) for uid, t in current.items()})
        active = [r for uid, r in records.items() if uid in current and current[uid].running]
        finished = sorted((r for uid, r in records.items()
                           if uid not in current or not current[uid].running),
                          key=lambda r: r["created_at"], reverse=True)[:MAX_HISTORY]
        ordered = sorted(active, key=lambda r: r["created_at"], reverse=True) + finished
        wanted = {r["uid"] for r in ordered}
        for uid in list(self.rows):
            if uid not in wanted:
                self.listbox.remove(self.rows.pop(uid))
        for index, r in enumerate(ordered):
            row = self.rows.get(r["uid"])
            if row is None:
                row = TaskRow(self, r, current.get(r["uid"]))
                self.rows[r["uid"]] = row
                self.listbox.insert(row, index)
            row.update()
        self.summary.set_text(f"{len(active)} 个活动任务 · {len(finished)} 条已结束记录（最多保留 {MAX_HISTORY} 条）")

    def retry(self, record, names=None, failed_only=False):
        try:
            retry_record(self.manager, record, names, failed_only)
        except BackendError as error:
            self.window.toast(str(error), True)
        self.sync()
