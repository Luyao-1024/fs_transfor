"""对话框: 新建连接 / 认证重试 / 主机密钥确认(未知与变化) / 重名覆盖 / 文本输入 / 删除确认.

约定: 构造时传入 done 回调, 对话框自动 present; 结束时恰好回调一次.
"""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from . import config

AUTH_METHODS = ["key", "password", "agent"]
AUTH_LABELS = ["密钥文件", "密码", "SSH Agent"]


def _margin_box(child, top=18, bottom=12, start=18, end=18):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_margin_top(top)
    box.set_margin_bottom(bottom)
    box.set_margin_start(start)
    box.set_margin_end(end)
    box.append(child)
    return box


def _button_row(window, on_cancel, on_ok, ok_label="确定"):
    row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
    cancel = Gtk.Button(label="取消")
    cancel.connect("clicked", lambda *_: on_cancel())
    ok = Gtk.Button(label=ok_label)
    ok.add_css_class("suggested-action")
    ok.connect("clicked", lambda *_: on_ok())
    row.append(cancel)
    row.append(ok)
    return row


# ----------------------------------------------------------------------
class ConnectDialog(Adw.Dialog):
    """新建/编辑连接表单. done(cfg, creds) 或 done(None, None)."""

    def __init__(self, parent, done, prefill: dict | None = None, server: dict | None = None):
        super().__init__()
        self.set_title("连接服务器")
        self.set_content_width(460)
        self._done_cb = done
        self._done = False
        self._prefill = prefill or {}
        self._server = server or {}

        src = server or self._prefill

        group = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title="显示名称 (可选)")
        self.host_row = Adw.EntryRow(title="主机地址")
        self.port_row = Adw.EntryRow(title="端口")
        self.port_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.user_row = Adw.EntryRow(title="用户名")

        self.auth_row = Adw.ComboRow(title="认证方式")
        self.auth_row.set_model(Gtk.StringList.new(AUTH_LABELS))

        self.key_row = Adw.EntryRow(title="密钥路径 (默认自动探测 ~/.ssh)")
        browse = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER)
        browse.add_css_class("flat")
        browse.connect("clicked", self._browse_key)
        self.key_row.add_suffix(browse)

        self.pass_row = Adw.PasswordEntryRow(title="密钥口令 (如有)")
        self.pwd_row = Adw.PasswordEntryRow(title="密码")

        for r in (self.name_row, self.host_row, self.port_row, self.user_row,
                  self.auth_row, self.key_row, self.pass_row, self.pwd_row):
            group.add(r)

        self.save_check = Gtk.CheckButton(label="保存到服务器列表, 下次自动连接")
        self.save_check.set_active(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(group)
        box.append(self.save_check)
        box.append(_button_row(self, self._cancel, self._submit, "连接"))
        self.set_child(_margin_box(box))

        # 预填
        self.name_row.set_text(src.get("name") or "")
        self.host_row.set_text(src.get("host") or "")
        self.port_row.set_text(str(src.get("port") or 22))
        self.user_row.set_text(src.get("username") or os_user())
        self.key_row.set_text(src.get("key_path") or "")
        method = src.get("auth_method") or "key"
        self.auth_row.set_selected(AUTH_METHODS.index(method) if method in AUTH_METHODS else 0)
        if server:
            self.save_check.set_active(True)
        self.auth_row.connect("notify::selected", lambda *_: self._sync_auth_rows())
        self._sync_auth_rows()

        self.connect("closed", lambda *_: self._finish(None, None))
        self.present(parent)

    def _sync_auth_rows(self):
        sel = self.auth_row.get_selected()
        self.key_row.set_visible(sel == 0)
        self.pass_row.set_visible(sel == 0)
        self.pwd_row.set_visible(sel == 1)

    def _browse_key(self, *_):
        dlg = Gtk.FileDialog()
        dlg.set_title("选择密钥文件")

        def cb(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.key_row.set_text(f.get_path())

        dlg.open(self, None, cb)

    def _cancel(self):
        self.close()  # 'closed' → finish(None)

    def _submit(self):
        host = self.host_row.get_text().strip()
        if not host:
            self.host_row.add_css_class("error")
            return
        try:
            port = int(self.port_row.get_text().strip() or 22)
        except ValueError:
            port = 22
        user = self.user_row.get_text().strip()
        name = self.name_row.get_text().strip() or (f"{user}@{host}" if user else host)
        cfg = {
            "id": self._server.get("id"),
            "name": name,
            "host": host,
            "port": port,
            "username": user,
            "auth_method": AUTH_METHODS[self.auth_row.get_selected()],
            "key_path": self.key_row.get_text().strip() or None,
        }
        if self.save_check.get_active():
            cfg = config.upsert_server(cfg)
        creds = {
            "password": self.pwd_row.get_text() or None,
            "passphrase": self.pass_row.get_text() or None,
        }
        self._finish(cfg, creds)

    def _finish(self, cfg, creds):
        if self._done:
            return
        self._done = True
        if cfg is not None:
            self.close()
        self._done_cb(cfg, creds)


# ----------------------------------------------------------------------
class AuthDialog(Adw.Dialog):
    """连接中补充凭据: kind = 'password' | 'passphrase' | 'both'."""

    def __init__(self, parent, done, kind: str, message: str):
        super().__init__()
        self.set_title("需要认证")
        self.set_content_width(420)
        self._done_cb = done
        self._done = False
        self._kind = kind

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        if message:
            msg = Gtk.Label(label=message, xalign=0, wrap=True)
            msg.add_css_class("dim-label")
            box.append(msg)

        group = Adw.PreferencesGroup()
        self.pass_row = None
        self.pwd_row = None
        if kind in ("passphrase", "both"):
            self.pass_row = Adw.PasswordEntryRow(title="密钥口令")
            group.add(self.pass_row)
        if kind in ("password", "both"):
            self.pwd_row = Adw.PasswordEntryRow(title="登录密码")
            group.add(self.pwd_row)
        box.append(group)
        box.append(_button_row(self, self._cancel, self._submit, "继续"))
        self.set_child(_margin_box(box))
        self.connect("closed", lambda *_: self._finish(None))
        self.present(parent)

    def _cancel(self):
        self.close()

    def _submit(self):
        result = {}
        if self.pass_row is not None:
            result["passphrase"] = self.pass_row.get_text() or None
        if self.pwd_row is not None:
            result["password"] = self.pwd_row.get_text() or None
        self._finish(result)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self.close()
        self._done_cb(result)


# ----------------------------------------------------------------------
class _KeyDialog(Adw.Dialog):
    """主机密钥对话框公共外壳: 关闭一律按"拒绝"收尾, 回调只触发一次."""

    def _begin(self, done):
        self._done_cb = done
        self._done = False

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self.close()
        self._done_cb(result)


class HostKeyDialog(_KeyDialog):
    """未知主机密钥: 展示 OpenSSH 风格 SHA256 指纹请求确认. done(True/False).

    note 用于"该主机已有其他类型密钥"等补充说明; 指纹可选中复制,
    便于与 ssh-keygen -lf 或服务器控制台输出逐字比对。
    """

    def __init__(self, parent, done, host: str, key_type: str, fingerprint: str,
                 note: str = ""):
        super().__init__()
        self.set_title("验证主机密钥")
        self.set_content_width(460)
        self._begin(done)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        heading = Gtk.Label()
        heading.set_markup(f"<b>{'首次连接' if not note else '需要确认'} "
                           f"{GLib.markup_escape_text(host)}</b>")
        heading.set_xalign(0)
        box.append(heading)

        info = Adw.PreferencesGroup()
        info.add(_value_row("密钥类型", key_type, monospace=False))
        info.add(_value_row("指纹 (SHA256)", fingerprint))
        box.append(info)

        if note:
            extra = Gtk.Label(label=note, xalign=0, wrap=True)
            extra.add_css_class("caption")
            extra.add_css_class("dim-label")
            box.append(extra)

        warn = Gtk.Label(label="仅在你确认这是自己服务器时继续连接。", xalign=0, wrap=True)
        warn.add_css_class("warning")
        box.append(warn)

        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        no = Gtk.Button(label="拒绝")
        no.connect("clicked", lambda *_: self._finish(False))
        yes = Gtk.Button(label="信任并连接")
        yes.add_css_class("suggested-action")
        yes.connect("clicked", lambda *_: self._finish(True))
        row.append(no)
        row.append(yes)
        box.append(row)

        self.set_child(_margin_box(box))
        self.connect("closed", lambda *_: self._finish(False))
        self.present(parent)


class HostKeyMismatchDialog(_KeyDialog):
    """known_hosts 中同类型主机密钥与服务器不一致: 只呈现证据, 不提供放行.

    自动接受被替换的密钥会让中间人攻击变成一次点击, 因此这里没有
    "仍然连接" 按钮; 用户必须先在 ~/.ssh/known_hosts 中人工处理该条目。
    """

    def __init__(self, parent, done, host: str, key_type: str,
                 expected: str, got: str):
        super().__init__()
        self.set_title("主机密钥不一致")
        self.set_content_width(480)
        self._begin(done)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        heading = Gtk.Label()
        heading.set_markup(f"<b>{GLib.markup_escape_text(host)} 的主机密钥已改变</b>")
        heading.set_xalign(0)
        box.append(heading)

        warn = Gtk.Label(
            label=("服务器提供的 " + key_type + " 主机密钥与 known_hosts 中已记录的不一致。"
                   "这可能是服务器密钥轮换, 也可能是中间人攻击。为安全起见本次连接已中止。"),
            xalign=0, wrap=True)
        warn.add_css_class("error")
        box.append(warn)

        info = Adw.PreferencesGroup()
        info.add(_value_row("已记录的指纹", expected))
        info.add(_value_row("本次收到的指纹", got))
        box.append(info)

        hint = Gtk.Label(
            label=("请先通过可信渠道(服务器控制台、SSH 客户端或其他主机)核对新指纹。"
                   "确认无误后, 删除 ~/.ssh/known_hosts 中该主机的旧条目再重新连接。"),
            xalign=0, wrap=True)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)

        close = Gtk.Button(label="拒绝连接")
        close.add_css_class("destructive-action")
        close.connect("clicked", lambda *_: self._finish(False))
        box.append(close)

        self.set_child(_margin_box(box))
        self.connect("closed", lambda *_: self._finish(False))
        self.present(parent)


def _value_row(title: str, value: str, monospace: bool = True) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    label = Gtk.Label(label=value, halign=Gtk.Align.END)
    label.set_selectable(True)
    label.add_css_class("dim-label")
    if monospace:
        label.add_css_class("monospace")
    row.add_suffix(label)
    return row


# ----------------------------------------------------------------------
class TextPromptDialog(Adw.Dialog):
    """单行文本输入(新建文件夹/重命名). done(text | None)."""

    def __init__(self, parent, done, title: str, initial: str = "",
                 ok_label: str = "确定", validator=None):
        """单行文本输入对话框. validator(text) -> 错误说明('' 表示可用)."""
        super().__init__()
        self.set_title(title)
        self.set_content_width(400)
        self._done_cb = done
        self._done = False
        self._validator = validator

        group = Adw.PreferencesGroup()
        self.entry_row = Adw.EntryRow(title=title)
        self.entry_row.set_text(initial)
        group.add(self.entry_row)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(group)
        self.error_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)
        box.append(self.error_label)
        box.append(_button_row(self, self._cancel, self._submit, ok_label))
        self.set_child(_margin_box(box))

        self.entry_row.connect("entry-activated", lambda *_: self._submit())
        self.entry_row.connect("changed", lambda *_: self._clear_error())
        self.connect("closed", lambda *_: self._finish(None))
        self.present(parent)

    def _clear_error(self):
        self.error_label.set_visible(False)
        self.entry_row.remove_css_class("error")

    def _show_error(self, message: str):
        self.error_label.set_text(message)
        self.error_label.set_visible(True)
        self.entry_row.add_css_class("error")

    def _cancel(self):
        self.close()

    def _submit(self):
        text = self.entry_row.get_text().strip()
        if not text:
            self._show_error("名称不能为空")
            return
        if self._validator is not None:
            error = self._validator(text)
            if error:
                self._show_error(error)
                return
        self._finish(text)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self.close()
        self._done_cb(result)


