"""单个文件面板: 连接管理 + 路径栏 + 文件列表 + 拖拽 + 右键菜单 + 快捷键.

每个面板归属一个 Workspace(标签页), 通过 window 与传输管理器、连接池交互.
所有阻塞操作在后台线程执行, 结果经 GLib.idle_add 回主线程.
"""
from __future__ import annotations

import datetime
import json
import threading

from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from . import config
from .backend.base import BackendError, CancelledError
from .backend.local import LocalBackend
from .connect_dialog import (ConnectDialog, TextPromptDialog, ask_delete,
                             ask_delete_local, ask_permanent_delete)
from .file_item import FileItem
from .util import fmt_size


class ContextMenu(Gtk.Popover):
    """原生右键弹出菜单, 由窗口系统处理屏幕边缘避让."""

    def __init__(self, pane):
        super().__init__(has_arrow=False, autohide=True)
        self.set_position(Gtk.PositionType.BOTTOM)
        self.set_halign(Gtk.Align.START)
        self.add_css_class("fs-context-menu")
        self.pane = pane


class FilePane(Gtk.Box):
    def __init__(self, window, workspace, pane_id: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.workspace = workspace
        self.pane_id = pane_id            # 全局唯一: "<工作区>-<left|right>"
        self.backend = LocalBackend()
        self.server_cfg: dict | None = None
        self.cwd = self.backend.home()
        self._history = [self.cwd]
        self._history_index = 0
        self._recent_paths = [self.cwd]
        self._filter_text = ""
        self._token = 0
        self._scan_cancel = threading.Event()
        self.suspended = False  # 连接暂停/断开: 文件界面保留并显示背景提示
        self._cell_items = {}  # 单元格内容部件 → FileItem (bind 时记录)
        self._interactive_rows = set()  # 已安装右键/拖动控制器的完整高亮行
        self._menu_popover = None  # 最近打开的原生右键菜单

        self._build_toolbar()
        self._build_stack()
        self._build_view()
        self._build_dnd()
        self._install_actions()

        self._set_state("files")
        self.navigate(self.backend.home())

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_toolbar(self):
        bar = Gtk.Box(spacing=6)
        for m in ("start", "end", "top", "bottom"):
            getattr(bar, f"set_margin_{m}")(6)

        self.connect_btn = Gtk.MenuButton(label="连接")
        self.connect_btn.set_always_show_arrow(True)
        self.connect_btn.set_tooltip_text("连接服务器 / 切换本地")
        popover = Gtk.Popover()
        popover.connect("show", self._rebuild_connect_popover)
        self.connect_btn.set_popover(popover)
        bar.append(self.connect_btn)

        # 注意: network-off-symbolic 在 Adwaita 主题中不存在, 会渲染成
        # "图片缺失"占位方块(被误认成红色方框); 改用暂停图标表达"挂起连接".
        # 点击行为见 _toggle_suspend: 暂停 ⇄ 恢复(重新连回原目录).
        self.disconnect_btn = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.disconnect_btn.set_tooltip_text("暂停连接(可恢复)")
        self.disconnect_btn.set_visible(False)
        self.disconnect_btn.connect("clicked", lambda *_: self._toggle_suspend())
        bar.append(self.disconnect_btn)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        bar.append(self.spinner)

        self.path_entry = Gtk.Entry(hexpand=True)
        self.path_entry.set_tooltip_text("输入路径后回车跳转")
        self.path_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-refresh-symbolic")
        self.path_entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "刷新")
        self.path_entry.connect("activate", self._on_path_activate)
        self.path_entry.connect("icon-press", self._on_entry_icon_press)
        bar.append(self.path_entry)

        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_btn.set_tooltip_text("后退 (Alt+Left)")
        self.back_btn.connect("clicked", lambda *_: self.go_back())
        bar.insert_child_after(self.back_btn, self.spinner)

        self.forward_btn = Gtk.Button(icon_name="go-next-symbolic")
        self.forward_btn.set_tooltip_text("前进 (Alt+Right)")
        self.forward_btn.connect("clicked", lambda *_: self.go_forward())
        bar.insert_child_after(self.forward_btn, self.back_btn)

        self.recent_btn = Gtk.MenuButton(icon_name="document-open-recent-symbolic")
        self.recent_btn.set_tooltip_text("最近路径")
        recent_popover = Gtk.Popover()
        recent_popover.connect("show", self._rebuild_recent_popover)
        self.recent_btn.set_popover(recent_popover)
        bar.insert_child_after(self.recent_btn, self.forward_btn)

        self.bookmark_btn = Gtk.MenuButton(icon_name="non-starred-symbolic")
        self.bookmark_btn.set_tooltip_text("路径收藏")
        bookmark_popover = Gtk.Popover()
        bookmark_popover.connect("show", self._rebuild_bookmark_popover)
        self.bookmark_btn.set_popover(bookmark_popover)
        bar.append(self.bookmark_btn)

        up = Gtk.Button(icon_name="go-up-symbolic")
        up.set_tooltip_text("上一级目录")
        up.connect("clicked", lambda *_: self.go_parent())
        bar.append(up)
        self.up_btn = up

        home = Gtk.Button(icon_name="user-home-symbolic")
        home.set_tooltip_text("主目录")
        home.connect("clicked", lambda *_: self.go_home())
        bar.append(home)
        self.home_btn = home

        self.filter_btn = Gtk.ToggleButton(icon_name="edit-find-symbolic")
        self.filter_btn.set_tooltip_text("筛选当前目录 (Ctrl+F)")
        self.filter_btn.connect("toggled", self._on_filter_toggled)
        bar.append(self.filter_btn)

        self.append(bar)

        self.filter_revealer = Gtk.Revealer()
        self.filter_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        filter_box = Gtk.Box(spacing=6, margin_start=6, margin_end=6,
                             margin_bottom=6)
        self.filter_entry = Gtk.SearchEntry(hexpand=True,
                                            placeholder_text="筛选当前目录中的名称")
        self.filter_entry.set_tooltip_text("只筛选当前列表，不改变所在目录")
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        self.filter_entry.connect("stop-search", lambda *_: self.hide_filter())
        filter_box.append(self.filter_entry)
        self.filter_revealer.set_child(filter_box)
        self.append(self.filter_revealer)

    def _build_stack(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.add_named(self._page_disconnected(), "disconnected")
        self.stack.add_named(self._page_busy(), "busy")
        self.append(self.stack)

    def _page_busy(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.set_size_request(36, 36)
        self.busy_spinner = spinner
        self.busy_label = Gtk.Label(label="")
        self.busy_label.add_css_class("dim-label")
        box.append(spinner)
        box.append(self.busy_label)
        return box

    def _page_disconnected(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        icon = Gtk.Image(icon_name="network-server-symbolic")
        icon.set_pixel_size(64)
        self.disc_label = Gtk.Label(label="未连接远程主机")
        self.disc_label.add_css_class("dim-label")
        self.reconnect_btn = Gtk.Button(label="重新连接")
        self.reconnect_btn.add_css_class("suggested-action")
        self.reconnect_btn.connect("clicked", lambda *_: self._reconnect())
        self.new_conn_btn = Gtk.Button(label="连接其他服务器…")
        self.new_conn_btn.connect(
            "clicked",
            lambda *_: ConnectDialog(self.window, self._on_connect_form_done))
        box.append(icon)
        box.append(self.disc_label)
        box.append(self.reconnect_btn)
        box.append(self.new_conn_btn)
        return box

    # ------------------------------------------------------------------
    def _build_view(self):
        self.store = Gio.ListStore.new(FileItem)
        self.name_filter = Gtk.CustomFilter.new(self._filter_item)
        self.filter_model = Gtk.FilterListModel(model=self.store,
                                                filter=self.name_filter)
        self.sort_model = Gtk.SortListModel(model=self.filter_model)
        self.selection = Gtk.MultiSelection(model=self.sort_model)
        self.selection.connect("selection-changed", self._on_selection_changed)
        self.view = Gtk.ColumnView(model=self.selection)
        self.view.set_enable_rubberband(True)
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        self.view.connect("notify::sorter", self._on_view_sorter)

        self.view.append_column(self._col_name())
        self.view.append_column(self._col_text("大小", 90, "size", self._text_size, 1.0, "numeric"))
        self.view.append_column(self._col_text("修改时间", 140, "mtime", self._text_mtime, 0.0, "numeric"))
        self.view.append_column(self._col_text("权限", 100, None, self._text_perms, 0.0, "monospace"))
        self.sort_model.set_sorter(self._make_sorter("name"))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.view)

        self.empty_revealer = Gtk.Revealer()
        self.empty_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.empty_revealer.set_can_target(False)
        empty_box = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.empty_label = Gtk.Label(label="此目录为空")
        self.empty_label.add_css_class("dim-label")
        empty_box.append(self.empty_label)
        self.empty_revealer.set_child(empty_box)

        # 连接暂停/断开的背景提示: 文件列表保留(清空条目), 覆盖提示词
        self.suspend_revealer = Gtk.Revealer()
        self.suspend_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.suspend_revealer.set_can_target(False)
        self.suspend_revealer.set_reveal_child(False)
        hint_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        hint_icon = Gtk.Image(icon_name="network-offline-symbolic")
        hint_icon.set_pixel_size(40)
        hint_icon.add_css_class("dim-label")
        self.suspend_title = Gtk.Label(label="连接已暂停")
        self.suspend_title.add_css_class("title-3")
        self.suspend_title.add_css_class("dim-label")
        self.suspend_detail = Gtk.Label(label="")
        self.suspend_detail.add_css_class("caption")
        self.suspend_detail.add_css_class("dim-label")
        hint_box.append(hint_icon)
        hint_box.append(self.suspend_title)
        hint_box.append(self.suspend_detail)
        self.suspend_revealer.set_child(hint_box)

        overlay = Gtk.Overlay()
        overlay.set_child(scrolled)
        overlay.add_overlay(self.empty_revealer)
        overlay.add_overlay(self.suspend_revealer)
        self.stack.add_named(overlay, "files")

        # 双击/回车进入目录: ColumnView 内建 activate 信号.
        # (自建视图级单击手势收不到事件: 行部件内部的 GestureClick 会先接管序列)
        self.view.connect("activate", self._on_row_activated)
        # 左键点击文件列表空白区域时清除旧选区。GestureClick 在发生拖动后会
        # 取消 click 序列，因此不会把橡皮筋框选的结果再次清空。
        lc = Gtk.GestureClick()
        lc.set_button(1)
        lc.connect("released", self._on_view_primary_released)
        self.view.add_controller(lc)
        self._view_primary_gesture = lc
        # 右键菜单: 空白区域(视图层); 行上的右键由单元格内容自带手势处理
        rc = Gtk.GestureClick()
        rc.set_button(3)
        # 必须等按键释放后再弹出；若在 pressed 阶段弹出，同一次 release
        # 可能命中新出现的第一项菜单，导致未点击菜单就直接开始传输。
        rc.connect("released", self._on_view_context_released)
        self.view.add_controller(rc)
        self._view_context_gesture = rc

    # ---- 列 ----
    def _col_name(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._name_setup)
        factory.connect("bind", self._name_bind)
        col = Gtk.ColumnViewColumn(title="名称")
        col.set_factory(factory)
        col.set_expand(True)
        col.set_sorter(self._make_sorter("name"))
        return col

    def _name_setup(self, factory, item):
        box = Gtk.Box(spacing=8)
        img = Gtk.Image()
        img.set_pixel_size(16)
        lbl = Gtk.Label(xalign=0, hexpand=True)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(img)
        box.append(lbl)
        item.set_child(box)

    def _name_bind(self, factory, item):
        e = item.get_item().entry
        box = item.get_child()
        self._bind_cell_item(item)
        img = box.get_first_child()
        lbl = img.get_next_sibling()
        if e.is_dir:
            img.set_from_icon_name("folder")
        else:
            ct, _ = Gio.content_type_guess(e.name, b"")
            img.set_from_gicon(Gio.content_type_get_icon(ct))
        lbl.set_text(e.name)

    def _col_text(self, title, width, sorter_key, bind, xalign, css):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._text_setup, xalign, css)
        factory.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title)
        col.set_factory(factory)
        col.set_fixed_width(width)
        if sorter_key:
            col.set_sorter(self._make_sorter(sorter_key))
        return col

    def _text_setup(self, factory, item, xalign, css):
        lbl = Gtk.Label(xalign=xalign)
        if css:
            lbl.add_css_class(css)
        item.set_child(lbl)

    def _text_size(self, factory, item):
        e = item.get_item().entry
        self._bind_cell_item(item)
        item.get_child().set_text("—" if e.is_dir else fmt_size(e.size))

    def _text_mtime(self, factory, item):
        e = item.get_item().entry
        self._bind_cell_item(item)
        if e.mtime > 0:
            text = datetime.datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M")
        else:
            text = "—"
        item.get_child().set_text(text)

    def _text_perms(self, factory, item):
        e = item.get_item().entry
        self._bind_cell_item(item)
        item.get_child().set_text(e.perms[1:] if len(e.perms) > 1 else e.perms)

    def _bind_cell_item(self, list_item):
        """绑定 FileItem 到完整高亮行，使高亮区域全部可以拖动."""
        child = list_item.get_child()
        file_item = list_item.get_item()
        self._cell_items[child] = file_item
        cell = child.get_parent()
        if cell is None:
            return
        self._cell_items[cell] = file_item
        row = cell.get_parent()
        if row is None:
            return
        self._cell_items[row] = file_item
        if row not in self._interactive_rows:
            self._interactive_rows.add(row)
            self._attach_cell_menu(row)
            self._attach_cell_drag(row)

    # ---- 排序(目录永远在前) ----
    def _make_sorter(self, key):
        def cmp(a, b, *_user_data):
            ea, eb = a.entry, b.entry
            if ea.is_dir != eb.is_dir:
                return -1 if ea.is_dir else 1
            if key == "size":
                return (ea.size > eb.size) - (ea.size < eb.size)
            if key == "mtime":
                return (ea.mtime > eb.mtime) - (ea.mtime < eb.mtime)
            la, lb = ea.name.lower(), eb.name.lower()
            return (la > lb) - (la < lb)
        return Gtk.CustomSorter.new(cmp)

    def _on_view_sorter(self, *args):
        s = self.view.get_sorter()
        if s is not None:
            self.sort_model.set_sorter(s)

    # ==================================================================
    # 列表加载
    # ==================================================================
    def navigate(self, path: str, record_history: bool = True):
        path = self.backend.normpath(path)
        if path == self.cwd:
            self.refresh()
            return
        self._clear_filter()
        self.cwd = path
        if record_history:
            self._record_navigation(path)
        else:
            self._remember_recent(path)
        self._sync_navigation_buttons()
        self.refresh()

    def _reset_navigation(self, path: str):
        """切换后端时建立独立历史，避免跨服务器复用无效路径。"""
        path = self.backend.normpath(path)
        self._clear_filter()
        self.cwd = path
        self._history = [path]
        self._history_index = 0
        self._recent_paths = [path]
        self._sync_navigation_buttons()
        self.refresh()

    def _record_navigation(self, path: str):
        if self._history_index < len(self._history) - 1:
            self._history = self._history[:self._history_index + 1]
        if not self._history or self._history[-1] != path:
            self._history.append(path)
            if len(self._history) > 50:
                self._history.pop(0)
        self._history_index = len(self._history) - 1
        self._remember_recent(path)

    def _remember_recent(self, path: str):
        self._recent_paths = [p for p in self._recent_paths if p != path]
        self._recent_paths.insert(0, path)
        del self._recent_paths[20:]

    def go_back(self):
        if self.suspended or self._history_index <= 0:
            return
        self._history_index -= 1
        self.navigate(self._history[self._history_index], record_history=False)

    def go_forward(self):
        if self.suspended or self._history_index >= len(self._history) - 1:
            return
        self._history_index += 1
        self.navigate(self._history[self._history_index], record_history=False)

    def go_parent(self):
        if not self.suspended:
            self.navigate(self.backend.parent(self.cwd))

    def go_home(self):
        if not self.suspended:
            self.navigate(self.backend.home())

    def focus_path(self):
        if self.path_entry.get_sensitive():
            self.workspace.activate_pane(self)
            self.path_entry.grab_focus()
            self.path_entry.select_region(0, -1)

    def show_filter(self):
        if not self.filter_btn.get_sensitive():
            return
        self.workspace.activate_pane(self)
        self.filter_btn.set_active(True)
        self.filter_entry.grab_focus()

    def hide_filter(self):
        self._clear_filter()
        self.filter_btn.set_active(False)
        self.view.grab_focus()

    def _on_filter_toggled(self, button):
        visible = button.get_active()
        self.filter_revealer.set_reveal_child(visible)
        if visible:
            self.filter_entry.grab_focus()
        else:
            self._clear_filter()

    def _on_filter_changed(self, entry):
        text = entry.get_text().strip().casefold()
        if text == self._filter_text:
            return
        # 位置会因过滤而重排；先清空可确保隐藏条目不参与后续操作。
        self.selection.unselect_all()
        self._filter_text = text
        self.name_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._sync_empty_state()

    def _clear_filter(self):
        if not hasattr(self, "filter_entry"):
            return
        if self.filter_entry.get_text():
            self.filter_entry.set_text("")
        if self._filter_text:
            self.selection.unselect_all()
            self._filter_text = ""
            self.name_filter.changed(Gtk.FilterChange.DIFFERENT)
            self._sync_empty_state()

    def _filter_item(self, item):
        return not self._filter_text or self._filter_text in item.entry.name.casefold()

    def _sync_empty_state(self):
        filtered_empty = self.sort_model.get_n_items() == 0
        has_unfiltered = self.store.get_n_items() > 0
        self.empty_label.set_text(
            "没有匹配的项目" if self._filter_text and has_unfiltered else "此目录为空")
        self.empty_revealer.set_reveal_child(filtered_empty)

    def _sync_navigation_buttons(self):
        if not hasattr(self, "back_btn"):
            return
        usable = (self.stack.get_visible_child_name() == "files"
                  and not self.suspended)
        self.back_btn.set_sensitive(usable and self._history_index > 0)
        self.forward_btn.set_sensitive(
            usable and self._history_index < len(self._history) - 1)
        self.recent_btn.set_sensitive(usable and len(self._recent_paths) > 1)

    def refresh(self):
        if self.stack.get_visible_child_name() != "files" or self.suspended:
            return
        self._cancel_browse()
        token = self._token
        path = self.cwd
        self.spinner.set_visible(True)
        self.spinner.start()
        backend = self.backend
        threading.Thread(target=self._load, args=(backend, token, path, self._scan_cancel),
                         daemon=True).start()

    def _cancel_browse(self):
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        self._token += 1
        self.spinner.stop()
        self.spinner.set_visible(False)

    def _load(self, backend, token, path, cancel_event):
        try:
            entries = list(backend.iter_dir(path, cancel_event))
        except CancelledError:
            return
        except BackendError as e:
            dead = (not backend.is_local) and backend.dead
            GLib.idle_add(self._load_failed, token, path, str(e),
                          backend if dead else None)
            return
        GLib.idle_add(self._load_done, token, path, entries)

    def _load_done(self, token, path, entries):
        if token != self._token:
            return False
        self.spinner.stop()
        self.spinner.set_visible(False)
        items = [FileItem(e) for e in entries
                 if self.window.show_hidden or not e.name.startswith(".")]
        self.store.splice(0, self.store.get_n_items(), items)
        self.path_entry.set_text(path)
        self.cwd = path
        self._sync_empty_state()
        self._sync_navigation_buttons()
        self._sync_bookmark_button()
        self.window.on_pane_path_changed(self)
        return False

    def _load_failed(self, token, path, msg, dead_backend):
        if token != self._token:
            return False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.window.toast(f"无法打开 {path}: {msg}", error=True)
        if dead_backend is not None:
            # 通知连接池: 该连接的所有使用面板一起进入断开状态
            self.window.hub.mark_dead(dead_backend, msg)
            if not self.suspended:
                # 本面板可能不在池使用方列表(如刚恢复): 也要进入断开提示
                self.connection_lost_ui(msg)
        return False

    # ==================================================================
    # 连接管理(经由 window.hub 连接池)
    # ==================================================================
    def connect_local(self, path: str | None = None):
        self.window.hub.cancel_pending(self)
        self._cancel_browse()
        self.suspended = False
        self._hide_suspend_hint()
        if self.server_cfg is not None and not self.backend.is_local:
            self.window.hub.release(self, self.backend)
        self.backend = self.window.local_backend
        self.server_cfg = None
        self._set_state("files")
        self._reset_navigation(path or self.backend.home())

    def connect_server_async(self, cfg: dict, creds: dict | None = None, path: str | None = None):
        """连接服务器(同服务器已在别处连接时立即复用). creds 为会话级凭据."""
        self._cancel_browse()
        self.suspended = False
        self._hide_suspend_hint()
        if self.server_cfg is not None and not self.backend.is_local:
            self.window.hub.release(self, self.backend)
        creds = creds or {}
        self.server_cfg = cfg  # 失败时"重新连接"仍可用
        user = cfg.get("username") or "user"
        self._set_state("busy", f"正在连接 {user}@{cfg['host']}…")

        def on_done(backend, done_path, error):
            if backend is not None:
                self._on_connected(backend, cfg, done_path)
            else:
                self._on_connect_failed(error)

        self.window.hub.request_connect(self, cfg, creds, path, on_done)

    def _on_connected(self, backend, cfg, path):
        self.backend = backend
        self.server_cfg = cfg
        self._set_state("files")
        self._sync_toolbar()
        self._reset_navigation(path or backend.home())
        self.window.on_pane_connected(self)
        self.window.toast(f"已连接 {backend.label}")
        return False

    def _on_connect_failed(self, msg):
        self._set_state("disconnected", f"连接失败: {msg}")
        self._sync_toolbar()
        self.window.toast(msg, error=True)
        return False

    def connection_lost_ui(self, msg):
        """连接池通知: 该连接已死亡(纯 UI, 不再操作连接).

        不退出文件界面: 清空列表并显示背景提示, 工具栏按钮切换为"恢复".
        """
        self._cancel_browse()
        self.suspended = True
        self._show_suspend_hint("连接已断开", msg or "点击工具栏播放图标恢复连接")
        self._sync_toolbar()
        self.window.toast("连接已断开", error=True)

    def _toggle_suspend(self):
        if self.suspended:
            self.resume_connection()
        else:
            self.suspend_connection()

    def suspend_connection(self):
        """暂停连接: 保留连接租约与当前路径(恢复时直接刷新, 无需重连);
        暂停期间仍接收断线通知, 提示词会更新为"连接已断开"."""
        if self.server_cfg is None or self.suspended:
            return
        self._cancel_browse()
        self.suspended = True
        self._show_suspend_hint("连接已暂停", "点击工具栏播放图标恢复连接")
        self._sync_toolbar()

    def resume_connection(self):
        """恢复连接: 连接仍在时直接刷新回当前目录; 已死亡则走完整重连."""
        if self.server_cfg is None or not self.suspended:
            return
        self.suspended = False
        self._hide_suspend_hint()
        if getattr(self.backend, "dead", False):
            self.connect_server_async(self.server_cfg, path=self.cwd)
            return
        self._sync_toolbar()
        self.navigate(self.cwd)  # 中途失败会经 _load_failed 再次进入断开提示

    def disconnect_remote(self):
        """回到本地(连接菜单项): 释放连接并清除暂停状态."""
        if self.server_cfg is None:
            return
        self.suspended = False
        self._hide_suspend_hint()
        self.connect_local()

    def _show_suspend_hint(self, title, detail=""):
        self.suspend_title.set_text(title)
        self.suspend_detail.set_text(detail)
        self.suspend_revealer.set_reveal_child(True)
        self.store.remove_all()
        self.empty_revealer.set_reveal_child(False)

    def _hide_suspend_hint(self):
        self.suspend_revealer.set_reveal_child(False)

    def _reconnect(self):
        if self.server_cfg:
            self.connect_server_async(self.server_cfg, path=self.cwd)

    # ---- 状态切换 ----
    def _set_state(self, state: str, message: str = ""):
        self.stack.set_visible_child_name(state)
        if state == "busy":
            self.busy_label.set_text(message)
            self.busy_spinner.start()
        elif state == "disconnected":
            self.disc_label.set_text(message or "未连接远程主机")
            self.reconnect_btn.set_visible(self.server_cfg is not None)
        self._sync_toolbar(state)

    def _sync_toolbar(self, state: str | None = None):
        if state is None:
            state = self.stack.get_visible_child_name()
        remote = self.server_cfg is not None
        self.disconnect_btn.set_visible(remote and state == "files")
        if remote and state == "files":
            if self.suspended:
                self.disconnect_btn.set_icon_name("media-playback-start-symbolic")
                self.disconnect_btn.set_tooltip_text("恢复连接")
            else:
                self.disconnect_btn.set_icon_name("media-playback-pause-symbolic")
                self.disconnect_btn.set_tooltip_text("暂停连接(可恢复)")
        if remote:
            label = self.server_cfg.get("name") or self.backend.label
            if self.suspended:
                label += "（已断开）"
            self.connect_btn.set_label(label)
        else:
            self.connect_btn.set_label("连接")
        sensitive = state == "files" and not self.suspended
        self.path_entry.set_sensitive(sensitive)
        self.bookmark_btn.set_sensitive(sensitive)
        self.up_btn.set_sensitive(sensitive)
        self.home_btn.set_sensitive(sensitive)
        self.filter_btn.set_sensitive(sensitive)
        self.filter_entry.set_sensitive(sensitive)
        self._sync_navigation_buttons()
        self._sync_bookmark_button()

    def _rebuild_recent_popover(self, *args):
        popover = self.recent_btn.get_popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{margin}")(8)
        title = Gtk.Label(label="最近路径", xalign=0)
        title.add_css_class("heading")
        box.append(title)
        paths = [path for path in self._recent_paths if path != self.cwd]
        if paths:
            box.append(Gtk.Separator())
            for path in paths:
                button = Gtk.Button(hexpand=True)
                label = Gtk.Label(label=path, xalign=0, hexpand=True)
                label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                button.set_child(label)
                button.add_css_class("flat")
                button.set_tooltip_text(path)
                button.connect("clicked", lambda *_c, _path=path: (
                    popover.popdown(), self.navigate(_path)))
                box.append(button)
        else:
            empty = Gtk.Label(label="还没有其他访问记录", xalign=0)
            empty.add_css_class("dim-label")
            box.append(empty)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(360)
        scrolled.set_child(box)
        popover.set_child(scrolled)

    # ---- 路径收藏 ----
    def _bookmark_entries(self):
        return config.bookmark_entries(self.window.settings, self.server_cfg)

    def _bookmark_paths(self):
        return [entry["path"] for entry in self._bookmark_entries()]

    def _current_path_is_bookmarked(self):
        path = self.backend.normpath(self.cwd)
        return path in self._bookmark_paths()

    def _sync_bookmark_button(self):
        saved = self._current_path_is_bookmarked()
        self.bookmark_btn.set_icon_name(
            "starred-symbolic" if saved else "non-starred-symbolic")
        self.bookmark_btn.set_tooltip_text(
            "当前路径已收藏" if saved else "路径收藏")

    def _set_current_bookmarked(self, enabled: bool, name: str | None = None):
        path = self.backend.normpath(self.cwd)
        entries = self._bookmark_entries()
        entry = next((item for item in entries if item["path"] == path), None)
        changed = False
        if enabled and entry is None:
            display_name = (name or self.backend.basename(path) or path).strip()
            entries.append({"path": path, "name": display_name})
            changed = True
        elif enabled and name and entry["name"] != name.strip():
            entry["name"] = name.strip()
            changed = True
        elif not enabled and entry is not None:
            entries.remove(entry)
            changed = True
        if not changed:
            return
        config.set_bookmark_entries(self.window.settings, self.server_cfg, entries)
        self._sync_bookmark_button()
        self.window.on_bookmarks_changed()
        self.window.toast("已收藏当前路径" if enabled else "已取消路径收藏")

    def _remove_bookmark(self, path: str):
        entries = self._bookmark_entries()
        remaining = [entry for entry in entries if entry["path"] != path]
        if len(remaining) == len(entries):
            return
        config.set_bookmark_entries(self.window.settings, self.server_cfg, remaining)
        self._sync_bookmark_button()
        self.window.on_bookmarks_changed()

    def _prompt_current_bookmark(self):
        path = self.backend.normpath(self.cwd)
        initial = self.backend.basename(path) or path
        TextPromptDialog(
            self.window,
            lambda name: self._set_current_bookmarked(True, name) if name else None,
            "收藏名称", initial=initial, ok_label="收藏")

    def _rename_bookmark(self, path: str, name: str | None):
        if not name:
            return
        entries = self._bookmark_entries()
        for entry in entries:
            if entry["path"] == path:
                entry["name"] = name.strip()
                config.set_bookmark_entries(
                    self.window.settings, self.server_cfg, entries)
                self.window.on_bookmarks_changed()
                return

    def _prompt_rename_bookmark(self, entry: dict):
        path = entry["path"]
        initial = entry["name"] or self.backend.basename(path) or path
        TextPromptDialog(
            self.window, lambda name: self._rename_bookmark(path, name),
            "重命名收藏", initial=initial, ok_label="保存")

    def _rebuild_bookmark_popover(self, *args):
        popover = self.bookmark_btn.get_popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for margin in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{margin}")(12)
        box.set_size_request(340, -1)

        scope = "本地收藏"
        if self.server_cfg is not None:
            name = self.server_cfg.get("name") or self.backend.label
            scope = f"{name} 的收藏"
        header = Gtk.Box(spacing=6)
        title = Gtk.Label(label=scope, xalign=0, hexpand=True)
        title.add_css_class("heading")
        header.append(title)

        saved = self._current_path_is_bookmarked()
        toggle = Gtk.Button(icon_name=("starred-symbolic" if saved
                                       else "non-starred-symbolic"))
        toggle.add_css_class("flat")
        toggle.set_tooltip_text("取消收藏当前路径" if saved
                                else "收藏当前路径")
        if saved:
            toggle.connect("clicked", lambda *_: (
                popover.popdown(), self._set_current_bookmarked(False)))
        else:
            toggle.connect("clicked", lambda *_: (
                popover.popdown(), self._prompt_current_bookmark()))
        header.append(toggle)
        box.append(header)

        entries = self._bookmark_entries()
        if entries:
            box.append(Gtk.Separator())
            for entry in entries:
                path = entry["path"]
                row = Gtk.Box(spacing=6)
                go = Gtk.Button(hexpand=True)
                name = entry["name"] or self.backend.basename(path) or path
                name_label = Gtk.Label(label=name, xalign=0, hexpand=True)
                name_label.set_ellipsize(Pango.EllipsizeMode.END)
                go.set_child(name_label)
                go.add_css_class("flat")
                go.set_tooltip_text(path)
                go.connect("clicked", lambda *_c, _path=path: (
                    popover.popdown(), self.navigate(_path)))
                rename = Gtk.Button(icon_name="document-edit-symbolic")
                rename.add_css_class("flat")
                rename.set_valign(Gtk.Align.CENTER)
                rename.set_tooltip_text("重命名收藏")
                rename.connect("clicked", lambda *_c, _entry=entry: (
                    popover.popdown(), self._prompt_rename_bookmark(_entry)))
                remove = Gtk.Button(icon_name="user-trash-symbolic")
                remove.add_css_class("flat")
                remove.set_valign(Gtk.Align.CENTER)
                remove.set_tooltip_text("删除收藏")
                remove.connect("clicked", lambda *_c, _path=path: (
                    self._remove_bookmark(_path),
                    self._rebuild_bookmark_popover()))
                row.append(go)
                row.append(rename)
                row.append(remove)
                box.append(row)
        else:
            empty = Gtk.Label(label="还没有收藏路径", xalign=0)
            empty.add_css_class("dim-label")
            empty.set_margin_top(4)
            box.append(empty)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(420)
        scrolled.set_child(box)
        popover.set_child(scrolled)

    # ---- 连接弹出菜单 ----
    def _rebuild_connect_popover(self, *args):
        popover = self.connect_btn.get_popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(12)
        box.set_size_request(300, -1)  # 主机行不因过窄而省略号截断

        def section(title):
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.set_margin_top(6)
            lbl.set_margin_start(2)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            box.append(lbl)

        if self.server_cfg is not None:
            b = Gtk.Button(label="此面板回到本地（传输继续）")
            b.connect("clicked", lambda *_: (popover.popdown(), self.disconnect_remote()))
            box.append(b)
            disconnect = Gtk.Button(label=f"断开 {self.backend.label} 的所有面板连接…")
            backend = self.backend
            disconnect.connect("clicked", lambda *_: (
                popover.popdown(), self.window.disconnect_connection(backend)))
            box.append(disconnect)
            box.append(Gtk.Separator())

        servers = config.load_servers()
        if servers:
            section("已保存的服务器")
            for s in servers:
                row = Gtk.Box(spacing=6)
                b = Gtk.Button(hexpand=True)
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                inner.set_margin_top(4)
                inner.set_margin_bottom(4)
                l1 = Gtk.Label(label=s.get("name") or s["host"], xalign=0, hexpand=True)
                l1.set_ellipsize(Pango.EllipsizeMode.END)
                l2 = Gtk.Label(label=f'{s.get("username", "")}@{s["host"]}:{s.get("port", 22)}',
                               xalign=0, hexpand=True)
                l2.set_ellipsize(Pango.EllipsizeMode.END)
                l2.add_css_class("dim-label")
                l2.add_css_class("caption")
                inner.append(l1)
                inner.append(l2)
                b.set_child(inner)
                b.connect("clicked", lambda *_c, _s=s: (popover.popdown(),
                                                        self.connect_server_async(_s)))
                trash = Gtk.Button(icon_name="user-trash-symbolic")
                trash.set_tooltip_text("从列表删除")
                trash.add_css_class("flat")
                trash.set_valign(Gtk.Align.CENTER)
                trash.connect("clicked", lambda *_c, _id=s.get("id"): (
                    config.delete_server(_id), self._rebuild_connect_popover()))
                row.append(b)
                row.append(trash)
                box.append(row)

        hosts = config.load_ssh_config_hosts()
        if hosts:
            box.append(Gtk.Separator())
            section("~/.ssh/config 中的主机")
            for h in hosts:
                b = Gtk.Button(hexpand=True)
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                inner.set_margin_top(4)
                inner.set_margin_bottom(4)
                l1 = Gtk.Label(label=h["name"], xalign=0, hexpand=True)
                l1.set_ellipsize(Pango.EllipsizeMode.END)
                l2 = Gtk.Label(label=f'{h["username"]}@{h["host"]}:{h["port"]}',
                               xalign=0, hexpand=True)
                l2.set_ellipsize(Pango.EllipsizeMode.END)
                l2.add_css_class("dim-label")
                l2.add_css_class("caption")
                inner.append(l1)
                inner.append(l2)
                b.set_child(inner)
                b.connect("clicked", lambda *_c, _h=h: (
                    popover.popdown(),
                    ConnectDialog(self.window, self._on_connect_form_done, prefill=_h)))
                box.append(b)

        box.append(Gtk.Separator())
        new = Gtk.Button(label="新建连接…")
        new.connect("clicked", lambda *_: (popover.popdown(),
                                           ConnectDialog(self.window, self._on_connect_form_done)))
        box.append(new)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(420)
        scrolled.set_child(box)
        popover.set_child(scrolled)

    def _on_connect_form_done(self, cfg, creds):
        """ConnectDialog 回调: cfg=None 表示取消."""
        if cfg:
            self.connect_server_async(cfg, creds)

    # ==================================================================
    # 列表交互
    # ==================================================================
    def _item_at(self, x, y):
        w = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None:
            item = self._cell_item(w)
            if item is not None:
                return item
            if hasattr(w, "get_item"):
                item = w.get_item()
                if item is not None:
                    return item
            w = w.get_parent()
        return None

    def _point_in_file_list(self, x, y):
        """判断坐标是否在列表正文，而不是列标题等 ColumnView 子区域."""
        widget = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.view:
            if widget.get_css_name() == "listview":
                return True
            widget = widget.get_parent()
        return False

    def _position_of(self, item):
        for i in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(i) is item:
                return i
        return -1

    def _on_selection_changed(self, selection, position, n_items):
        """当前面板有选区时，将它设为工作区内唯一的活动面板."""
        if selection.get_selection().get_size() == 0:
            return
        self._activate_pane()

    def _activate_pane(self):
        """聚焦当前面板并清除对侧残留选区，避免快捷键作用到旧选区."""
        self.workspace.activate_pane(self)
        self.view.grab_focus()

    def _on_row_activated(self, view, position):
        """双击/回车激活行 → 进入目录."""
        item = self.sort_model.get_item(position)
        if item is not None and item.entry.is_dir:
            self.navigate(item.entry.path)

    def _on_view_primary_released(self, gesture, n_press, x, y):
        """单击列表正文空白处时取消当前选择."""
        if (n_press == 1 and self._point_in_file_list(x, y)
                and self._item_at(x, y) is None):
            self.workspace.activate_pane(self)
            self.selection.unselect_all()

    def _on_view_context_released(self, gesture, n_press, x, y):
        """空白区域右键(坐标相对视图)."""
        if not self._point_in_file_list(x, y):
            return
        self._activate_pane()
        item = self._item_at(x, y)
        if item is not None:
            pos = self._position_of(item)
            if pos >= 0 and not self.selection.is_selected(pos):
                self.selection.select_item(pos, True)
        else:
            self.selection.unselect_all()
        self._popup_menu_at(x, y)

    # ---- 单元格右键(挂在单元格内容上: 比行部件内部手势更深, 事件先到) ----
    def _attach_cell_menu(self, widget):
        rc = Gtk.GestureClick()
        rc.set_button(3)
        rc.connect("released", self._on_cell_context_released)
        widget.add_controller(rc)

    def _attach_cell_drag(self, widget):
        """把拖动源挂到高亮行；ColumnView 外层收不到行内的左键拖动序列."""
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.COPY)
        source.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        source.connect("prepare", self._on_cell_drag_prepare)
        widget.add_controller(source)

    def _on_cell_context_released(self, gesture, n_press, x, y):
        self._activate_pane()
        widget = gesture.get_widget()
        item = self._cell_item(widget)
        if item is not None:
            pos = self._position_of(item)
            if pos >= 0 and not self.selection.is_selected(pos):
                self.selection.select_item(pos, True)
        vx, vy = self._to_view_coords(widget, x, y)
        self._popup_menu_at(vx, vy)

    def _on_cell_drag_prepare(self, source, x, y):
        """按住文件行拖动时选择该行，并构造面板间传输载荷."""
        item = self._cell_item(source.get_widget())
        if item is None:
            return None
        pos = self._position_of(item)
        if pos < 0:
            return None
        if not self.selection.is_selected(pos):
            self.selection.unselect_all()
            self.selection.select_item(pos, True)
        else:
            self._activate_pane()
        return self._on_drag_prepare(source, x, y)

    def _cell_item(self, widget):
        """单元格内容 → 绑定时记录的 FileItem.

        不能爬控件树: 工厂的 GtkListItem 与树中的 GtkColumnViewCellWidget
        是不同对象, 后者不暴露 get_item.
        """
        return self._cell_items.get(widget)

    def _to_view_coords(self, widget, x, y):
        """转换行坐标, 包含滚动偏移与 GTK 布局变换."""
        point = Graphene.Point()
        point.init(x, y)
        ok, converted = widget.compute_point(self.view, point)
        return (converted.x, converted.y) if ok else (x, y)

    def _menu_entries(self):
        """右键菜单条目: (标签, 动作名后缀, 快捷键文本) 或 "sep" (分隔线)."""
        has_sel = bool(self._selected_items())
        entries = [("刷新", "refresh", None)]
        if has_sel:
            entries += [("传输到对侧", "transfer", None),
                        ("复制", "copy", "Ctrl+C"),
                        ("剪切", "cut", "Ctrl+X")]
        entries.append(("粘贴", "paste", "Ctrl+V"))
        if has_sel:
            entries.append(("删除", "delete", "Delete"))
        entries.append("sep")
        entries.append(("新建文件夹", "mkdir", None))
        if has_sel:
            entries += [("重命名", "rename", "F2"),
                        ("复制完整路径", "copy-path", None)]
        return entries

    def _popup_menu_at(self, x, y):
        """在 view 坐标 (x,y) 弹出菜单, 允许超出应用窗口."""
        self.window.close_context_menu()
        content = self._build_menu_content()
        self._menu_popover = content
        self.window.open_context_menu(content, self.view, x, y)

    def _build_menu_content(self):
        menu = ContextMenu(self)
        lb = Gtk.ListBox()
        lb.set_size_request(220, -1)
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.set_activate_on_single_click(True)
        actions = {}

        def activate_row(listbox, row):
            action = actions.get(row)
            if action is not None:
                menu.popdown()
                self.activate_action(f"{self.pane_id}.{action}", None)

        # 鼠标点击直接触发 row-activated; 键盘 activate 也汇入该信号。
        lb.connect("row-activated", activate_row)
        for entry in self._menu_entries():
            if entry == "sep":
                sep_row = Gtk.ListBoxRow()
                sep_row.add_css_class("menu-separator")
                sep_row.set_selectable(False)
                sep_row.set_activatable(False)
                sep_row.set_focusable(False)
                sep_row.set_sensitive(False)
                sep = Gtk.Separator()
                sep.set_margin_top(4)
                sep.set_margin_bottom(4)
                sep_row.set_child(sep)
                lb.append(sep_row)
                continue
            label, action, accel = entry
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(spacing=14, margin_start=10, margin_end=10)
            l = Gtk.Label(label=label, xalign=0, hexpand=True)
            hb.append(l)
            if accel:
                a = Gtk.Label(label=accel, xalign=0)
                a.add_css_class("dim-label")
                a.add_css_class("caption")
                hb.append(a)
            row.set_child(hb)
            actions[row] = action
            lb.append(row)
        menu.set_child(lb)
        return menu

    def _selected_items(self) -> list[FileItem]:
        sel = self.selection.get_selection()
        out = []
        for i in range(sel.get_size()):
            pos = sel.get_nth(i)
            item = self.sort_model.get_item(pos)
            if item is not None:
                out.append(item)
        return out

    def _selected_paths(self) -> list[str]:
        return [it.entry.path for it in self._selected_items()]

    # ==================================================================
    # 动作(右键菜单 + 快捷键共用)
    # ==================================================================
    def _install_actions(self):
        # 动作名必须全局唯一: GTK4 中兄弟部件安装同名动作会互相覆盖
        # (后安装者赢得所有激活), 因此用 pane_id 做前缀.
        p = self.pane_id
        for name, fn in (
            (f"{p}.transfer", self._action_transfer),
            (f"{p}.copy-path", self._action_copy_path),
            (f"{p}.copy", self._action_copy),
            (f"{p}.cut", self._action_cut),
            (f"{p}.paste", self._action_paste),
            (f"{p}.mkdir", self._action_mkdir),
            (f"{p}.rename", self._action_rename),
            (f"{p}.delete", self._action_delete),
            (f"{p}.refresh", self.refresh),
        ):
            self.install_action(name, None, lambda *a, _f=fn: _f())

    def _action_transfer(self):
        paths = self._selected_paths()
        if not paths:
            self.window.toast("先选择要传输的项目")
            return
        other = self.workspace.other(self)
        self.window.manager.enqueue(self.backend, paths, other.backend, other.cwd)

    # ---- 剪贴板 ----
    def _action_copy(self):
        paths = self._selected_paths()
        if not paths:
            return
        self.window.set_clipboard(self.backend, self.cwd, paths, "copy")

    def _action_cut(self):
        paths = self._selected_paths()
        if not paths:
            return
        self.window.set_clipboard(self.backend, self.cwd, paths, "cut")

    def _action_paste(self):
        clip = self.window.get_clipboard()
        if clip is None:
            self.window.toast("剪贴板为空")
            return
        src_backend, src_dir, paths, mode = clip
        if src_backend is self.backend and src_dir == self.cwd:
            if mode == "cut":
                self.window.clear_clipboard()  # 剪切回原目录 = 无操作
            return
        # 移动语义在核心层: 只有成功提交的项目才删源, 跳过/失败保留源
        t = self.window.manager.enqueue(src_backend, paths, self.backend, self.cwd,
                                        move=(mode == "cut"))
        if t is not None and t.status != "error" and mode == "cut":
            self.window.clear_clipboard()

    # ---- 其他操作 ----
    def _action_copy_path(self):
        items = self._selected_items()
        if not items:
            return
        text = "\n".join(it.entry.path for it in items)
        clipboard = self.get_display().get_clipboard()
        v = GObject.Value()
        v.init(str)
        v.set_string(text)
        clipboard.set(v)

    def _action_mkdir(self):
        backend, path = self.backend, self.cwd
        TextPromptDialog(self.window, lambda name: self._do_mkdir(name, backend, path),
                         "新建文件夹", ok_label="创建")

    def _do_mkdir(self, name, backend=None, path=None):
        if not name:
            return
        backend = self.backend if backend is None else backend
        path = self.cwd if path is None else path

        def op():
            backend.mkdir(backend.join(path, name))
        self._run_op(op)

    def _action_rename(self):
        items = self._selected_items()
        if len(items) != 1:
            self.window.toast("请选择单个项目重命名")
            return
        entry = items[0].entry
        backend, path = self.backend, self.cwd
        TextPromptDialog(self.window, lambda n: self._do_rename(entry, n, backend, path),
                         "重命名", initial=entry.name, ok_label="重命名")

    def _do_rename(self, entry, new_name, backend=None, path=None):
        if not new_name or new_name == entry.name:
            return
        backend = self.backend if backend is None else backend
        path = self.cwd if path is None else path

        def op():
            backend.rename(entry.path, backend.join(path, new_name))
        self._run_op(op)

    def _action_delete(self):
        items = self._selected_items()
        if not items:
            return
        names = [it.entry.name for it in items]
        backend = self.backend
        if backend.is_local:
            # 本地: 对话框中选择 移入回收站(可找回) 或 直接删除
            ask_delete_local(self.window, names,
                             lambda mode: mode and self._do_delete(
                                 items, mode == "trash", backend))
        else:
            # 远程: 永久删除, 必须确认
            ask_delete(self.window, names,
                       lambda ok: ok and self._do_delete(items, False, backend))

    def _do_delete(self, items, to_trash, backend=None):
        paths = [it.entry.path for it in items]
        backend = self.backend if backend is None else backend

        def op():
            permanent = not to_trash
            asked = False
            for p in paths:
                if permanent:
                    backend.delete(p)
                    continue
                try:
                    backend.delete_to_trash(p)
                except BackendError:
                    # 回收站不可用(如 /tmp): 询问一次是否永久删除
                    if not asked:
                        asked = True
                        permanent = self.window.blocking_dialog(
                            lambda done: ask_permanent_delete(self.window, done))
                    if not permanent:
                        raise BackendError("已取消，部分文件可能已在回收站")
                    backend.delete(p)

        self._run_op(op)

    def _run_op(self, op):
        if self.window._exit_mode or self.window._closed:
            self.window.toast("正在退出，无法执行新操作", True)
            return
        if self.suspended:
            self.window.toast("连接已暂停，无法执行该操作", True)
            return
        token = object()
        self.window._operations.add(token)

        def finished():
            self.window._operations.discard(token)
            if not self.workspace.closed:
                self.refresh()

        def work():
            try:
                if self.window._cancel_dialogs.is_set():
                    return
                op()
            except BackendError as e:
                GLib.idle_add(self.window.toast, str(e), True)
            finally:
                GLib.idle_add(finished)
        threading.Thread(target=work, daemon=True).start()

    # ==================================================================
    # 拖拽
    # ==================================================================
    def _build_dnd(self):
        # 使用 G_TYPE_STRING 让 GTK 能在拖动经过目标时完成格式协商。
        # 自定义 MIME + GBytes 虽能手动调用 drop 回调，但 GTK 不一定能
        # 自动反序列化，真实拖放时会显示“禁止”光标。
        dt = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
        dt.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        dt.connect("drop", self._on_drop_internal)
        dt.connect("enter", self._on_drop_enter)
        dt.connect("leave", self._on_drop_leave)
        self.view.add_controller(dt)
        self._internal_drop_target = dt

        dtf = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        dtf.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        dtf.connect("drop", self._on_drop_files)
        dtf.connect("enter", self._on_drop_enter)
        dtf.connect("leave", self._on_drop_leave)
        self.view.add_controller(dtf)

    def _on_drop_enter(self, target, x, y):
        self.stack.add_css_class("pane-drop")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, target):
        self.stack.remove_css_class("pane-drop")

    def _on_drag_prepare(self, source, x, y):
        paths = self._selected_paths()
        if not paths:
            return None
        self.window.stage_drag_payload(self.pane_id, self.backend, paths)
        # 拖拽图标: 第一项的图标
        try:
            items = self._selected_items()
            e = items[0].entry
            icon_name = "folder" if e.is_dir else (
                Gio.content_type_get_icon(Gio.content_type_guess(e.name, b"")[0])
                .get_names()[0])
            theme = Gtk.IconTheme.get_for_display(self.get_display())
            paint = theme.lookup_icon(icon_name, None, 40, self.get_scale_factor(),
                                      self.get_direction(), 0)
            source.set_icon(paint, 20, 20)
        except Exception:
            pass
        value = GObject.Value()
        value.init(str)
        value.set_string(json.dumps({"pane": self.pane_id}))
        return Gdk.ContentProvider.new_for_value(value)

    def _on_drop_internal(self, target, value, x, y):
        self.stack.remove_css_class("pane-drop")
        try:
            if isinstance(value, str):
                text = value
            elif hasattr(value, "get_string"):
                text = value.get_string()
            else:  # 兼容旧测试载荷
                text = bytes(value.get_boxed().get_data()).decode()
            data = json.loads(text)
        except (TypeError, ValueError, UnicodeDecodeError, AttributeError):
            return False
        if data.get("pane") == self.pane_id:
            return True  # 同一面板内拖拽: 忽略
        payload = self.window.consume_drag_payload(data.get("pane"))
        if payload is None:
            return False
        backend, paths = payload
        if paths:
            self.window.manager.enqueue(backend, paths, self.backend, self.cwd)
        return True

    def _on_drop_files(self, target, value, x, y):
        self.stack.remove_css_class("pane-drop")
        fl = value.get_boxed()
        paths = []
        for f in fl.get_files():
            p = f.get_path()
            if p:
                paths.append(p)
        if not paths:
            self.window.toast("没有可用的本地文件路径")
            return False
        self.window.manager.enqueue(self.window.local_backend, paths,
                                    self.backend, self.cwd)
        return True

    def _on_path_activate(self, entry):
        self.navigate(entry.get_text())

    def _on_entry_icon_press(self, entry, icon_pos):
        if icon_pos == Gtk.EntryIconPosition.SECONDARY:
            self.refresh()
