"""SFTP 远程后端 (paramiko 封装).

- 连接/认证在 worker 线程执行; 未知主机抛 HostKeyUnknown, 缺凭据抛 AuthNeeded,
  由 UI 层弹框后重试.
- 所有 SFTP 操作持连接级锁; 大文件按块读写, 每块持锁,
  因此同一连接上的多个任务按块交错推进(安全且各自有进度).
"""
from __future__ import annotations

import os
import posixpath
import stat as stat_mod
import threading

import paramiko

from .base import BaseBackend, BackendError, FileEntry, perms_str

DEFAULT_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")


class HostKeyUnknown(Exception):
    """主机密钥不在 known_hosts 中, 携带指纹供用户确认."""

    def __init__(self, key):
        super().__init__("host key unknown")
        self.key = key
        self.fingerprint = ":".join(f"{b:02x}" for b in key.get_fingerprint())
        self.key_type = key.get_name()


class AuthNeeded(Exception):
    """缺少或错误的凭据: kind = 'password' | 'passphrase' | 'both'."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class _RejectUnknown(paramiko.MissingHostKeyPolicy):
    """未知主机密钥: 不静默接受, 抛出供 UI 确认."""

    def missing_host_key(self, client, hostname, key):
        raise HostKeyUnknown(key)


class _LockedFile:
    """给 SFTP 文件句柄加连接级锁."""

    def __init__(self, fh, lock):
        self._fh = fh
        self._lock = lock

    def read(self, n=-1):
        with self._lock:
            return self._fh.read(n)

    def write(self, b):
        with self._lock:
            return self._fh.write(b)

    def close(self):
        # 必须传播关闭异常: SFTP 管道写入的错误可能延迟到 close 才出现,
        # 吞掉它会让传输层把失败任务标成成功(进而误删移动源).
        with self._lock:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SftpBackend(BaseBackend):
    is_local = False

    def __init__(self, host, port=22, username=None, auth_method="key",
                 key_path=None, password=None, passphrase=None):
        self.host = host
        self.port = int(port or 22)
        self.username = username
        self.auth_method = auth_method  # 'key' | 'password' | 'agent'
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self.password = password
        self.passphrase = passphrase
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self._lock = threading.RLock()
        self._home: str | None = None

    @property
    def label(self):
        u = self.username or os.environ.get("USER", "user")
        return f"{u}@{self.host}"

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def connect(self, accepted_key=None):
        """阻塞连接. accepted_key: 用户已确认的主机密钥(首连确认后传入).

        抛出: HostKeyUnknown / AuthNeeded / BackendError.
        """
        client = paramiko.SSHClient()
        kh = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.exists(kh):
            try:
                client.load_host_keys(kh)  # 可写加载: 新密钥可保存回文件
            except (IOError, OSError):
                pass
        client.set_missing_host_key_policy(_RejectUnknown())

        if accepted_key is not None:
            entry = f"[{self.host}]:{self.port}" if self.port != 22 else self.host
            client.get_host_keys().add(entry, accepted_key.get_name(), accepted_key)

        try:
            self._do_connect(client)
        except paramiko.PasswordRequiredException:
            client.close()
            raise AuthNeeded("passphrase", "密钥文件已加密, 需要口令") from None
        except paramiko.AuthenticationException:
            client.close()
            raise AuthNeeded("both", "认证失败: 可重试密钥口令, 或输入登录密码") from None
        except paramiko.SSHException as e:
            client.close()
            raise BackendError(f"无法连接 {self.host}:{self.port} — {e}") from None
        except (OSError, EOFError) as e:
            client.close()
            raise BackendError(f"无法连接 {self.host}:{self.port} — {e}") from None

        if accepted_key is not None and os.path.exists(kh):
            try:
                client.save_host_keys(kh)
            except (IOError, OSError):
                pass

        self.client = client
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        try:
            self.sftp = client.open_sftp()
            self.sftp.get_channel().settimeout(15)
        except paramiko.SSHException as e:
            client.close()
            raise BackendError(f"打开 SFTP 会话失败: {e}") from None

    def _do_connect(self, client):
        kw = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=15,
            auth_timeout=15,
            banner_timeout=15,
            allow_agent=True,
            look_for_keys=False,
        )
        if self.auth_method == "password" and not self.password:
            raise AuthNeeded("password", "需要登录密码")
        if self.auth_method == "key":
            kp = self.key_path or self._detect_key()
            if kp and os.path.exists(kp):
                kw["key_filename"] = kp
            elif not self.password:
                # 未指定密钥: 让 paramiko 搜索默认路径 + agent
                kw["look_for_keys"] = True
            if self.passphrase:
                kw["passphrase"] = self.passphrase
        if self.auth_method == "password":
            kw["allow_agent"] = False
        if self.password:
            kw["password"] = self.password
        client.connect(**kw)

    def _detect_key(self):
        ssh_dir = os.path.expanduser("~/.ssh")
        for name in DEFAULT_KEY_NAMES:
            p = os.path.join(ssh_dir, name)
            if os.path.exists(p):
                return p
        return None

    def disconnect(self):
        with self._lock:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None
            self.sftp = None
            self.dead = True

    def _check(self):
        if self.sftp is None or self.client is None:
            raise BackendError("未连接")

    def _check_alive(self):
        t = self.client.get_transport() if self.client else None
        self.dead = not (t and t.is_active())

    # ------------------------------------------------------------------
    # 文件操作(每个操作持锁)
    # ------------------------------------------------------------------
    def list_dir(self, path):
        self._check()
        with self._lock:
            try:
                attrs = self.sftp.listdir_attr(path)
            except FileNotFoundError:
                self._check_alive()
                raise BackendError(f"目录不存在: {path}") from None
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(self._errmsg(e)) from None
        out = []
        for a in attrs:
            mode = a.st_mode or 0
            out.append(FileEntry(
                name=a.filename,
                path=self.join(path, a.filename),
                size=a.st_size or 0,
                mtime=a.st_mtime or 0.0,
                is_dir=stat_mod.S_ISDIR(mode),
                perms=perms_str(mode),
                is_link=stat_mod.S_ISLNK(mode),
            ))
        return out

    def stat(self, path):
        self._check()
        with self._lock:
            try:
                a = self.sftp.stat(path)
            except FileNotFoundError:
                return None
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(self._errmsg(e)) from None
        mode = a.st_mode or 0
        return FileEntry(
            name=self.basename(path),
            path=self.normpath(path),
            size=a.st_size or 0,
            mtime=a.st_mtime or 0.0,
            is_dir=stat_mod.S_ISDIR(mode),
            perms=perms_str(mode),
        )

    def exists(self, path):
        try:
            return self.stat(path) is not None
        except BackendError:
            return False

    def open_read(self, path):
        self._check()
        with self._lock:
            try:
                fh = self.sftp.open(path, "rb")
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"打开 {path} 失败: {self._errmsg(e)}") from None
        return _LockedFile(fh, self._lock)

    def open_write(self, path):
        self._check()
        with self._lock:
            try:
                fh = self.sftp.open(path, "wb")
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"创建 {path} 失败: {self._errmsg(e)}") from None
            if hasattr(fh, "set_pipelined"):
                try:
                    fh.set_pipelined(True)  # 管道化写: 大幅提升吞吐
                except Exception:
                    pass
        return _LockedFile(fh, self._lock)

    def mkdir(self, path):
        self._check()
        with self._lock:
            try:
                self.sftp.mkdir(path)
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(self._errmsg(e)) from None

    def delete(self, path):
        self._check()
        with self._lock:
            self._delete_rec(path)

    def _delete_rec(self, path):
        # 用 lstat 判断: 符号链接只删除链接本身, 绝不递归进链接目标
        # (stat 会跟随链接, 曾导致删除目录链接时误删目标内容).
        try:
            st = self.sftp.lstat(path)
        except FileNotFoundError:
            return
        except (OSError, paramiko.SSHException, EOFError) as e:
            raise BackendError(f"删除 {path} 失败: {self._errmsg(e)}") from None
        try:
            if stat_mod.S_ISLNK(st.st_mode or 0):
                self.sftp.remove(path)
            elif stat_mod.S_ISDIR(st.st_mode or 0):
                for name in self.sftp.listdir(path):
                    self._delete_rec(self.join(path, name))
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
        except (OSError, paramiko.SSHException, EOFError) as e:
            self._check_alive()
            raise BackendError(f"删除 {path} 失败: {self._errmsg(e)}") from None

    def rename(self, old, new):
        self._check()
        with self._lock:
            try:
                self.sftp.posix_rename(old, new)
            except (OSError, paramiko.SSHException, EOFError):
                try:
                    self.sftp.rename(old, new)
                except (OSError, paramiko.SSHException, EOFError) as e:
                    self._check_alive()
                    raise BackendError(f"重命名失败: {self._errmsg(e)}") from None

    @staticmethod
    def _errmsg(e) -> str:
        try:
            return e.strerror or str(e)
        except AttributeError:
            return str(e)

    # ------------------------------------------------------------------
    # 路径工具(远端总是 POSIX)
    # ------------------------------------------------------------------
    def home(self):
        if self._home is None:
            with self._lock:
                self._check()
                self._home = self.sftp.normalize(".")
        return self._home

    def parent(self, path):
        return posixpath.dirname(self.normpath(path)) or "/"

    def basename(self, path):
        return posixpath.basename(self.normpath(path))

    def join(self, base, name):
        if not base or base == "/":
            return "/" + name
        return base.rstrip("/") + "/" + name

    def normpath(self, path):
        if not path:
            return "/"
        return posixpath.normpath(path)

    # ------------------------------------------------------------------
    # 同服务器识别与安全提交
    # ------------------------------------------------------------------
    def same_as(self, other) -> bool:
        return (isinstance(other, SftpBackend)
                and (self.host, self.port, self.username or "")
                == (other.host, other.port, other.username or ""))

    def commit_temp(self, temp: str, target: str):
        """严格提交: 优先 posix-rename(原子覆盖); 不可用时仅允许目标不存在
        的普通 rename, 绝不覆盖旧目标(不做先删后提交的危险替换)."""
        self._check()
        with self._lock:
            try:
                self.sftp.posix_rename(temp, target)
                return
            except (OSError, paramiko.SSHException, EOFError):
                pass  # 服务器可能不支持 posix-rename 扩展
            try:
                self.sftp.stat(target)
            except FileNotFoundError:
                try:
                    self.sftp.rename(temp, target)
                except (OSError, paramiko.SSHException, EOFError) as e:
                    self._check_alive()
                    raise BackendError(f"提交 {target} 失败: {self._errmsg(e)}") from None
                return
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"提交 {target} 失败: {self._errmsg(e)}") from None
            raise BackendError(
                f"服务器不支持原子覆盖, 已保留原文件: {target} — 请先删除同名文件后重试")

    def discard_temp(self, temp: str) -> str | None:
        try:
            with self._lock:
                if self.sftp is not None:
                    self.sftp.remove(temp)
        except Exception:
            return temp
        return None

    def exec_cmd(self, cmd):
        """在独立 channel 上执行命令, 立即返回 channel(不持锁)."""
        self._check()
        t = self.client.get_transport()
        if t is None:
            raise BackendError("未连接")
        chan = t.open_session()
        chan.settimeout(60)
        chan.exec_command(cmd)
        return chan
