"""无 GUI 主机密钥契约: OpenSSH 风格指纹、未知/变化区分、known_hosts 创建与写回.

用替身替换 paramiko.SSHClient, 并把 HOME 指向临时目录, 不触碰真实 ~/.ssh,
也不连接任何服务器。
运行: .venv/bin/python tests/hostkey_policy.py
对应 docs/PROJECT_IMPROVEMENT_PLAN.md 7.4 主机密钥项.
"""
import base64
import hashlib
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko  # noqa: E402

from fsapp.backend.sftp import (HostKeyChanged, HostKeyUnknown,  # noqa: E402
                                SftpBackend, _RejectUnknown,
                                fingerprint_key, prepare_known_hosts)


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}", flush=True)


class FakeHostKeys:
    def __init__(self):
        self.added = []

    def add(self, host, key_type, key):
        self.added.append((host, key_type))


class FakeChannel:
    def settimeout(self, _seconds):
        pass


class FakeSFTP:
    def get_channel(self):
        return FakeChannel()


class FakeClient:
    """记录 known_hosts 读写与连接尝试的 SSHClient 替身."""

    instances = []

    def __init__(self):
        self.host_keys = FakeHostKeys()
        self.loaded = []
        self.saved = []
        self.closed = 0
        self.policy = None
        self.connect_error = None
        self.save_error = None
        self.load_error = None
        FakeClient.instances.append(self)

    def load_host_keys(self, path):
        if self.load_error:
            raise IOError(self.load_error)
        self.loaded.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self.host_keys

    def connect(self, **kwargs):
        if self.connect_error:
            raise self.connect_error

    def get_transport(self):
        return None

    def open_sftp(self):
        return FakeSFTP()

    def save_host_keys(self, path):
        if self.save_error:
            raise OSError(self.save_error)
        self.saved.append(path)

    def close(self):
        self.closed += 1


def make_backend(home, **kwargs):
    os.environ["HOME"] = home
    backend = SftpBackend(kwargs.get("host", "example.test"),
                          kwargs.get("port", 22), "tester", "key")
    return backend


