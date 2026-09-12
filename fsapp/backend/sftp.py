"""SFTP 远程后端 (paramiko 封装).

- 连接/认证在 worker 线程执行; 未知主机抛 HostKeyUnknown, 主机密钥被替换抛
  HostKeyChanged(绝不自动接受), 缺凭据抛 AuthNeeded, 由 UI 层弹框后处理.
- known_hosts 缺失时创建(0700/0600)并写回用户确认的密钥; 读写受限只提示不阻断.
- 所有 SFTP 操作持连接级锁; 大文件按块读写, 每块持锁,
  因此同一连接上的多个任务按块交错推进(安全且各自有进度).
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import stat as stat_mod
import threading

import paramiko

from .base import BaseBackend, BackendError, FileEntry, perms_str

DEFAULT_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")


def fingerprint_key(key) -> str:
    """OpenSSH 风格 SHA256 指纹, 可与 ssh/ssh-keygen 输出直接比对."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class HostKeyUnknown(Exception):
    """主机密钥不在 known_hosts 中, 携带指纹供用户确认.

    note 非空表示该主机已有其他类型的记录(密钥类型升级), 确认时需一并展示.
    """

    def __init__(self, key, hostname: str = "", note: str = ""):
        super().__init__("host key unknown")
        self.key = key
        self.hostname = hostname
        self.note = note
        self.fingerprint = fingerprint_key(key)
        self.key_type = key.get_name()


class HostKeyChanged(Exception):
    """known_hosts 中同类型密钥与服务器提供的不一致: 密钥轮换或中间人.

    安全策略: 绝不自动接受, 也不提供"这次相信"入口; 只能拒绝并由用户
    人工核对后更新 known_hosts。
    """

    def __init__(self, hostname: str, key_type: str, expected: str, got: str):
        super().__init__(f"{hostname} 的主机密钥与已知记录不一致")
        self.hostname = hostname
        self.key_type = key_type
        self.expected = expected
        self.got = got


