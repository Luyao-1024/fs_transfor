"""无 GUI 传输韧性回归: 逐项隔离、链接与元数据保留、确认等待不占并发额度.

运行: .venv/bin/python tests/transfer_resilience.py
对应 docs/PROJECT_IMPROVEMENT_PLAN.md TASK-01 逐项结果、7.4 链接/元数据规则、
以及"等待确认不占用工作线程"的生命周期修复.
"""
import os
import shutil
import stat as stat_mod
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib  # noqa: E402

from fsapp.backend.base import BackendError  # noqa: E402
from fsapp.backend.local import LocalBackend  # noqa: E402
from fsapp.transfer import TransferManager  # noqa: E402

TEMP_MARK = ".fstransfer-tmp-"


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}", flush=True)


def drain():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def wait_for(predicate, what, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain()
        if predicate():
            return
        time.sleep(0.01)
    check(False, f"{what} 超时")


def write(path, content="data", mode=None, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)
    if mode is not None:
        os.chmod(path, mode)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class FailingRead(LocalBackend):
    """指定文件名读取失败, 模拟单个不可读项目(权限/消失/IO 错误)."""

    def __init__(self, names):
        super().__init__()
        self.names = set(names)

    def open_read(self, path):
        if self.basename(path) in self.names:
            raise BackendError(f"权限不足: {self.basename(path)}")
        return super().open_read(path)


class NoLinks(LocalBackend):
    supports_links = False


class NoSymlinkExt(LocalBackend):
    def make_symlink(self, target, path):
        raise BackendError("创建符号链接失败: not supported")


class BusyLinks(LocalBackend):
    def make_symlink(self, target, path):
        raise BackendError("目标已存在, 无法创建链接: " + path)


class NoMetadata(LocalBackend):
    def set_metadata(self, path, mode=None, mtime=None):
        raise BackendError("该位置不支持保留权限或时间戳")


def no_temp_left(dirpath):
    for root, _dirs, files in os.walk(dirpath):
        for name in files:
            if TEMP_MARK in name:
                return os.path.join(root, name)
    return None


def main():
    root = tempfile.mkdtemp(prefix="fstransfor-resilience-")
    try:
        # ---- 1. 单项失败只影响该项, 同批其余项目继续 ----
        pkg = os.path.join(root, "pkg")
        write(os.path.join(pkg, "one.txt"), "ONE")
        write(os.path.join(pkg, "two.txt"), "TWO")
        write(os.path.join(pkg, "sub/three.txt"), "THREE")
        write(os.path.join(pkg, "sub/four.txt"), "FOUR")
        out = os.path.join(root, "out")
        manager = TransferManager()
        task = manager.enqueue(FailingRead({"two.txt"}), [pkg], LocalBackend(), out)
        wait_for(lambda: not task.running, "逐项隔离任务")
        check(task.status == "partial", f"目录内单项失败 → 部分完成({task.status})")
        check(task.item_status == {"pkg": "error"}, f"失败目录标记为 error({task.item_status})")
        check(open(os.path.join(out, "pkg/one.txt")).read() == "ONE"
              and open(os.path.join(out, "pkg/sub/three.txt")).read() == "THREE"
              and open(os.path.join(out, "pkg/sub/four.txt")).read() == "FOUR",
              "失败项之后的文件仍然传输(不再整批中止)")
        check(not os.path.exists(os.path.join(out, "pkg/two.txt")), "失败项不产出目标文件")
        check("two.txt" in task.error and task.item_errors["pkg"].startswith("1 个项目失败"),
              f"逐项结果包含失败原因({task.item_errors})")
        check(no_temp_left(out) is None, "失败项只清理自己的临时文件")

        # ---- 2. 移动语义: 目录内有失败项时保留源 ----
        out2 = os.path.join(root, "out2")
        manager = TransferManager()
        task = manager.enqueue(FailingRead({"two.txt"}), [pkg], LocalBackend(), out2,
                               move=True)
        wait_for(lambda: not task.running, "移动部分失败")
        check(task.status == "partial" and os.path.isdir(pkg),
              "目录内有失败项时不删除源目录")
        check(task.item_status["pkg"] == "error", "移动失败项不会被标记为已移动")
        check(os.path.exists(os.path.join(pkg, "two.txt")), "源目录内容保持完整")

        # ---- 3. 目录创建失败只影响该子树, 且不覆盖阻挡的同名文件 ----
        src3 = os.path.join(root, "mix")
        write(os.path.join(src3, "parent/child.txt"), "CHILD")
        write(os.path.join(src3, "ok.txt"), "OK")
        out3 = os.path.join(root, "out3")
        os.makedirs(out3)
        blocker = write(os.path.join(out3, "parent"), "I-AM-A-FILE")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(),
                               [os.path.join(src3, "parent"),
                                os.path.join(src3, "ok.txt")],
                               LocalBackend(), out3)
        wait_for(lambda: not task.running, "目录创建失败隔离")
        check(task.status == "partial" and task.item_status["ok.txt"] == "committed"
              and task.item_status["parent"] == "error",
              f"创建目录失败只影响该顶层项({task.item_status})")
        check(open(os.path.join(out3, "ok.txt")).read() == "OK", "无关项照常提交")
        check("创建失败" in task.item_errors["parent"],
              f"目录失败原因可解释({task.item_errors['parent']})")
        check(open(blocker).read() == "I-AM-A-FILE"
              and os.path.isfile(blocker)
              and not os.path.exists(os.path.join(out3, "parent/child.txt")),
              "阻挡目录创建的已有文件保持不变")

        # ---- 4. 符号链接原样保留(文件链接/目录链接/悬空链接) ----
        links = os.path.join(root, "links")
        os.makedirs(links)
        write(os.path.join(links, "real.txt"), "REAL")
        os.makedirs(os.path.join(links, "realdir"))
        write(os.path.join(links, "realdir/inner.txt"), "INNER")
        os.symlink("real.txt", os.path.join(links, "filelink"))
        os.symlink("realdir", os.path.join(links, "dirlink"))
        os.symlink("nowhere-at-all", os.path.join(links, "dangling"))
        out4 = os.path.join(root, "out4")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(),
                               [os.path.join(links, n) for n in
                                ("filelink", "dirlink", "dangling", "real.txt")],
                               LocalBackend(), out4)
        wait_for(lambda: not task.running, "链接复制")
        check(task.status == "done", f"链接复制整体成功({task.status}/{task.error})")
        check(os.path.islink(os.path.join(out4, "filelink"))
              and os.readlink(os.path.join(out4, "filelink")) == "real.txt",
              "文件链接按相对目标原样重建")
        check(os.path.islink(os.path.join(out4, "dirlink"))
              and os.readlink(os.path.join(out4, "dirlink")) == "realdir"
              and not os.path.exists(os.path.join(out4, "realdir")),
              "目录链接按链接重建, 不展开成真实目录内容")
        check(os.path.islink(os.path.join(out4, "dangling"))
              and not os.path.exists(os.path.join(out4, "dangling")),
              "悬空链接不再让整批任务失败")
        check(no_temp_left(out4) is None, "链接复制不残留临时文件")

        # 目录树内的链接
        out4b = os.path.join(root, "out4b")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(), [links], LocalBackend(), out4b)
        wait_for(lambda: not task.running, "目录内链接")
        check(os.path.islink(os.path.join(out4b, "links/dangling"))
              and os.path.islink(os.path.join(out4b, "links/filelink")),
              "递归目录内的链接也保持为链接")

        # ---- 5. 目标端不支持链接: 按内容复制 / 悬空链接只记单项失败 ----
        out5 = os.path.join(root, "out5")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(),
                               [os.path.join(links, n) for n in
                                ("filelink", "real.txt", "dangling")],
                               NoLinks(), out5)
        wait_for(lambda: not task.running, "目标端不支持链接")
        check(open(os.path.join(out5, "filelink")).read() == "REAL",
              "目标端无链接能力时按内容复制")
        check(task.status == "partial" and task.item_status["dangling"] == "error"
              and task.item_status["filelink"] == "committed",
              f"无法表示的悬空链接只让该项失败({task.item_status})")

        # 服务器明确拒绝 symlink 扩展 → 回退内容复制; 其他错误(同名) → 单项失败
        out6 = os.path.join(root, "out6")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(), [os.path.join(links, "filelink")],
                               NoSymlinkExt(), out6)
        wait_for(lambda: not task.running, "不支持 symlink 扩展时回退")
        check(task.status == "done" and open(os.path.join(out6, "filelink")).read() == "REAL",
              "服务器不支持 symlink 时按内容复制而不是报错")
        out7 = os.path.join(root, "out7")
        os.makedirs(out7)
        write(os.path.join(out7, "filelink"), "KEEP")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(), [os.path.join(links, "filelink")],
                               BusyLinks(), out7)
        wait_for(lambda: not task.running, "链接创建同名冲突")
        check(task.status == "error" and "目标已存在" in task.error,
              f"链接创建的真实失败不被当成不支持而吞掉({task.error})")
        check(open(os.path.join(out7, "filelink")).read() == "KEEP",
              "链接创建失败时不覆盖已有目标")

        # 源端无法读取链接(例如权限) → 该项失败, 其他项继续
        class NoReadlink(LocalBackend):
            def read_link(self, path):
                raise BackendError("不允许读取链接目标")

        out7b = os.path.join(root, "out7b")
        manager = TransferManager()
        task = manager.enqueue(NoReadlink(),
                               [os.path.join(links, "dirlink"),
                                os.path.join(links, "real.txt")],
                               NoLinks(), out7b)
        wait_for(lambda: not task.running, "源端无法读取链接")
        check(task.item_status["real.txt"] == "committed"
              and task.item_status["dirlink"] == "error",
              f"链接不可读只影响该项({task.item_status})")

        # ---- 6. 权限与修改时间保留 ----
        meta = os.path.join(root, "meta")
        script = write(os.path.join(meta, "run.sh"), "#!/bin/sh\n", mode=0o750,
                       mtime=1_600_000_000)
        private = write(os.path.join(meta, "secret.key"), "k", mode=0o600)
        privdir = os.path.join(meta, "secure")
        os.makedirs(privdir)
        write(os.path.join(privdir, "inside.txt"), "I")
        os.chmod(privdir, 0o700)
        out8 = os.path.join(root, "out8")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(),
                               [script, private, privdir], LocalBackend(), out8)
        wait_for(lambda: not task.running, "元数据保留")
        check(task.status == "done" and task.meta_errors == 0, "复制不报元数据错误")
        st = os.stat(os.path.join(out8, "run.sh"))
        check(stat_mod.S_IMODE(st.st_mode) == 0o750, "可执行位与权限被保留")
        check(int(st.st_mtime) == 1_600_000_000, "修改时间被保留")
        check(stat_mod.S_IMODE(os.stat(os.path.join(out8, "secret.key")).st_mode) == 0o600,
              "私有文件权限不被放宽")
        check(stat_mod.S_IMODE(os.stat(os.path.join(out8, "secure")).st_mode) == 0o700,
              "本任务新建目录保留源端权限")
        # 合并进已存在目录时不得改动用户已有目录的权限
        existing = os.path.join(root, "existing/secure")
        os.makedirs(existing)
        os.chmod(existing, 0o777)
        write(os.path.join(existing, "keep.txt"), "KEEP")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(), [privdir], LocalBackend(),
                               os.path.join(root, "existing"))
        wait_for(lambda: not task.running, "合并目录不改权限")
        check(stat_mod.S_IMODE(os.stat(existing).st_mode) == 0o777,
              "已存在目录的权限保持不变(只调本任务新建的目录)")
        check(os.path.isfile(os.path.join(existing, "inside.txt")), "目录内容仍按合并写入")

        class FailingMeta(LocalBackend):
            def set_metadata(self, path, mode=None, mtime=None):
                super().set_metadata(path, mode=mode, mtime=mtime)
                raise BackendError("只读文件系统")

        out9 = os.path.join(root, "out9")
        manager = TransferManager()
        task = manager.enqueue(LocalBackend(), [script], FailingMeta(), out9)
        wait_for(lambda: not task.running, "元数据失败")
        check(task.status == "done" and task.meta_errors == 1
              and "权限/时间戳未能保留" in task.note,
              f"元数据失败只提示不推翻内容({task.status}/{task.note})")
        check(open(os.path.join(out9, "run.sh")).read() == "#!/bin/sh\n", "内容仍然正确提交")

        # ---- 7. 等待确认不占用工作线程 ----
        pending = []
        manager = TransferManager()
        manager.ask_conflict_async = lambda task, names, done: pending.append((task, done))

        def resolve(task):
            """取回该任务交给主线程的答复回调."""
            return next(entry for entry in pending if entry[0] is task)[1]
        clash_dir = os.path.join(root, "clash")
        os.makedirs(clash_dir)
        parked = []
        for index in range(2):
            name = f"job{index}.txt"
            write(os.path.join(root, name), f"NEW{index}")
            write(os.path.join(clash_dir, name), f"OLD{index}")
            parked.append(manager.enqueue(LocalBackend(), [os.path.join(root, name)],
                                          LocalBackend(), clash_dir))
        free = write(os.path.join(root, "free.txt"), "FREE")
        plain = manager.enqueue(LocalBackend(), [free], LocalBackend(), clash_dir)
        wait_for(lambda: plain.status == "done", "冲突等待期间其他任务仍能推进")
        check(all(t.parked and t.status == "running" and t.phase == "waiting"
                  for t in parked),
              "两个任务停在等待确认, 且没有占用 worker")
        check(len(pending) == 2 and all(t.parked for t, _ in pending),
              "确认请求已交给主线程")
        check(os.path.join(clash_dir, "free.txt") and
              open(os.path.join(clash_dir, "free.txt")).read() == "FREE",
              "非冲突任务在并发额度已满时依然完成")
        # 取消其中一条 parked 任务: 立即结束, 不改目标内容
        manager.cancel(parked[0].id)
        check(parked[0].status == "cancelled" and not parked[0].parked,
              "取消等待确认的任务立即收尾")
        check(open(os.path.join(clash_dir, "job0.txt")).read() == "OLD0",
              "取消等待确认不改动已有目标")
        check(os.path.exists(os.path.join(root, "job0.txt")), "取消等待确认保留源文件")
        # 回答"跳过": 任务继续并按跳过收尾
        resolve(parked[1])("skip")
        wait_for(lambda: not parked[1].running, "跳过后继续任务")
        check(parked[1].status == "done"
              and parked[1].item_status["job1.txt"] == "skipped"
              and "跳过 1 项" in parked[1].note,
              f"回答跳过后续跑并在结果里说明跳过({parked[1].item_status}/{parked[1].note})")
        check(open(os.path.join(clash_dir, "job1.txt")).read() == "OLD1", "跳过项保留原目标")
        # 回答"覆盖": 原子替换并成功
        check(not manager.resolve_conflict(parked[0].id, "overwrite"),
              "已取消的任务不再接受答复")
        write(os.path.join(root, "job0.txt"), "NEW0-again")
        again = manager.enqueue(LocalBackend(), [os.path.join(root, "job0.txt")],
                                LocalBackend(), clash_dir, move=True)
        wait_for(lambda: again.parked, "覆盖确认可再次发起")
        resolve(again)("overwrite")
        wait_for(lambda: not again.running, "覆盖后完成任务")
        check(again.status == "done"
              and open(os.path.join(clash_dir, "job0.txt")).read() == "NEW0-again"
              and not os.path.exists(os.path.join(root, "job0.txt")),
              "选择覆盖后完成替换, 移动语义删除源")
        check(no_temp_left(clash_dir) is None, "冲突流程不残留临时文件")

        # 断连可以立刻结束等待确认
        late = TransferManager()
        holder = []
        late.ask_conflict_async = lambda task, names, done: holder.append(done)
        write(os.path.join(root, "late.txt"), "LATE")
        out10 = os.path.join(root, "out10")
        write(os.path.join(out10, "late.txt"), "OLD-LATE")     # 制造同名冲突
        parked_late = late.enqueue(LocalBackend(), [os.path.join(root, "late.txt")],
                                   LocalBackend(), out10)
        wait_for(lambda: parked_late.parked, "第二个管理器进入等待确认")
        parked_late.connection_error = "连接已断开: 测试"
        check(late.check_parked() and not parked_late.running
              and parked_late.status == "error" and "连接已断开" in parked_late.error,
              "连接断开时等待确认的任务立即报错收尾")
        holder[0]("overwrite")
        drain()
        check(open(os.path.join(out10, "late.txt")).read() == "OLD-LATE",
              "已结束的任务不会被迟到的答复复活(目标内容保持不变)")
        late._pool.shutdown(wait=True)
        drain()
        manager._pool.shutdown(wait=True)
        drain()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\ntransfer_resilience 全部通过 ✅")


if __name__ == "__main__":
    main()
