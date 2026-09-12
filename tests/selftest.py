"""无 GUI 自测: LocalBackend + TransferManager 的核心传输逻辑.

覆盖: 本地↔本地 文件/目录递归复制、空文件、取消、速度计算、
执行计划的链接/权限信息、新建与重命名的名称校验.
运行: .venv/bin/python tests/selftest.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsapp.backend.local import LocalBackend  # noqa: E402
from fsapp.transfer import TransferManager  # noqa: E402
from fsapp.util import fmt_size  # noqa: E402


def make_tree(root):
    os.makedirs(f"{root}/src/dir/sub")
    with open(f"{root}/src/a.txt", "wb") as f:
        f.write(b"hello" * 1000)          # 5000 B
    with open(f"{root}/src/dir/b.bin", "wb") as f:
        f.write(os.urandom(300 * 1024))    # 300 KiB
    with open(f"{root}/src/dir/sub/empty", "wb"):
        pass                               # 0 B
    with open(f"{root}/src/.hidden", "wb") as f:
        f.write(b"x")


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def main():
    root = tempfile.mkdtemp(prefix="fstransfer-test-")
    try:
        make_tree(root)
        be = LocalBackend()

        # ---- backend 基础 ----
        entries = be.list_dir(f"{root}/src")
        names = {e.name for e in entries}
        check({"a.txt", "dir", ".hidden"} <= names, "list_dir")
        check(any(e.is_dir for e in entries if e.name == "dir"), "is_dir")
        st = be.stat(f"{root}/src/a.txt")
        check(st is not None and st.size == 5000, "stat")
        check(be.exists(f"{root}/src/a.txt"), "exists")
        dirs, files, total = be.walk(f"{root}/src")
        check(set(dirs) == {"dir", "dir/sub"}, f"walk dirs {dirs}")
        check(total == 5000 + 300 * 1024 + 1, f"walk total={total}")  # 含 .hidden 1B
        check(be.basename("/a/b/c.txt") == "c.txt", "basename")
        check(be.parent("/a/b/c.txt") == "/a/b", "parent")

        # ---- 执行计划: 链接与目录分开, 不把链接当目录展开 ----
        os.makedirs(f"{root}/plan/realdir")
        with open(f"{root}/plan/realdir/r.txt", "wb") as f:
            f.write(b"R")
        with open(f"{root}/plan/plain.txt", "wb") as f:
            f.write(b"P" * 7)
        os.symlink("plain.txt", f"{root}/plan/filelink")
        os.symlink("realdir", f"{root}/plan/dirlink")
        os.symlink("gone", f"{root}/plan/dangling")
        plan = be.walk_plan(f"{root}/plan")
        check({i.rel_path for i in plan.links}
              == {"filelink", "dirlink", "dangling"},
              f"walk_plan 单独收集链接 {[i.rel_path for i in plan.links]}")
        check({i.rel_path for i in plan.files} == {"plain.txt", "realdir/r.txt"},
              f"walk_plan 不跟随链接展开内容 {[i.rel_path for i in plan.files]}")
        check(plan.dirs == ["realdir"], f"walk_plan 只收集真实目录 {plan.dirs}")
        check(plan.total == 8, f"walk_plan 字节量不含链接目标 {plan.total}")
        check(all(i.mode for i in plan.files), "walk_plan 带上权限位供还原")
        link_stat = be.lstat(f"{root}/plan/dirlink")
        check(link_stat is not None and link_stat.is_link and not link_stat.is_dir,
              "lstat 不把目录链接当目录")
        check(be.stat(f"{root}/plan/dirlink").is_dir, "stat 仍跟随链接(浏览用)")

        # ---- 名称校验: 阻止新建/重命名路径穿越 ----
        from fsapp.util import validate_item_name
        check(validate_item_name("正常文件 名.txt") == ("正常文件 名.txt", ""),
              "普通名称可用")
        check(validate_item_name("  a.txt  ")[0] == "a.txt", "首尾空白会被去掉")
        for bad, why in (("../run", "上级目录"), ("a/b", "子路径"), ("/abs", "绝对路径"),
                         (".", "当前目录"), ("..", "上级目录"), ("x\ty", "制表符"),
                         ("", "空白"), ("   ", "全空白")):
            name, error = validate_item_name(bad)
            check(bool(error), f"拒绝 {why}: {bad!r} → {error}")
        check(validate_item_name(".hidden")[1] == "", "隐藏文件名仍然允许")
        check(validate_item_name("带/斜杠 的名字")[1] != "", "含斜杠一律拒绝")

        # ---- 传输: 单文件 ----
        mgr = TransferManager()
        t1 = mgr.enqueue(be, [f"{root}/src/a.txt"], be, f"{root}/dst")
        wait_done(mgr, t1, "单文件")
        check(t1.status == "done", "单文件完成")
        check(os.path.getsize(f"{root}/dst/a.txt") == 5000, "单文件内容大小")
        check(t1.total_bytes == 5000, f"单文件 total={t1.total_bytes}")

        # ---- 传输: 目录递归 ----
        t2 = mgr.enqueue(be, [f"{root}/src"], be, f"{root}/dst")
        wait_done(mgr, t2, "目录递归")
        check(t2.status == "done", "目录完成")
        check(t2.total_bytes == 5000 + 300 * 1024 + 1, f"目录 total={t2.total_bytes}")
        check(os.path.isfile(f"{root}/dst/src/dir/sub/empty"), "空文件已复制")
        check(os.path.getsize(f"{root}/dst/src/dir/b.bin") == 300 * 1024, "大文件内容一致")
        with open(f"{root}/src/dir/b.bin", "rb") as a, open(f"{root}/dst/src/dir/b.bin", "rb") as b:
            check(a.read() == b.read(), "二进制内容一致")

        # ---- 覆盖确认: 全部跳过 ----
        mgr.ask_overwrite = lambda names: "skip"
        t3 = mgr.enqueue(be, [f"{root}/src/a.txt"], be, f"{root}/dst")
        wait_done(mgr, t3, "跳过冲突")
        check(t3.status == "done" and t3.total_bytes == 0, f"跳过后 total={t3.total_bytes}")

        # ---- 覆盖确认: 覆盖 ----
        mgr.ask_overwrite = lambda names: "overwrite"
        with open(f"{root}/src/a.txt", "wb") as f:
            f.write(b"new-content")
        t4 = mgr.enqueue(be, [f"{root}/src/a.txt"], be, f"{root}/dst")
        wait_done(mgr, t4, "覆盖冲突")
        with open(f"{root}/dst/a.txt", "rb") as f:
            check(f.read() == b"new-content", "覆盖后内容一致")

        # ---- 取消: 启动后立即取消 ----
        mgr.ask_overwrite = None
        with open(f"{root}/big.bin", "wb") as f:
            f.write(os.urandom(20 * 1024 * 1024))  # 20 MB
        t5 = mgr.enqueue(be, [f"{root}/big.bin"], be, f"{root}/dst")
        mgr.cancel(t5.id)
        wait_done(mgr, t5, "取消")
        check(t5.status == "cancelled", f"取消状态={t5.status}")
        check(not os.path.exists(f"{root}/dst/big.bin"), "取消后无半成品")

        # ---- 速度格式 ----
        check(fmt_size(0) == "0 B", "fmt_size 0")
        check(fmt_size(1536) == "1.5 KB", "fmt_size 1.5KB")
        check(fmt_size(3 * 1024 * 1024) == "3.0 MB", "fmt_size 3MB")

        print("\n全部通过 ✅")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def wait_done(mgr, t, what, timeout=30):
    start = time.monotonic()
    while t.running and time.monotonic() - start < timeout:
        time.sleep(0.05)
    if t.running:
        check(False, f"{what} 超时")
    elif t.status == "error":
        check(False, f"{what} 出错: {t.error}")


if __name__ == "__main__":
    main()
