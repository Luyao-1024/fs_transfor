"""传输面板: 底部可收起面板, 每条传输一行(方向/进度/速度/取消)."""
from __future__ import annotations

from gi.repository import GLib, Gtk, Pango

from .transfer import DIRECTIONS
from .util import fmt_size, fmt_speed

DONE_VISIBLE_SECONDS = 3.0
MAX_VISIBLE_NOTIFICATIONS = 5


class TransferRow(Gtk.ListBoxRow):
    def __init__(self, manager, t):
        super().__init__()
        self.manager = manager
        self.t = t
        self.add_css_class("transfer-notification")

        # 名称(多选时附 +N)
        names = [t.src.basename(p) for p in t.src_paths]
        title = ", ".join(names[:2])
        if len(names) > 2:
            title += f" 等 {len(names)} 项"
        sub = f"{t.src.label}  →  {t.dst.label}:{t.dst_dir}"

        self.icon = Gtk.Image(pixel_size=16)
        self.set_child(self._build(title, sub))
        self.update(t)

    def _build(self, title, sub):
        root = Gtk.Box(spacing=7)
        root.set_margin_top(4)
        root.set_margin_bottom(4)
        root.set_margin_start(8)
        root.set_margin_end(8)
        root.set_tooltip_text(sub)
        root.append(self.icon)

        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                           hexpand=True)
        t_lbl = Gtk.Label(label=title, xalign=0, hexpand=True)
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(t_lbl)

        progress_box = Gtk.Box(spacing=7)
        self.pb = Gtk.ProgressBar(hexpand=True)
        self.pb.set_valign(Gtk.Align.CENTER)
        self.pb.set_size_request(-1, 4)
        progress_box.append(self.pb)
        self.bytes_lbl = Gtk.Label(label="")
        self.bytes_lbl.add_css_class("dim-label")
        self.bytes_lbl.add_css_class("caption")
        progress_box.append(self.bytes_lbl)
        name_box.append(progress_box)
        root.append(name_box)

        self.status_lbl = Gtk.Label(label="")
        self.status_lbl.add_css_class("caption")
        root.append(self.status_lbl)

        self.cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        self.cancel_btn.add_css_class("flat")
        self.cancel_btn.set_tooltip_text("取消")
        self.cancel_btn.set_valign(Gtk.Align.CENTER)
        self.cancel_btn.connect("clicked", self._on_button_clicked)
        root.append(self.cancel_btn)

        return root

    def _on_button_clicked(self, *_args):
        if self.t.running:
            self.manager.cancel(self.t.id)
        else:
            self.manager.remove_finished(self.t.id)

    def update(self, t):
        label, icon = DIRECTIONS.get(t.direction, ("传输", "emblem-synchronizing-symbolic"))

        if t.status == "done":
            self.icon.set_from_icon_name("object-select-symbolic")
            self.pb.set_fraction(1.0)
            self.status_lbl.set_text(t.note or f"{label}完成 · {fmt_size(t.total_bytes)}")
            self.bytes_lbl.set_text("")
            self.cancel_btn.set_visible(False)
        elif t.status == "partial":
            self.icon.set_from_icon_name("dialog-warning-symbolic")
            err = t.error if len(t.error) <= 90 else t.error[:90] + "…"
            self.status_lbl.set_text(f"部分完成: {err or t.note}")
            if t.error:
                self.status_lbl.set_tooltip_text(t.error)
            self.status_lbl.add_css_class("warning")
            self.bytes_lbl.set_text("")
            self.cancel_btn.set_icon_name("window-close-symbolic")
            self.cancel_btn.set_tooltip_text("关闭")
            self.cancel_btn.set_visible(True)
        elif t.status == "cancelled":
            self.icon.set_from_icon_name("action-unavailable-symbolic")
            self.status_lbl.set_text("已取消")
            self.bytes_lbl.set_text("")
            self.cancel_btn.set_icon_name("window-close-symbolic")
            self.cancel_btn.set_tooltip_text("关闭")
            self.cancel_btn.set_visible(True)
        elif t.status == "error":
            self.icon.set_from_icon_name("dialog-warning-symbolic")
            err = t.error if len(t.error) <= 90 else t.error[:90] + "…"
            self.status_lbl.set_text(f"失败: {err}")
            self.status_lbl.set_tooltip_text(t.error)
            self.status_lbl.add_css_class("error")
            self.bytes_lbl.set_text("")
            self.cancel_btn.set_icon_name("window-close-symbolic")
            self.cancel_btn.set_tooltip_text("关闭")
            self.cancel_btn.set_visible(True)
        else:  # pending / running
            self.icon.set_from_icon_name(icon)
            frac = t.frac
            if frac is None:
                self.pb.pulse()
            else:
                self.pb.set_fraction(frac)
            speed = fmt_speed(t.speed) if t.speed > 0 else "…"
            if frac is not None:
                self.status_lbl.set_text(f"{speed} · {frac * 100:.0f}%")
            else:
                self.status_lbl.set_text(speed)
            if t.total_bytes > 0:
                self.bytes_lbl.set_text(f"{fmt_size(t.done_bytes)} / {fmt_size(t.total_bytes)}")
            else:
                self.bytes_lbl.set_text(fmt_size(t.done_bytes))
            self.cancel_btn.set_icon_name("process-stop-symbolic")
            self.cancel_btn.set_tooltip_text("取消")
            self.cancel_btn.set_visible(t.status == "running")


class TransferPanel(Gtk.Revealer):
    """底部传输面板: 由窗口的定时器驱动 tick()."""

    def __init__(self, manager):
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_reveal_child(False)
        self.manager = manager
        self._rows: dict[int, TransferRow] = {}
        self._visible_order: list[int] = []
        self._dismissed: set[int] = set()

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("transfer-stack")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(300)
        scrolled.set_child(self.listbox)
        self.set_child(scrolled)

    def tick(self) -> bool:
        self.manager.clear_expired_done(DONE_VISIBLE_SECONDS)
        transfers = list(self.manager.transfers)

        # 行同步
        alive = {t.id for t in transfers}
        self._dismissed.intersection_update(alive)
        for tid in list(self._rows):
            if tid not in alive:
                self.listbox.remove(self._rows.pop(tid))
                self._visible_order.remove(tid)
        for t in transfers:
            if t.id in self._dismissed:
                continue
            row = self._rows.get(t.id)
            if row is None:
                row = TransferRow(self.manager, t)
                self._rows[t.id] = row
                self._visible_order.append(t.id)
                self.listbox.append(row)
            row.update(t)

        # 新通知追加在底部；超过上限时，仅隐藏最上方最老的通知，
        # 后台传输不受影响。
        while len(self._visible_order) > MAX_VISIBLE_NOTIFICATIONS:
            tid = self._visible_order.pop(0)
            self.listbox.remove(self._rows.pop(tid))
            self._dismissed.add(tid)

        self.set_reveal_child(bool(self._rows))
        return GLib.SOURCE_CONTINUE
