"""共享连接池: 同一服务器(host,port,username)在多个标签页间复用同一条连接.

- request_connect: 已连接 → 立即复用; 连接进行中 → 挂起等待; 否则新建(含指纹/认证对话框)
- release: 引用计数, 最后一个使用方释放时才真正断开
- mark_dead: 连接死亡 → 通知所有使用它的面板
"""
from __future__ import annotations

import threading

from gi.repository import GLib

from .backend.base import BackendError
from .backend.sftp import AuthNeeded, HostKeyUnknown, SftpBackend
from .connect_dialog import AuthDialog, HostKeyDialog

MAX_CONNECT_RETRIES = 8


class _Entry:
    __slots__ = ("backend", "cfg", "users")

    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = cfg
        self.users: set = set()


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

    @staticmethod
    def key(cfg):
        return (cfg.get("host"), int(cfg.get("port") or 22), cfg.get("username") or "")

    # ------------------------------------------------------------------
    # 请求连接. on_done(backend, path, error): 成功时 error=None.
    # ------------------------------------------------------------------
    def request_connect(self, pane, cfg, creds, path, on_done):
        key = self.key(cfg)
        entry = self._live.get(key)
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
                         args=(cfg, creds or {}), daemon=True).start()

    def _connect_thread(self, cfg, creds):
        backend = SftpBackend(
            cfg["host"], cfg.get("port", 22), cfg.get("username"),
            cfg.get("auth_method", "key"), cfg.get("key_path"),
            password=creds.get("password"), passphrase=creds.get("passphrase"),
        )
        accepted = None
        try:
            for _attempt in range(MAX_CONNECT_RETRIES):
                try:
                    backend.connect(accepted_key=accepted)
                    break
                except HostKeyUnknown as e:
                    ok = self.window.blocking_dialog(
                        lambda done, _e=e: HostKeyDialog(
                            self.window, done, backend.host,
                            _e.key_type.replace("ssh-", ""), _e.fingerprint))
                    if not ok:
                        self._dispatch_fail(cfg, "已拒绝主机密钥")
                        return
                    accepted = e.key
                except AuthNeeded as e:
                    got = self.window.blocking_dialog(
                        lambda done, _e=e: AuthDialog(self.window, done, _e.kind, str(_e)))
                    if not got:
                        self._dispatch_fail(cfg, "缺少凭据, 已取消连接")
                        return
                    if got.get("password"):
                        backend.password = got["password"]
                    if got.get("passphrase"):
                        backend.passphrase = got["passphrase"]
                except BackendError as e:
                    self._dispatch_fail(cfg, str(e))
                    return
                except Exception as e:
                    self._dispatch_fail(cfg, f"连接失败: {e}")
                    return
            else:
                self._dispatch_fail(cfg, "多次尝试后仍无法连接")
                return
        except Exception as e:  # 防御: 保证 waiters 总能收到结果
            self._dispatch_fail(cfg, f"连接异常: {e}")
            return
        GLib.idle_add(self._dispatch_ok, cfg, backend)

    def _dispatch_ok(self, cfg, backend):
        key = self.key(cfg)
        waiters = self._pending.pop(key, [])
        entry = _Entry(backend, cfg)
        self._live[key] = entry
        for pane, path, on_done in waiters:
            if _pane_alive(pane):
                entry.users.add(pane)
                on_done(backend, path, None)
        if not entry.users:
            # 请求者都已关闭: 不要留下孤儿连接
            self._live.pop(key, None)
            _safe_disconnect(backend)
        return GLib.SOURCE_REMOVE

    def _dispatch_fail(self, cfg, msg):
        GLib.idle_add(self._dispatch_fail_ui, self.key(cfg), msg)

    def _dispatch_fail_ui(self, key, msg):
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
                if not entry.users:
                    del self._live[key]
                    _safe_disconnect(backend)
                return

    def mark_dead(self, backend, msg):
        """连接已死亡: 移除并通知所有使用它的面板."""
        for key, entry in list(self._live.items()):
            if entry.backend is backend:
                del self._live[key]
                users = list(entry.users)
                entry.users.clear()
                _safe_disconnect(backend)
                for pane in users:
                    if _pane_alive(pane):
                        pane.connection_lost_ui(msg)
                return

    def cancel_pending(self, pane):
        """标签关闭: 丢弃该面板的等待项(连接结果到达时会被跳过)."""
        for key, waiters in list(self._pending.items()):
            self._pending[key] = [(p, pt, cb) for (p, pt, cb) in waiters if p is not pane]
            if not self._pending[key]:
                del self._pending[key]
