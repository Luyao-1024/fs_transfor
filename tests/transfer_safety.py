"""无 GUI 安全回归: 自拷贝保护/移动语义/取消不损坏目标/关闭错误/目录收集.

运行: .venv/bin/python tests/transfer_safety.py
对应 docs/PROJECT_IMPROVEMENT_PLAN.md SAFE-01/02/03/05、CORRECT-02.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsapp.backend.base import BackendError  # noqa: E402
from fsapp.backend.local import LocalBackend  # noqa: E402
from fsapp.transfer import TransferManager  # noqa: E402

TEMP_MARK = ".fstransfer-tmp-"


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def main():
    root = tempfile.mkdtemp(prefix="fstransfer-safety-")
    try:
        be = LocalBackend()
        os.makedirs(f"{root}/src")
        os.makedirs(f"{root}/hard")
        os.symlink(f"{root}/src", f"{root}/alt")        # 目录符号链接
        with open(f"{root}/src/a.txt", "wb") as f:
            f.write(b"AAA")
        os.link(f"{root}/src/a.txt", f"{root}/hard/a.txt")  # 硬链接别名
        with open(f"{root}/src/b.txt", "wb") as f:
            f.write(b"BBB")
        os.makedirs(f"{root}/src/emptydir")
        os.makedirs(f"{root}/src/flat")
        with open(f"{root}/src/flat/f.bin", "wb") as f:
            f.write(b"\x00" * 1024)

        def expect_error(paths, dst_dir, what):
            mgr = TransferManager()
            t = mgr.enqueue(be, paths, be, dst_dir)
            check(t is not None and t.status == "error" and not t.running,
                  f"{what}: 任务立即报错")
            check(bool(t.error), f"{what}: 错误信息非空({t.error})")

        # ---- SAFE-02: 自拷贝/自嵌套拒绝 ----
        expect_error([f"{root}/src/a.txt"], f"{root}/src", "同文件复制")
        expect_error([f"{root}/src/emptydir"], f"{root}/src/emptydir", "目录复制进自身")
        expect_error([f"{root}/src"], f"{root}/src/emptydir", "目录复制进其子目录")
        expect_error([f"{root}/src", f"{root}/src/a.txt"], f"{root}/dst-x", "父子源同选")
        expect_error([f"{root}/hard/a.txt"], f"{root}/src", "硬链接别名覆盖")
        expect_error([f"{root}/src/a.txt"], f"{root}/alt", "目录链接别名覆盖")
        # 源保持完好
        with open(f"{root}/src/a.txt", "rb") as f:
            check(f.read() == b"AAA", "拒绝后源文件未损坏")

        # 重名源
        mgr = TransferManager()
        t = mgr.enqueue(be, [f"{root}/src/a.txt", f"{root}/hard/a.txt"], be, f"{root}/dst-x")
        check(t.status == "error" and "重名" in t.error, f"重名源拒绝({t.error})")

        # 完全重复的源: 静默去重后正常执行
        mgr = TransferManager()
        t = mgr.enqueue(be, [f"{root}/src/b.txt", f"{root}/src/b.txt"], be, f"{root}/dst-x")
        wait_done(mgr, t, "重复源去重")
        check(t.status == "done" and os.path.isfile(f"{root}/dst-x/b.txt"),
              "重复源: 去重后正常复制")

        # ---- SAFE-01: 移动语义 ----
        os.makedirs(f"{root}/dst")
        with open(f"{root}/dst/b.txt", "wb") as f:
            f.write(b"OLD-B")

        mgr = TransferManager()
        mgr.ask_overwrite = lambda names: "skip"
        t = mgr.enqueue(be, [f"{root}/src/a.txt", f"{root}/src/b.txt"], be,
                        f"{root}/dst", move=True)
        wait_done(mgr, t, "移动混合跳过")
        check(t.status == "partial", f"移动有跳过 → partial(实际 {t.status})")
        check(os.path.exists(f"{root}/src/b.txt"), "跳过项源文件保留")
        check(not os.path.exists(f"{root}/src/a.txt"), "已提交项源文件已删除")
        with open(f"{root}/dst/b.txt", "rb") as f:
            check(f.read() == b"OLD-B", "跳过项目标保留旧内容")
        with open(f"{root}/dst/a.txt", "rb") as f:
            check(f.read() == b"AAA", "已提交项目标内容正确")

        # 全部跳过: 源完整保留
        with open(f"{root}/src/a.txt", "wb") as f:
            f.write(b"AAA2")
        with open(f"{root}/dst/a.txt", "wb") as f:
            f.write(b"TARGET-A")
        mgr = TransferManager()
        mgr.ask_overwrite = lambda names: "skip"
        t = mgr.enqueue(be, [f"{root}/src/a.txt"], be, f"{root}/dst", move=True)
        wait_done(mgr, t, "移动全跳过")
        check(t.status == "partial", f"移动全跳过 → partial(实际 {t.status})")
        check(os.path.exists(f"{root}/src/a.txt"), "全跳过: 源完整保留")
        with open(f"{root}/dst/a.txt", "rb") as f:
            check(f.read() == b"TARGET-A", "全跳过: 目标保留旧内容")

        # 移动成功: 源删除, 状态 done
        os.remove(f"{root}/dst/a.txt")
        mgr = TransferManager()
        t = mgr.enqueue(be, [f"{root}/src/a.txt"], be, f"{root}/dst", move=True)
        wait_done(mgr, t, "移动成功")
        check(t.status == "done", f"移动成功状态(实际 {t.status})")
        check(not os.path.exists(f"{root}/src/a.txt"), "移动成功: 源已删除")

        # 目录移动: 全部成功才删源
        mgr = TransferManager()
        t = mgr.enqueue(be, [f"{root}/src/flat"], be, f"{root}/dst", move=True)
        wait_done(mgr, t, "目录移动")
        check(t.status == "done", f"目录移动状态(实际 {t.status})")
        check(not os.path.exists(f"{root}/src/flat"), "目录移动: 源目录已删除")
        with open(f"{root}/dst/flat/f.bin", "rb") as f:
            check(f.read() == b"\x00" * 1024, "目录移动: 内容一致")

        # ---- CORRECT-02: 顶层空目录 ----
        mgr = TransferManager()
        t = mgr.enqueue(be, [f"{root}/src/emptydir"], be, f"{root}/dst")
        wait_done(mgr, t, "顶层空目录复制")
        check(t.status == "done", f"空目录复制状态(实际 {t.status})")
        check(os.path.isdir(f"{root}/dst/emptydir"), "顶层空目录在目标端创建")

        # ---- SAFE-03: 首块写入后取消, 已有目标不变 ----
        gate = threading.Event(), threading.Event()
        started, release = gate
        big = os.urandom(2 * 1024 * 1024)
        with open(f"{root}/big.bin", "wb") as f:
            f.write(big)
        with open(f"{root}/dst/big.bin", "wb") as f:
            f.write(b"PRECIOUS-OLD-CONTENT")

        class SlowBackend(LocalBackend):
            def open_read(self, path):
                fh = super().open_read(path)

                class R:
                    def read(self, n):
                        if not started.is_set():
                            started.set()
                            release.wait(10)
                        return fh.read(n)

                    def close(self):
                        return fh.close()
                return R()

        sbe = SlowBackend()
        mgr = TransferManager()
        mgr.ask_overwrite = lambda names: "overwrite"
        t = mgr.enqueue(sbe, [f"{root}/big.bin"], be, f"{root}/dst")
        check(started.wait(10), "慢读同步点已到达")
        mgr.cancel(t.id)
        release.set()
        wait_done(mgr, t, "首块后取消")
        check(t.status == "cancelled", f"取消状态(实际 {t.status})")
        with open(f"{root}/dst/big.bin", "rb") as f:
            check(f.read() == b"PRECIOUS-OLD-CONTENT", "中途取消: 已有目标内容不变")
        leftover = [n for n in os.listdir(f"{root}/dst") if TEMP_MARK in n]
        check(not leftover, f"中途取消: 无临时文件残留({leftover})")

        # ---- SAFE-05: close() 失败 → 任务报错, 不提交, 不覆盖旧目标 ----
        class BadCloseBackend(LocalBackend):
            def open_write(self, path):
                class W:
                    def write(self, b):
                        return len(b)

                    def close(self):
                        raise OSError("磁盘已满(模拟 close 失败)")
                return W()

        bbe = BadCloseBackend()
        mgr = TransferManager()
        mgr.ask_overwrite = lambda names: "overwrite"
        t = mgr.enqueue(be, [f"{root}/src/b.txt"], bbe, f"{root}/dst")
        wait_done_expect_error(mgr, t, "close 失败")
        with open(f"{root}/dst/b.txt", "rb") as f:
            check(f.read() == b"OLD-B", "close 失败: 旧目标内容不变")
        leftover = [n for n in os.listdir(f"{root}/dst") if TEMP_MARK in n]
        check(not leftover, f"close 失败: 临时文件已清理({leftover})")
        check(os.path.exists(f"{root}/src/b.txt"), "close 失败: 源保留")

        # ---- mkdir 失败传播(权限) ----
        ro = f"{root}/ro"
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        try:
            mgr = TransferManager()
            t = mgr.enqueue(be, [f"{root}/src/b.txt"], be, f"{ro}/sub")
            wait_done(mgr, t, "mkdir 权限失败")
            check(t.status == "error" and "创建失败" in t.error,
                  f"mkdir 失败上报(实际 {t.status}/{t.error})")
        finally:
            os.chmod(ro, 0o755)

        print("\ntransfer_safety 全部通过 ✅")
    finally:
        os.chmod(f"{root}/ro", 0o755) if os.path.isdir(f"{root}/ro") else None
        shutil.rmtree(root, ignore_errors=True)


def wait_done(mgr, t, what, timeout=30):
    start = time.monotonic()
    while t.running and time.monotonic() - start < timeout:
        time.sleep(0.02)
    if t.running:
        check(False, f"{what} 超时")


def wait_done_expect_error(mgr, t, what, timeout=30):
    start = time.monotonic()
    while t.running and time.monotonic() - start < timeout:
        time.sleep(0.02)
    if t.running:
        check(False, f"{what} 超时")
    check(t.status == "error", f"{what}: 任务报错(实际 {t.status}: {t.error})")
    check("close" in t.error or "磁盘" in t.error or "OSError" in t.error,
          f"{what}: 错误信息包含原因({t.error})")


if __name__ == "__main__":
    main()
