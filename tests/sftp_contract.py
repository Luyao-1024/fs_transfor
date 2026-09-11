"""无 GUI SFTP 契约回归: 链接删除/关闭错误传播/严格提交.

用 object.__new__(SftpBackend) + 记录型 mock 构造后端, 不连真实服务器.
运行: .venv/bin/python tests/sftp_contract.py
对应 docs/PROJECT_IMPROVEMENT_PLAN.md SAFE-03/04/05.
"""
import os
import stat as stat_mod
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsapp.backend.base import BackendError  # noqa: E402
from fsapp.backend.sftp import SftpBackend, _LockedFile  # noqa: E402


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


class FakeAttr:
    def __init__(self, mode):
        self.st_mode = mode


class MockSFTP:
    """记录调用的 SFTP mock; entries: 路径 → st_mode."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def lstat(self, path):
        self.calls.append(("lstat", path))
        if path not in self.entries:
            raise FileNotFoundError(path)
        return FakeAttr(self.entries[path])

    def stat(self, path):
        self.calls.append(("stat", path))
        if path not in self.entries:
            raise FileNotFoundError(path)
        return FakeAttr(self.entries[path])

    def listdir(self, path):
        self.calls.append(("listdir", path))
        return [p.rsplit("/", 1)[-1] for p in self.entries
                if p != path and p.startswith(path.rstrip("/") + "/")
                and p.count("/") == path.count("/") + 1]

    def remove(self, path):
        self.calls.append(("remove", path))
        if path not in self.entries:
            raise FileNotFoundError(path)
        self.entries.pop(path)

    def rmdir(self, path):
        self.calls.append(("rmdir", path))
        if path not in self.entries:
            raise FileNotFoundError(path)
        self.entries.pop(path)

    def posix_rename(self, a, b):
        self.calls.append(("posix_rename", a, b))
        raise OSError("unsupported")  # 模拟服务器不支持该扩展

    def rename(self, a, b):
        self.calls.append(("rename", a, b))


class FakeClient:
    def get_transport(self):
        class T:
            def is_active(self):
                return True
        return T()


def make_backend(entries):
    be = object.__new__(SftpBackend)
    be.sftp = MockSFTP(entries)
    be.client = FakeClient()
    be._lock = threading.RLock()
    be._home = None
    return be


def calls_for(mock, op, path):
    return [c for c in mock.calls if c[0] == op and (path is None or path in c[1:])]


def main():
    # ---- SAFE-04: 链接删除不触及目标 ----
    LNK = stat_mod.S_IFLNK | 0o777
    DIR = stat_mod.S_IFDIR | 0o755
    REG = stat_mod.S_IFREG | 0o644

    # 文件链接
    be = make_backend({"/r/link": LNK, "/r/target.txt": REG})
    be.delete("/r/link")
    mock = be.sftp
    check(("lstat", "/r/link") in mock.calls, "文件链接: 使用 lstat")
    check(("remove", "/r/link") in mock.calls, "文件链接: 只删链接本身")
    check(not calls_for(mock, "remove", "/r/target.txt"), "文件链接: 目标未被删除")
    check(not calls_for(mock, "listdir", "/r/link"), "文件链接: 未进入链接内部")

    # 目录链接: 链接指向目录, 不得递归删除目标内容
    be = make_backend({"/r/dlink": LNK, "/r/realdir": DIR,
                       "/r/realdir/data.txt": REG})
    be.delete("/r/dlink")
    mock = be.sftp
    check(("remove", "/r/dlink") in mock.calls, "目录链接: 只删链接本身")
    check(not calls_for(mock, "listdir", "/r/dlink"), "目录链接: 未递归进入链接")
    check(not calls_for(mock, "remove", "/r/realdir/data.txt"), "目录链接: 目标内容未被删除")
    check("/r/realdir/data.txt" in mock.entries, "目录链接: 目标文件仍在")

    # 悬空链接
    be = make_backend({"/r/dangling": LNK})
    be.delete("/r/dangling")
    check(("remove", "/r/dangling") in be.sftp.calls, "悬空链接: 正常删除链接")

    # 循环链接: 目录内指向自身的链接
    be = make_backend({"/r/root": DIR, "/r/root/loop": LNK})
    be.delete("/r/root")
    mock = be.sftp
    check(("remove", "/r/root/loop") in mock.calls, "循环链接: 链接被删除")
    check(("rmdir", "/r/root") in mock.calls, "循环链接: 目录本身被删除")
    check(not any(c[0] == "listdir" and c[1] == "/r/root/loop" for c in mock.calls),
          "循环链接: 无限递归未发生")

    # 普通目录: 递归 + rmdir 保持正确
    be = make_backend({"/r/dir": DIR, "/r/dir/sub": DIR,
                       "/r/dir/sub/f.txt": REG, "/r/dir/g.txt": REG})
    be.delete("/r/dir")
    mock = be.sftp
    check("/r/dir/sub/f.txt" not in mock.entries and "/r/dir/g.txt" not in mock.entries,
          "普通目录: 子内容全部删除")
    check(("rmdir", "/r/dir/sub") in mock.calls and ("rmdir", "/r/dir") in mock.calls,
          "普通目录: 自底向上 rmdir")

    # ---- SAFE-05: _LockedFile.close 传播异常 ----
    class BoomFH:
        def close(self):
            raise OSError("delayed pipe error")

        def read(self, n):
            return b""

        def write(self, b):
            return len(b)

    lf = _LockedFile(BoomFH(), threading.RLock())
    try:
        lf.close()
        check(False, "close 异常应传播")
    except OSError as e:
        check("delayed" in str(e), f"close 异常已传播({e})")

    # ---- SAFE-03: 严格提交 ----
    # posix-rename 不可用 + 目标存在 → 必须安全失败, 不得覆盖
    be = make_backend({"/r/exists.txt": REG})
    try:
        be.commit_temp("/r/.tmp-1", "/r/exists.txt")
        check(False, "目标存在且无 posix-rename 应报错")
    except BackendError as e:
        check("原子覆盖" in str(e), f"严格提交报错({e})")
    check(("rename", "/r/.tmp-1", "/r/exists.txt") not in be.sftp.calls,
          "未发生危险的普通 rename 覆盖")
    check("/r/exists.txt" in be.sftp.entries, "旧目标仍在")

    # posix-rename 不可用 + 目标不存在 → 普通 rename 安全放行
    be = make_backend({})
    be.commit_temp("/r/.tmp-2", "/r/new.txt")
    check(("rename", "/r/.tmp-2", "/r/new.txt") in be.sftp.calls,
          "目标不存在: 普通 rename 放行")

    # posix-rename 可用 → 直接提交
    class PosixSFTP(MockSFTP):
        def posix_rename(self, a, b):
            self.calls.append(("posix_rename", a, b))
            self.entries.pop(a, None)
            self.entries[b] = stat_mod.S_IFREG | 0o644

    be = make_backend({"/r/old.txt": REG})
    be.sftp.__class__ = PosixSFTP
    be.commit_temp("/r/.tmp-3", "/r/old.txt")
    check(("posix_rename", "/r/.tmp-3", "/r/old.txt") in be.sftp.calls,
          "posix-rename 可用时原子提交")

    # ---- discard_temp: 尽力删除, 失败返回路径 ----
    be = make_backend({"/r/.tmp-4": REG})
    check(be.discard_temp("/r/.tmp-4") is None, "discard 成功返回 None")
    check(be.discard_temp("/r/.tmp-gone") == "/r/.tmp-gone", "discard 失败返回路径")

    print("\nsftp_contract 全部通过 ✅")


if __name__ == "__main__":
    main()