# ----------------------------------------------------------------------
def ask_overwrite(parent, names: list[str], done):
    """同名冲突确认: done('overwrite' | 'skip' | None). 关闭/取消均视为 None."""
    preview = "\n".join(names[:5])
    if len(names) > 5:
        preview += f"\n… 共 {len(names)} 个"
    dlg = Adw.AlertDialog.new("覆盖已存在的项目？",
                              f"目标位置已有 {len(names)} 个同名项目：\n{preview}")
    dlg.add_response("cancel", "取消")
    dlg.add_response("skip", "跳过")
    dlg.add_response("overwrite", "覆盖")
    dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.SUGGESTED)

    def cb(d, task):
        try:
            resp = d.choose_finish(task)
        except GLib.Error:
            resp = "cancel"
        # 只认明确的两个选择; Esc/关闭('close')/取消 一律视为取消
        done("overwrite" if resp == "overwrite" else "skip" if resp == "skip" else None)

    dlg.choose(parent, None, cb)
    return dlg


def ask_replace(parent, name: str, done):
    """重命名目标已存在: 明确二次确认, 默认不替换. done(True/False)."""
    dlg = Adw.AlertDialog.new(f"“{name}”已存在",
                              "替换会永久覆盖目标位置已有的项目，且无法通过回收站找回。")
    dlg.add_response("cancel", "取消")
    dlg.add_response("replace", "替换")
    dlg.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_close_response("cancel")
    dlg.set_default_response("cancel")

    def cb(d, result):
        try:
            resp = d.choose_finish(result)
        except GLib.Error:
            resp = "cancel"
        done(resp == "replace")

    dlg.choose(parent, None, cb)
    return dlg


