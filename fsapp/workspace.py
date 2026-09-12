"""一个标签页的工作区: 左右双面板."""
from __future__ import annotations

from gi.repository import Gtk

from .pane import FilePane


class Workspace(Gtk.Box):
    def __init__(self, window, wid: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.wid = wid
        self.closed = False
        self.active_pane = None

        self.left = FilePane(window, self, f"{wid}-left")
        self.right = FilePane(window, self, f"{wid}-right")
        self.active_pane = self.left

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_start_child(self.left)
        self.paned.set_end_child(self.right)
        self.paned.set_resize_start_child(True)
        self.paned.set_resize_end_child(True)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        self.paned.set_wide_handle(True)
        self.paned.set_vexpand(True)
        try:
            self.paned.set_position(window.get_property("default-width") // 2)
        except Exception:
            self.paned.set_position(575)
        self.append(self.paned)
        self._install_pane_shortcuts()

    def other(self, pane) -> FilePane:
        return self.right if pane is self.left else self.left

    def activate_pane(self, pane: FilePane):
        """记录快捷键目标，并清除另一侧可能造成误操作的旧选区."""
        self.active_pane = pane
        other = self.other(pane)
        if other.selection.get_selection().get_size():
            other.selection.unselect_all()

    def _install_pane_shortcuts(self):
        """在工作区统一分发快捷键，避免焦点残留时调用错误面板动作."""
        actions = {
            "copy": "_action_copy",
            "cut": "_action_cut",
            "paste": "_action_paste",
            "delete": "_action_delete",
            "rename": "_action_rename",
            "back": "go_back",
            "forward": "go_forward",
            "parent": "go_parent",
            "home": "go_home",
            "location": "focus_path",
            "filter": "show_filter",
            "refresh": "refresh",
        }
        for name, method in actions.items():
            action = f"{self.wid}.{name}"
            self.install_action(
                action, None,
                lambda *args, _method=method: self._run_pane_action(_method))

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.MANAGED)
        for accel, name in (
            ("<Primary>c", "copy"),
            ("<Primary>x", "cut"),
            ("<Primary>v", "paste"),
            ("Delete", "delete"),
            ("F2", "rename"),
            ("<Alt>Left", "back"),
            ("<Alt>Right", "forward"),
            ("<Alt>Up", "parent"),
            ("<Alt>Home", "home"),
            ("<Primary>l", "location"),
            ("<Primary>f", "filter"),
            ("F5", "refresh"),
        ):
            shortcuts.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.NamedAction.new(f"{self.wid}.{name}")))
        self.add_controller(shortcuts)

    def _run_pane_action(self, method):
        pane = self.active_pane or self.left
        getattr(pane, method)()

    def title(self) -> str:
        def side(pane: FilePane) -> str:
            if pane.server_cfg is None:
                return "本地"
            state = pane.stack.get_visible_child_name()
            if state == "disconnected":
                return "未连接"
            if pane.suspended:
                return (pane.server_cfg.get("name") or pane.backend.label) + "（已断开）"
            return pane.server_cfg.get("name") or pane.backend.label
        return f"{side(self.left)} ↔ {side(self.right)}"

    def close(self):
        """标签页关闭: 释放连接引用与挂起的连接请求."""
        self.closed = True
        for pane in (self.left, self.right):
            pane._cancel_browse()
            if pane.server_cfg is not None and not pane.backend.is_local:
                try:
                    self.window.hub.release(pane, pane.backend)
                except Exception:
                    pass
            try:
                self.window.hub.cancel_pending(pane)
            except Exception:
                pass