class AuthNeeded(Exception):
    """缺少或错误的凭据: kind = 'password' | 'passphrase' | 'both'."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class _RejectUnknown(paramiko.MissingHostKeyPolicy):
    """未知主机密钥: 不静默接受, 抛出供 UI 确认."""

    def missing_host_key(self, client, hostname, key):
        raise HostKeyUnknown(key, hostname)


def prepare_known_hosts(path: str) -> str:
    """确保 ~/.ssh 与 known_hosts 存在(0700/0600); 返回错误说明('' = 成功)."""
    try:
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.isdir(path):
            return f"{path} 是一个目录, 无法作为 known_hosts 使用"
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        if not os.access(path, os.R_OK | os.W_OK):
            return f"{path} 不可读写, 新确认的主机密钥不会被保存"
    except OSError as e:
        return f"无法准备 {path}: {e.strerror or e}"
    return ""


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
    supports_links = True    # 服务器不支持时具体调用会抛错, 传输层按内容回退

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
        self.host_key_note = ""

    @property
    def label(self):
        u = self.username or os.environ.get("USER", "user")
        return f"{u}@{self.host}"

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def connect(self, accepted_key=None):
        """阻塞连接. accepted_key: 用户已确认的主机密钥(首连确认后传入).

        抛出: HostKeyUnknown(新密钥待确认) / HostKeyChanged(已记录密钥被替换,
        只能拒绝) / AuthNeeded / BackendError. 读写 known_hosts 受阻不抛异常,
        而是记录到 self.host_key_note 供 UI 提示。
        """
        client = paramiko.SSHClient()
        self.host_key_note = ""
        kh = os.path.expanduser("~/.ssh/known_hosts")
        note = prepare_known_hosts(kh)
        if note:
            self.host_key_note = note
        else:
            try:
                client.load_host_keys(kh)  # 可写加载: 新密钥可保存回文件
            except (IOError, OSError) as e:
                self.host_key_note = f"无法读取 {kh}: {e}"
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
        except paramiko.BadHostKeyException as e:
            client.close()
            raise self._host_key_error(e) from None
        except paramiko.SSHException as e:
            client.close()
            raise BackendError(f"无法连接 {self.host}:{self.port} — {e}") from None
        except (OSError, EOFError) as e:
            client.close()
            raise BackendError(f"无法连接 {self.host}:{self.port} — {e}") from None

        if accepted_key is not None:
            try:
                client.save_host_keys(kh)
            except (IOError, OSError) as e:
                self.host_key_note = f"主机密钥未能写入 {kh}: {e}"

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

    def _host_key_error(self, error):
        """把 paramiko 的密钥异常分成两类: 新密钥待确认 / 同类型密钥被替换.

        类型不同通常是服务器新增了一种主机密钥, 仍按首连确认处理;
        类型相同而内容不同才是不一致, 必须拒绝而不是让用户"这次相信"。
        """
        # paramiko 5 用 .key 表示服务器提供的密钥(旧版叫 server_key)
        server = getattr(error, "key", None) or getattr(error, "server_key", None)
        expected = getattr(error, "expected_key", None)
        got_type = server.get_name() if server is not None else ""
        if expected is not None and server is not None and \
                expected.get_name() != got_type:
            return HostKeyUnknown(
                server, self.host,
                note=f"该主机已记录 {expected.get_name().replace('ssh-', '')} "
                     f"密钥({fingerprint_key(expected)}); 服务器同时提供了新的密钥类型")
        return HostKeyChanged(
            self.host,
            got_type.replace("ssh-", ""),
            fingerprint_key(expected) if expected is not None else "无记录",
            fingerprint_key(server) if server is not None else "无记录")

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
        return [self._entry(self.join(path, a.filename), a) for a in attrs]

    def _entry(self, path, attrs) -> FileEntry:
        """SFTPAttributes → FileEntry.

        服务器可能省略 size/mtime 等字段(甚至只回 mode), 因此全部按缺省取,
        不能假设字段一定存在。
        """
        mode = int(getattr(attrs, "st_mode", 0) or 0)
        return FileEntry(
            name=self.basename(path),
            path=self.normpath(path),
            size=int(getattr(attrs, "st_size", 0) or 0),
            mtime=float(getattr(attrs, "st_mtime", 0.0) or 0.0),
            is_dir=stat_mod.S_ISDIR(mode),
            perms=perms_str(mode),
            is_link=stat_mod.S_ISLNK(mode),
            mode=mode,
        )

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
        return self._entry(path, a)

    def lstat(self, path):
        """不跟随链接的 stat: 传输层据此区分链接/目录/普通文件."""
        self._check()
        with self._lock:
            try:
                a = self.sftp.lstat(path)
            except FileNotFoundError:
                return None
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(self._errmsg(e)) from None
        return self._entry(path, a)

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

    def read_link(self, path):
        self._check()
        with self._lock:
            try:
                return self._fsname(self.sftp.readlink(path))
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"读取链接失败: {self._errmsg(e)}") from None

    def make_symlink(self, target, path):
        self._check()
        with self._lock:
            try:
                self.sftp.symlink(target, path)
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"创建符号链接失败: {self._errmsg(e)}") from None

    def set_metadata(self, path, mode=None, mtime=None):
        """尽力还原权限与修改时间; 链接权限会被服务器跟随, 因此跳过."""
        self._check()
        with self._lock:
            try:
                if mode:
                    self.sftp.chmod(path, stat_mod.S_IMODE(mode))
                if mtime is not None:
                    self.sftp.utime(path, (int(mtime), int(mtime)))
            except (OSError, paramiko.SSHException, EOFError) as e:
                self._check_alive()
                raise BackendError(f"保留权限/时间戳失败: {self._errmsg(e)}") from None

    @staticmethod
    def _fsname(value) -> str:
        """SFTP readlink 可能返回 bytes: 统一成 str 供 symlink 使用."""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

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

