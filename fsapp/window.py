"""主窗口: 标签页(Adw.TabView) + 双面板工作区 + 传输面板 + 会话记忆."""
from __future__ import annotations

import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import config
from .backend.local import LocalBackend
from .connect_dialog import ask_overwrite
from .connections import ConnectionHub
from .transfer import TransferManager
from .transfer_row import TransferPanel
from .util import fmt_speed
from .workspace import Workspace

APP_NAME = "FsTransfor"

CSS = b"""
.pane-drop {
    outline: 2px dashed @accent_color;
    outline-offset: -6px;
    background-color: alpha(@accent_color, 0.07);
}
.transfer-stack {
    background: transparent;
}
.transfer-notification {
    background-color: @window_bg_color;
    border: 1px solid alpha(@window_fg_color, 0.16);
    border-radius: 12px;
    box-shadow: 0 6px 18px alpha(black, 0.35);
    margin: 3px;
}
/* Native context menu: retain the theme's popup border and shadow. */
.fs-context-menu contents {
    padding: 5px 0px;
}
.fs-context-menu row {
    min-height: 32px;
    padding: 0 12px;
}
.fs-context-menu list {
    background: transparent;
}
.fs-context-menu row:hover {
    background-color: alpha(@window_fg_color, 0.08);
}
.fs-context-menu row.menu-separator {
    min-height: 0;
    padding: 0 12px;
    margin: 0;
}
"""


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1150, 720)

        self.settings = config.load_settings()
        self.show_hidden = self.settings.get("show_hidden", False)
        self.local_backend = LocalBackend()
        self.manager = TransferManager()
        self.manager.ask_overwrite = self._ask_overwrite
        self.manager.add_listener(self._on_manager_changed)
        self.hub = ConnectionHub(self)
        self._drag_payloads: dict = {}
        self._clip: tuple | None = None  # (backend, src_dir, paths, 'copy'|'cut')
        self._restored = False

        self.workspaces: list[Workspace] = []
        self._ws_counter = 0

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # ---- 顶栏 ----
        header = Gtk.HeaderBar()
        self.speed_chip = Gtk.Label(label="")
        self.speed_chip.add_css_class("dim-label")
        self.speed_chip.set_visible(False)
        header.pack_start(self.speed_chip)

        new_tab_btn = Gtk.Button(icon_name="tab-new-symbolic")
        new_tab_btn.set_tooltip_text("新标签页 (Ctrl+T)")
        new_tab_btn.set_action_name("win.new-tab")
        header.pack_start(new_tab_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.set_menu_model(self._build_menu())
        header.pack_end(menu_btn)

        # ---- 标签页 + 右下角通知式传输栈 ----
        self.tab_view = Adw.TabView()
        self.tab_view.set_vexpand(True)
        self.tab_view.set_hexpand(True)
        self.tab_view.connect("close-page", self._on_close_page)

        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)  # 单标签时自动隐藏

        self.transfer_panel = TransferPanel(self.manager)
        self.transfer_panel.set_halign(Gtk.Align.END)
        self.transfer_panel.set_valign(Gtk.Align.END)
        self.transfer_panel.set_margin_end(12)
        self.transfer_panel.set_margin_bottom(12)
        self.transfer_panel.set_size_request(420, -1)

        self.content_overlay = Gtk.Overlay()
        self.content_overlay.set_child(self.tab_view)
        self.content_overlay.add_overlay(self.transfer_panel)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.tab_bar)
        toolbar.set_content(self.content_overlay)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar)
        self._context_menu = None
        self.set_content(self.toast_overlay)

        self._install_actions()
        self._install_shortcuts()

        GLib.timeout_add(300, self._on_tick)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------
    # 标签页
    # ------------------------------------------------------------------
    def new_workspace(self, left_state: dict | None = None,
                      right_state: dict | None = None, select: bool = True) -> Workspace:
        self._ws_counter += 1
        ws = Workspace(self, f"w{self._ws_counter}")
        self.workspaces.append(ws)
        page = self.tab_view.append(ws)
        page.set_title(ws.title())
        page.set_tooltip("FsTransfor 工作区")
        if select:
            self.tab_view.set_selected_page(page)

        for side, st in (("left", left_state), ("right", right_state)):
            pane = getattr(ws, side)
            st = st or {}
            if (st.get("type") == "server" and self.settings.get("auto_connect", True)
                    and st.get("server_id")):
                cfg = config.find_server(st.get("server_id"))
                if cfg:
                    pane.connect_server_async(cfg, path=st.get("path"))
                    continue
            pane.connect_local(st.get("path") if st.get("type") == "local" else None)
        return ws

    def _on_close_page(self, view, page):
        self.close_context_menu()
        ws = page.get_child()
        if ws in self.workspaces:
            self.workspaces.remove(ws)
        ws.close()
        # 不手动调用 close_page_finish: Adw 默认处理器会完成页面移除,
        # 手动再调会双重移除(CRITICAL). 关闭中的页面此刻仍在列表中,
        # n_pages==1 即它是最后一个 → 自动补一个新标签
        if view.get_n_pages() == 1:
            self.new_workspace()
        self._save_session()

    @property
    def active_workspace(self) -> Workspace | None:
        page = self.tab_view.get_selected_page()
        if page is not None:
            return page.get_child()
        return self.workspaces[0] if self.workspaces else None

    @property
    def left(self):
        ws = self.active_workspace
        return ws.left if ws else None

    @property
    def right(self):
        ws = self.active_workspace
        return ws.right if ws else None

    def get_pane(self, pane_id: str):
        for ws in self.workspaces:
            for pane in (ws.left, ws.right):
                if pane.pane_id == pane_id:
                    return pane
        return None

    # ------------------------------------------------------------------
    # 菜单与动作
    # ------------------------------------------------------------------
    def _build_menu(self):
        menu = Gio.Menu.new()
        s1 = Gio.Menu.new()
        it = Gio.MenuItem.new("显示隐藏文件")
        it.set_action_and_target_value("win.show-hidden", None)
        s1.append_item(it)
        it2 = Gio.MenuItem.new("启动时自动重连上次会话")
        it2.set_action_and_target_value("win.auto-connect", None)
        s1.append_item(it2)
        menu.append_section(None, s1)
        s2 = Gio.Menu.new()
        s2.append("新建标签页", "win.new-tab")
        s2.append("关闭当前标签页", "win.close-tab")
        menu.append_section(None, s2)
        s3 = Gio.Menu.new()
        s3.append("关于", "win.about")
        s3.append("退出", "app.quit")
        menu.append_section(None, s3)
        return menu

    def _install_actions(self):
        a = Gio.SimpleAction.new_stateful(
            "show-hidden", None, GLib.Variant.new_boolean(self.show_hidden))
        a.connect("change-state", self._on_toggle_hidden)
        self.add_action(a)

        b = Gio.SimpleAction.new_stateful(
            "auto-connect", None,
            GLib.Variant.new_boolean(self.settings.get("auto_connect", True)))
        b.connect("change-state", self._on_toggle_autoconnect)
        self.add_action(b)

        for name, cb in (("new-tab", self._on_new_tab), ("close-tab", self._on_close_tab)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

    def _install_shortcuts(self):
        for accel, name in (("<Control>t", "win.new-tab"),
                            ("<Control>w", "win.close-tab")):
            self.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.NamedAction.new(name)))

    def _on_toggle_hidden(self, action, value):
        action.set_state(value)
        self.show_hidden = value.get_boolean()
        for ws in self.workspaces:
            ws.left.refresh()
            ws.right.refresh()
        self._save_session()

    def _on_toggle_autoconnect(self, action, value):
        action.set_state(value)
        self.settings["auto_connect"] = value.get_boolean()
        self._save_session()

    def _on_new_tab(self, *args):
        self.new_workspace()

    def _on_close_tab(self, *args):
        page = self.tab_view.get_selected_page()
        if page is not None:
            self.tab_view.close_page(page)

    def _on_about(self, *args):
        dlg = Adw.AboutDialog.new()
        dlg.set_application_name(APP_NAME)
        dlg.set_comments("GTK4 远程 SSH 文件传输器: 多标签双面板, 拖拽上传下载, 实时速度显示")
        dlg.set_version("0.2.0")
        dlg.present(self)

    def _on_close(self, *args):
        self.close_context_menu()
        self._save_session()
        return False

    # ------------------------------------------------------------------
    # 面板协调
    # ------------------------------------------------------------------
    def stage_drag_payload(self, pane_id, backend, paths):
        self._drag_payloads[pane_id] = (backend, paths)

    def consume_drag_payload(self, pane_id):
        return self._drag_payloads.pop(pane_id, None)

    # ---- 内部剪贴板(Ctrl+C/X/V) ----
    def set_clipboard(self, backend, src_dir, paths, mode):
        self._clip = (backend, src_dir, paths, mode)

    def get_clipboard(self):
        return self._clip

    def clear_clipboard(self):
        self._clip = None

    # ---- 会话记忆 ----
    def on_pane_connected(self, pane):
        self._save_session()

    def on_pane_disconnected(self, pane):
        self._save_session()

    def on_pane_path_changed(self, pane):
        self._save_session()

    def on_bookmarks_changed(self):
        for ws in self.workspaces:
            ws.left._sync_bookmark_button()
            ws.right._sync_bookmark_button()
        self._save_session()

    @staticmethod
    def _pane_state(pane):
        if pane.server_cfg:
            return {"type": "server",
                    "server_id": pane.server_cfg.get("id"),
                    "path": pane.cwd}
        return {"type": "local", "path": pane.cwd}

    def _save_session(self):
        if not self._restored:
            return
        s = self.settings
        s["show_hidden"] = self.show_hidden
        s["tabs"] = [{"left": self._pane_state(ws.left),
                      "right": self._pane_state(ws.right)}
                     for ws in self.workspaces]
        try:
            selected = self.tab_view.get_selected_page()
            s["active_tab"] = self.tab_view.get_position(selected) if selected else 0
        except Exception:
            s["active_tab"] = 0
        # 同步标签标题
        for ws in self.workspaces:
            page = self.tab_view.get_page(ws)
            if page is not None:
                page.set_title(ws.title())
        config.save_settings(s)

    def restore_session(self):
        """窗口显示后调用: 按记忆恢复标签页."""
        s = self.settings
        tabs = s.get("tabs") or []
        if not tabs:
            self.new_workspace()
        else:
            active = s.get("active_tab") or 0
            active = max(0, min(int(active), len(tabs) - 1))
            for i, tab in enumerate(tabs):
                self.new_workspace(tab.get("left"), tab.get("right"),
                                   select=(i == active))
        self._restored = True

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 原生右键菜单
    # ------------------------------------------------------------------
    def open_context_menu(self, menu, anchor, x, y):
        """以点击位置为锚点, 让 GTK/合成器按屏幕可用区域定位."""
        self.close_context_menu()
        menu.set_parent(anchor)
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(x), int(y)
        rect.width = rect.height = 1
        menu.set_pointing_to(rect)
        menu.connect("closed", self._on_context_menu_closed)
        self._context_menu = menu
        menu.popup()

    def close_context_menu(self):
        if self._context_menu is not None:
            self._context_menu.popdown()

    def _on_context_menu_closed(self, menu):
        if self._context_menu is menu:
            self._context_menu = None
        menu.unparent()

    # Toast 与阻塞对话框
    # ------------------------------------------------------------------
    def toast(self, message: str, error: bool = False):
        t = Adw.Toast.new(("⚠ " if error else "") + message)
        t.set_timeout(4)
        self.toast_overlay.add_toast(t)

    def blocking_dialog(self, build):
        """从工作线程调用: 在主线程弹一个对话框并阻塞取回结果(≤10 分钟)."""
        ev = threading.Event()
        holder = {}

        def done(value):
            holder["v"] = value
            ev.set()

        def show():
            build(done)

        GLib.idle_add(show)
        if not ev.wait(timeout=600):
            return None
        return holder.get("v")

    def _ask_overwrite(self, names):
        """TransferManager 回调(worker 线程): 同名覆盖确认, 阻塞等待."""
        return self.blocking_dialog(
            lambda done: ask_overwrite(self, names, done))

    # ------------------------------------------------------------------
    # 传输事件
    # ------------------------------------------------------------------
    def _on_manager_changed(self):
        """TransferManager 通知(主线程): 错误 Toast + 完成后刷新目标/源面板."""
        for t in self.manager.transfers:
            if t.status in ("error", "partial") and t.error and not t.toasted:
                t.toasted = True
                prefix = "传输失败" if t.status == "error" else "部分完成"
                msg = t.error if len(t.error) <= 120 else t.error[:120] + "…"
                self.toast(f"{prefix}: {msg}", error=True)
            if t.status in ("done", "partial", "cancelled", "error") and not t.refreshed:
                t.refreshed = True
                cwds = {t.dst_dir}
                if t.move:
                    # 移动后源面板也要刷新
                    for name, st in dict(t.item_status).items():
                        if st == "committed":
                            p = t.top_srcs.get(name)
                            if p:
                                cwds.add(t.src.parent(p))
                for ws in self.workspaces:
                    for pane in (ws.left, ws.right):
                        if pane.cwd in cwds:
                            pane.refresh()

    def _on_tick(self) -> bool:
        self.transfer_panel.tick()
        self._update_speed_chip()
        return GLib.SOURCE_CONTINUE

    def _update_speed_chip(self):
        up = down = other = 0
        active = False
        for t in self.manager.transfers:
            if t.running:
                active = True
                if t.direction == "upload":
                    up += t.speed
                elif t.direction == "download":
                    down += t.speed
                else:
                    other += t.speed
        if not active:
            self.speed_chip.set_visible(False)
            return
        parts = []
        if up:
            parts.append(f"↑ {fmt_speed(up)}")
        if down:
            parts.append(f"↓ {fmt_speed(down)}")
        if other:
            parts.append(f"⇅ {fmt_speed(other)}")
        self.speed_chip.set_text("   ".join(parts))
        self.speed_chip.set_visible(True)