def ask_delete(parent, names: list[str], done):
    """删除确认(远程/永久删除): done(True/False). 关闭/取消均视为 False."""
    n = len(names)
    what = names[0] if n == 1 else f"{n} 个项目"
    dlg = Adw.AlertDialog.new(f"删除 {what}？",
                              "远程删除无法恢复（目录将递归删除）。")
    dlg.add_response("cancel", "取消")
    dlg.add_response("delete", "删除")
    dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

    def cb(d, task):
        try:
            resp = d.choose_finish(task)
        except GLib.Error:
            resp = "cancel"
        done(resp == "delete")

    dlg.choose(parent, None, cb)
    return dlg


def ask_delete_local(parent, names: list[str], done):
    """本地删除确认: done(mode) — 'trash'(回收站) | 'permanent'(直接删除) | None(取消)."""
    n = len(names)
    what = names[0] if n == 1 else f"{n} 个项目"
    dlg = Adw.AlertDialog.new(f"删除 {what}？",
                              "移入回收站可在文件管理器中找回；直接删除无法恢复。")
    dlg.add_response("cancel", "取消")
    dlg.add_response("permanent", "直接删除")
    dlg.set_response_appearance("permanent", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.add_response("delete", "移入回收站")
    dlg.set_response_appearance("delete", Adw.ResponseAppearance.SUGGESTED)

    def cb(d, task):
        try:
            resp = d.choose_finish(task)
        except GLib.Error:
            resp = "cancel"
        if resp == "delete":
            done("trash")
        elif resp == "permanent":
            done("permanent")
        else:
            done(None)

    dlg.choose(parent, None, cb)
    return dlg


def ask_permanent_delete(parent, done):
    """回收站不可用时的永久删除确认: done(True/False)."""
    dlg = Adw.AlertDialog.new("该位置不支持回收站",
                              "无法移入回收站(如 /tmp 等系统内部挂载)。改为永久删除？"
                              "永久删除无法恢复。")
    dlg.add_response("cancel", "取消")
    dlg.add_response("delete", "永久删除")
    dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

    def cb(d, task):
        try:
            resp = d.choose_finish(task)
        except GLib.Error:
            resp = "cancel"
        done(resp == "delete")

    dlg.choose(parent, None, cb)
    return dlg


def os_user() -> str:
    import os
    return os.environ.get("USER") or "root"