def main():
    root = tempfile.mkdtemp(prefix="fstransfor-hostkey-")
    home = os.path.join(root, "home")
    os.makedirs(home)
    original_home = os.environ.get("HOME")
    original_client = paramiko.SSHClient
    try:
        key = paramiko.RSAKey.generate(2048)
        other = paramiko.RSAKey.generate(2048)
        ecdsa = paramiko.ECDSAKey.generate()

        # ---- 指纹格式与算法 ----
        fp = fingerprint_key(key)
        expected = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        check(fp == expected, f"指纹为 OpenSSH 风格 SHA256 ({fp[:16]}…)")
        check(fp.startswith("SHA256:") and "=" not in fp and len(fp) == 50,
              "指纹前缀正确且无 base64 填充")
        check(":" not in fp[7:], "指纹不是 MD5 冒号分组格式")

        # 与系统 ssh-keygen 的输出交叉核对(可用时)
        pub = os.path.join(root, "id_rsa.pub")
        with open(pub, "w", encoding="ascii") as stream:
            stream.write(f"ssh-rsa {key.get_base64()} test\n")
        try:
            out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", pub],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0:
            shown = out.stdout.strip().split()[1]
            check(shown == fp, f"与 ssh-keygen 输出一致 ({shown[:16]}…)")
        else:
            print("skip: 本机没有可用的 ssh-keygen, 未做外部交叉核对")

        # ---- 未知主机: 抛 HostKeyUnknown 并带上主机名与指纹 ----
        try:
            _RejectUnknown().missing_host_key(FakeClient(), "[hub.test]:2222", key)
            check(False, "未知主机密钥必须被拒绝")
        except HostKeyUnknown as error:
            check(error.hostname == "[hub.test]:2222" and error.key is key,
                  "未知主机异常携带主机标识与密钥对象")
            check(error.fingerprint == fp, "未知主机异常携带 SHA256 指纹")

        # ---- 密钥被替换 vs 新增密钥类型 ----
        backend = make_backend(home)
        changed = backend._host_key_error(
            paramiko.BadHostKeyException("example.test", key, other))
        check(isinstance(changed, HostKeyChanged),
              "同类型密钥内容不同 → HostKeyChanged(不允许本次放行)")
        check(changed.expected == fingerprint_key(other)
              and changed.got == fingerprint_key(key) and changed.key_type == "rsa",
              "变化异常同时携带已记录与实际收到的指纹")
        check(key.get_base64() not in str(changed)
              and other.get_base64() not in str(changed),
              "错误信息不倾倒整段公钥(避免污染提示条)")
        upgraded = backend._host_key_error(
            paramiko.BadHostKeyException("example.test", ecdsa, other))
        check(isinstance(upgraded, HostKeyUnknown) and bool(upgraded.note),
              "服务器新增密钥类型 → 按首连确认处理并说明已有记录")

        # ---- known_hosts 缺失时创建, 确认后写回 ----
        paramiko.SSHClient = FakeClient
        kh = os.path.join(home, ".ssh", "known_hosts")
        check(not os.path.exists(kh), "测试前 known_hosts 不存在")
        backend = make_backend(home)
        backend.connect(accepted_key=key)
        check(os.path.exists(kh), "首次确认密钥时创建 known_hosts")
        check(stat_mod.S_IMODE(os.stat(kh).st_mode) == 0o600,
              "known_hosts 权限为 0600")
        check(stat_mod.S_IMODE(os.stat(os.path.dirname(kh)).st_mode) == 0o700,
              "~/.ssh 目录权限为 0700")
        client = FakeClient.instances[-1]
        check(client.saved == [kh], "确认的主机密钥被写回 known_hosts")
        check(not backend.host_key_note, "正常写回时没有告警")

        # 已存在的 known_hosts 会被加载并保留
        backend = make_backend(home)
        backend.connect()
        client = FakeClient.instances[-1]
        check(client.loaded == [kh], "已存在的 known_hosts 被加载校验")

        # ---- 写回失败: 连接仍可用, 但必须报告原因 ----
        class SaveFails(FakeClient):
            def save_host_keys(self, path):
                raise OSError("Read-only file system")

        paramiko.SSHClient = SaveFails
        backend = make_backend(home)
        backend.connect(accepted_key=key)
        check("known_hosts" in backend.host_key_note
              and "Read-only" in backend.host_key_note,
              f"写回失败被记录为可提示的说明({backend.host_key_note})")
        check(backend.sftp is not None, "写回失败不阻断本次连接")

        # ---- 替换密钥时: 拒绝连接并关闭 transport ----
        class KeyMismatch(FakeClient):
            def connect(self, **kwargs):
                raise paramiko.BadHostKeyException("example.test", key, other)

        paramiko.SSHClient = KeyMismatch
        backend = make_backend(home)
        try:
            backend.connect()
            check(False, "主机密钥不一致时不能连接成功")
        except HostKeyChanged as error:
            check(error.key_type == "rsa" and error.got == fingerprint_key(key),
                  "不一致异常向 UI 提供类型与指纹")
        check(FakeClient.instances[-1].closed == 1, "拒绝连接时关闭 SSH 客户端")
        paramiko.SSHClient = FakeClient

        # ---- prepare_known_hosts 的失败路径 ----
        ro = os.path.join(root, "readonly")
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        try:
            note = prepare_known_hosts(os.path.join(ro, "sub", "known_hosts"))
            check(bool(note), f"目录不可写时返回说明而不是抛异常({note})")
        finally:
            os.chmod(ro, 0o700)
        blocked = os.path.join(root, "blocked")
        os.makedirs(blocked)
        note = prepare_known_hosts(blocked)
        check(bool(note) and "目录" in note,
              f"known_hosts 位置是目录时返回说明({note})")
    finally:
        paramiko.SSHClient = original_client
        if original_home is not None:
            os.environ["HOME"] = original_home
        shutil.rmtree(root, ignore_errors=True)

    print("\nhostkey_policy 全部通过 ✅")


if __name__ == "__main__":
    main()
