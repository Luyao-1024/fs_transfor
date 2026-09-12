"""共享连接池: 同一服务器(host,port,username)在多个标签页间复用同一条连接.

- request_connect: 已连接 → 立即复用; 连接进行中 → 挂起等待; 否则新建(含指纹/认证对话框)
- release: 引用计数, 最后一个使用方释放时才真正断开
- mark_dead: 连接死亡 → 通知所有使用它的面板
"""
from __future__ import annotations

import threading

from gi.repository import GLib

from .backend.base import BackendError
from .backend.sftp import AuthNeeded, HostKeyChanged, HostKeyUnknown, SftpBackend
from .connect_dialog import AuthDialog, HostKeyDialog, HostKeyMismatchDialog

MAX_CONNECT_RETRIES = 8


class _Entry:
    __slots__ = ("backend", "cfg", "users", "tasks", "disconnecting")

    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = cfg
        self.users: set = set()
        self.tasks: set = set()
        self.disconnecting = False


def _safe_disconnect(backend):
    try:
        backend.disconnect()
    except Exception:
        pass


def _pane_alive(pane) -> bool:
    try:
        return not pane.workspace.closed
    except Exception:
        return False


class ConnectionHub:
    """窗口级连接池. 所有方法除 _connect_thread 外均在主线程调用."""

    def __init__(self, window):
        self.window = window
        self._live: dict[tuple, _Entry] = {}
        self._pending: dict[tuple, list] = {}  # key -> [(pane, path, on_done)]
        self._dialogs: dict[int, tuple] = {}
        self.closing = False

    @staticmethod
    def key(cfg):
        return (cfg.get("host"), int(cfg.get("port") or 22), cfg.get("username") or "")

    # ------------------------------------------------------------------
    # 请求连接. on_done(backend, path, error): 成功时 error=None.
    # ------------------------------------------------------------------
    def request_connect(self, pane, cfg, creds, path, on_done):
        if self.closing:
            on_done(None, None, "正在退出，已停止新连接")
            return
        self.cancel_pending(pane)
        key = self.key(cfg)
        entry = self._live.get(key)
        if entry is not None and entry.disconnecting:
            on_done(None, None, "此连接正在等待任务清理后断开")
            return
        if entry is not None and not entry.backend.dead:
            entry.users.add(pane)          # 复用现有连接: 立即生效
            on_done(entry.backend, path, None)
            return
        if entry is not None:
            del self._live[key]            # 清理死条目

        waiters = self._pending.setdefault(key, [])
        waiters.append((pane, path, on_done))
        if len(waiters) > 1:
            return                         # 同一服务器正在连接中: 挂起等待结果
        threading.Thread(target=self._connect_thread,
                         args=(cfg, creds or {}, waiters), daemon=True).start()

    def _connect_thread(self, cfg, creds, request):
        backend = SftpBackend(
            cfg["host"], cfg.get("port", 22), cfg.get("username"),
            cfg.get("auth_method", "key"), cfg.get("key_path"),
            password=creds.get("password"), passphrase=creds.get("passphrase"),
        )
        accepted = None
        handed_off = False
        try:
            for _attempt in range(MAX_CONNECT_RETRIES):
                try:
                    backend.connect(accepted_key=accepted)
                    break
                except HostKeyUnknown as e:
                    ok = self._request_dialog(cfg, request,
                        lambda done, _e=e: HostKeyDialog(
                            self.window, done, _e.hostname or backend.host,
                            _e.key_type.replace("ssh-", ""), _e.fingerprint,
                            _e.note))
                    if not ok:
                        self._dispatch_fail(cfg, "已拒绝主机密钥", request)
                        return
                    accepted = e.key
                except HostKeyChanged as e:
                    # 已记录的同类型密钥被替换: 只呈现证据, 不提供"仍然连接"
                    self._request_dialog(cfg, request,
                        lambda done, _e=e: HostKeyMismatchDialog(
                            self.window, done, _e.hostname or backend.host,
                            _e.key_type, _e.expected, _e.got))
                    self._dispatch_fail(cfg, (
                        f"{e.hostname or backend.host} 的主机密钥与 known_hosts 不一致，"
                        "已拒绝连接。请核对指纹后再更新该条目。"), request)
                    return
                except AuthNeeded as e:
                    got = self._request_dialog(cfg, request,
                        lambda done, _e=e: AuthDialog(self.window, done, _e.kind, str(_e)))
                    if not got:
                        self._dispatch_fail(cfg, "缺少凭据, 已取消连接", request)
                        return
                    if got.get("password"):
                        backend.password = got["password"]
                    if got.get("passphrase"):
                        backend.passphrase = got["passphrase"]
                except BackendError as e:
                    self._dispatch_fail(cfg, str(e), request)
                    return
                except Exception as e:
                    self._dispatch_fail(cfg, f"连接失败: {e}", request)
                    return
            else:
                self._dispatch_fail(cfg, "多次尝试后仍无法连接", request)
                return
            if self.closing:
                return
            GLib.idle_add(self._dispatch_ok, cfg, backend, request)
            handed_off = True
        except Exception as e:  # 防御: 保证 waiters 总能收到结果
            self._dispatch_fail(cfg, f"连接异常: {e}", request)
            return
        finally:
            if not handed_off:
                _safe_disconnect(backend)

    def _request_dialog(self, cfg, request, build):
        def show(done):
            if (self._pending.get(self.key(cfg)) is request
                    and any(_pane_alive(p) for p, _, _ in request)):
                finished = False

                def finish(value):
                    nonlocal finished
                    if not finished:
                        finished = True
                        self._dialogs.pop(id(request), None)
                        done(value)

                try:
                    dialog = build(finish)
                except Exception:
                    finish(None)
                    return
                if not finished:
                    self._dialogs[id(request)] = (dialog, finish)
                return
            done(None)
        return self.window.blocking_dialog(show)

    def _dispatch_ok(self, cfg, backend, request):
        key = self.key(cfg)
        if self.closing or self._pending.get(key) is not request:
            _safe_disconnect(backend)
            return GLib.SOURCE_REMOVE
        waiters = self._pending.pop(key, [])
        if getattr(backend, "host_key_note", ""):
            # known_hosts 读写受限: 连接仍可用, 但新确认的密钥不会被记住
            GLib.idle_add(self.window.toast, backend.host_key_note, True)
        entry = _Entry(backend, cfg)
        self._live[key] = entry
        for pane, path, on_done in waiters:
            if _pane_alive(pane):
                entry.users.add(pane)
                on_done(backend, path, None)
        if not entry.users and not entry.tasks:
            # 请求者都已关闭: 不要留下孤儿连接
            self._live.pop(key, None)
            _safe_disconnect(backend)
        return GLib.SOURCE_REMOVE

    def _dispatch_fail(self, cfg, msg, request):
        GLib.idle_add(self._dispatch_fail_ui, self.key(cfg), msg, request)

    def _dispatch_fail_ui(self, key, msg, request):
        if self._pending.get(key) is not request:
            return GLib.SOURCE_REMOVE
        for pane, _path, on_done in self._pending.pop(key, []):
            if _pane_alive(pane):
                on_done(None, None, msg)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def release(self, pane, backend):
        """面板不再使用该连接; 最后一个使用方释放时断开."""
        for key, entry in list(self._live.items()):
            if entry.backend is backend:
                entry.users.discard(pane)
                if not entry.users and not entry.tasks:
                    del self._live[key]
                    _safe_disconnect(backend)
                return

    def acquire_task(self, task):
        """入队前在主线程取得全部远端租约，失败时不留下部分引用。"""
        entries = []
        for backend in (task.src, task.dst):
            if backend.is_local:
                continue
            entry = next((e for e in self._live.values()
                          if e.backend is backend), None)
            if entry is None or backend.dead or entry.disconnecting:
                raise BackendError("传输连接已失效，请重新连接并重新选择源文件")
            entries.append(entry)
        for entry in entries:
            entry.tasks.add(task)

    def release_task(self, task):
        """任务清理回调在主线程释放租约，排队期间也持有引用。"""
        for key, entry in list(self._live.items()):
            if task not in entry.tasks:
                continue
            entry.tasks.discard(task)
            if not entry.users and not entry.tasks:
                del self._live[key]
                _safe_disconnect(entry.backend)
        return GLib.SOURCE_REMOVE

    def mark_dead(self, backend, msg):
        """连接已死亡: 移除并通知所有使用它的面板."""
        for key, entry in list(self._live.items()):
            if entry.backend is backend:
                del self._live[key]
                users = list(entry.users)
                entry.users.clear()
                for task in entry.tasks:
                    if task.running:
                        task.connection_error = f"连接已断开: {msg}"
                        task.cancel_event.set()
                entry.tasks.clear()
                _safe_disconnect(backend)
                for pane in users:
                    if _pane_alive(pane):
                        pane.connection_lost_ui(msg)
                return

    def cancel_pending(self, pane):
        """标签关闭: 丢弃该面板的等待项(连接结果到达时会被跳过)."""
        for key, waiters in list(self._pending.items()):
            waiters[:] = [(p, pt, cb) for (p, pt, cb) in waiters if p is not pane]
            if not self._pending[key]:
                del self._pending[key]
                active = self._dialogs.pop(id(waiters), None)
                if active is not None:
                    dialog, finish = active
                    finish(None)
                    dialog.close()

    def stop_connecting(self):
        self.closing = True
        for waiters in list(self._pending.values()):
            for pane, _, on_done in list(waiters):
                self.cancel_pending(pane)
                if _pane_alive(pane):
                    on_done(None, None, "连接已取消")

    def shutdown(self):
        self.stop_connecting()
        entries = list(self._live.values())
        self._live.clear()
        for entry in entries:
            _safe_disconnect(entry.backend)
