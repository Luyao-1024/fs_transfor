"""传输调度: 执行计划 + 逐项结果 + 临时文件安全提交 + 滚动速度计算.

方向无关: 本地↔本地 / 本地↔远端 / 远端↔远端 走同一套流式复制;
先扫描出执行计划(目录树 / 普通文件 / 符号链接), 再逐项写入目标目录的
任务专属临时文件并原子提交:
- 单个项目失败只记入该项的逐项结果, 同任务其余项目继续;
- 权限位与修改时间尽力还原, 符号链接原样重建(目标端不支持才按内容复制);
- 只有完整提交成功的顶层条目才删除源(跳过/失败/取消一律保留源);
- 同名冲突确认交给主线程后释放工作线程, 等待确认不占用并发额度.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from gi.repository import GLib

from .backend.base import BackendError, CancelledError, TransferItem

CHUNK = 512 * 1024      # 复制块大小
SPEED_WINDOW = 2.0      # 速度滚动窗口(秒)
MAX_WORKERS = 2         # 并行传输数

PARKED = object()       # 冲突确认已交给主线程, 工作线程可以释放

# 目标端拒绝创建链接(而非同名冲突)时的错误特征: 允许回退成内容复制
_UNSUPPORTED = re.compile(r"not supported|unsupported|not implemented"
                          r"|operation not supported|不支持", re.IGNORECASE)


@dataclass
class _Job:
    """运行期执行计划(仅内存, 不写历史)."""
    dirs: list[str] = field(default_factory=list)
    dir_modes: dict[str, int] = field(default_factory=dict)
    items: list[TransferItem] = field(default_factory=list)
    dead: set[str] = field(default_factory=set)        # 扫描即失败的顶层条目
    skipped: set[str] = field(default_factory=set)     # 用户选择跳过的条目

    def add_item(self, item: TransferItem):
        self.items.append(item)


DIRECTIONS = {
    "copy": ("复制", "edit-copy-symbolic"),
    "upload": ("上传", "go-up-symbolic"),
    "download": ("下载", "go-down-symbolic"),
    "remote": ("远端复制", "network-transmit-receive-symbolic"),
}


class Transfer:
    _next_id = 1
    _id_lock = threading.Lock()

    def __init__(self, src, dst, src_paths, dst_dir, direction, move=False):
        with Transfer._id_lock:
            self.id = Transfer._next_id
            Transfer._next_id += 1
        self.src = src
        self.uid = uuid.uuid4().hex
        self.dst = dst
        self.src_paths = src_paths
        self.dst_dir = dst_dir
        self.direction = direction
        self.move = move
        self.status = "pending"     # pending / running / done / partial / cancelled / error
        self.phase = "pending"
        self.total_known = False
        self.file_count = 0
        self.done_files = 0
        self.created_at = time.time()
        self.ended_at = None
        self.future = None
        self.finalized = False
        self.notification_hidden = False
        self.item_errors = {}
        self.total_bytes = 0
        self.done_bytes = 0
        self.error = ""
        self.note = ""              # 部分完成/跳过等补充说明
        self.speed = 0.0
        self.finished_at: float | None = None
        self.cancel_event = threading.Event()
        self.connection_error = ""
        # 执行计划与冲突等待(运行期状态, 不进历史)
        self.plan: _Job | None = None
        self.parked = False         # 等待用户确认: 不占用工作线程
        self.conflict_names: list[str] = []
        self.meta_errors = 0        # 权限/时间戳保留失败计数(只提示)
        self._apply_lock = threading.Lock()
        self.applying = False
        # 逐顶层条目结果与源路径映射(移动删源依据): 主线程只读快照
        self.item_status: dict[str, str] = {}   # 顶层名 → pending/committed/skipped
        self.top_srcs: dict[str, str] = {}      # 顶层名 → 源路径
        # UI 用一次性标记
        self.toasted = False
        self.refreshed = False
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    @property
    def frac(self):
        """进度 0..1; None 表示不确定进度."""
        if self.total_known and self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        return None

    @property
    def running(self):
        return self.status in ("pending", "running")

    def _sample(self, done_bytes):
        with self._lock:
            self.done_bytes = done_bytes
            now = time.monotonic()
            self._samples.append((now, done_bytes))
            cut = now - SPEED_WINDOW
            while len(self._samples) > 2 and self._samples[0][0] < cut:
                self._samples.pop(0)
            if len(self._samples) >= 2:
                t0, b0 = self._samples[0]
                t1, b1 = self._samples[-1]
                if t1 > t0:
                    self.speed = (b1 - b0) / (t1 - t0)

    def add_progress(self, n):
        self._sample(self.done_bytes + n)

    def set_done(self, n):
        self._sample(n)

    def reset_progress(self):
        with self._lock:
            self._samples = []
            self.done_bytes = 0
            self.speed = 0.0


class TransferManager:
    def __init__(self, connection_hub=None):
        self.connection_hub = connection_hub
        self.transfers: list[Transfer] = []
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="transfer")
        self._listeners: list = []          # fn(): 在主线程被调用
        self.ask_overwrite = None           # 同步钩子: (names) -> 'overwrite'|'skip'|None
        self.ask_conflict = None            # 同步钩子: (transfer, names), 可取消等待
        self.ask_conflict_async = None      # UI 注册: (transfer, names, done) 非阻塞
        self.accepting = True
        self.history = None

    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    @property
    def busy(self):
        return any(t.running or (t.future is not None
                                and (not t.future.done() or not t.finalized))
                   for t in self.transfers)

    def stop_accepting(self, cancel=False):
        self.accepting = False
        if cancel:
            for t in list(self.transfers):
                self.cancel(t.id)

    def shutdown(self):
        if self.busy:
            raise RuntimeError("仍有任务正在清理")
        self.accepting = False
        self._pool.shutdown(wait=False)
        self._listeners.clear()

    def _finish(self, t):
        """工作线程完成后统一在 GTK 路径释放租约和保存历史。"""
        if t.finalized:
            return GLib.SOURCE_REMOVE
        t.finalized = True
        if self.connection_hub is not None:
            for backend in (t.src, t.dst):
                if not backend.is_local and getattr(backend, "dead", False):
                    self.connection_hub.mark_dead(backend, t.error or "传输连接已断开")
            self.connection_hub.release_task(t)
        if self.history is not None and t in self.transfers:
            self.history.record(t)
            finished = [item for item in self.transfers if not item.running]
            if len(finished) > 200:
                remove = set(finished[:-200])
                self.transfers = [item for item in self.transfers if item not in remove]
        self._notify()
        return GLib.SOURCE_REMOVE

    def _notify(self):
        def emit():
            for fn in list(self._listeners):
                try:
                    fn()
                except Exception:
                    pass
            return False
        GLib.idle_add(emit)

    # ------------------------------------------------------------------
    def enqueue(self, src, src_paths, dst, dst_dir, move=False) -> Transfer | None:
        if not src_paths:
            return None
        if src.is_local and dst.is_local:
            direction = "copy"
        elif src.is_local:
            direction = "upload"
        elif dst.is_local:
            direction = "download"
        elif getattr(src, "same_as", lambda o: False)(dst):
            direction = "copy"      # 同服务器: 流式复制
        else:
            direction = "remote"
        t = Transfer(src, dst, list(src_paths), dst_dir, direction, move)
        self.transfers.append(t)
        try:
            if not self.accepting:
                raise BackendError("正在退出，无法添加新传输")
            if self.connection_hub is not None:
                self.connection_hub.acquire_task(t)
            self._check_plan(t, list(src_paths))
            t.future = self._pool.submit(self._run, t)
        except Exception as e:
            if self.connection_hub is not None:
                self.connection_hub.release_task(t)
            t.status = "error"
            t.error = str(e)
            t.finished_at = time.monotonic()
            t.ended_at = time.time()
            self._finish(t)
            self._notify()
            return t
        self._notify()
        return t

    def cancel(self, tid) -> bool:
        for t in self.transfers:
            if t.id == tid:
                if not t.running:
                    return False
                t.cancel_event.set()
                if t.parked:
                    # 停在确认框上的任务没有占用 worker, 直接按取消收尾
                    return self.resolve_conflict(t.id, None)
                if t.future is not None and t.future.cancel():
                    t.status = "error" if t.connection_error else "cancelled"
                    t.error = t.connection_error
                    t.finished_at = time.monotonic()
                    t.ended_at = time.time()
                    self._finish(t)
                self._notify()
                return True
        return False

    def check_parked(self) -> bool:
        """主线程定时检查: 取消/断连可以立刻结束等待确认的任务."""
        changed = False
        for t in list(self.transfers):
            if t.parked and (t.cancel_event.is_set() or t.connection_error):
                self.resolve_conflict(t.id, None)
                changed = True
        return changed

    def clear_finished(self):
        self.transfers = [t for t in self.transfers
                          if t.running or (t.future is not None and not t.finalized)]
        if self.history is not None:
            self.history.clear()
        self._notify()

    def remove_finished(self, tid: int) -> bool:
        """移除单条已结束记录；运行中的任务只能取消，不能直接移除."""
        for t in self.transfers:
            if t.id == tid:
                if t.running or (t.future is not None and not t.finalized):
                    return False
                self.transfers.remove(t)
                if self.history is not None:
                    self.history.remove(t.uid)
                self._notify()
                return True
        return False

    def clear_expired_done(self, max_age: float, now: float | None = None) -> bool:
        """移除完成时间超过 max_age 的成功任务，保留失败和取消记录."""
        now = time.monotonic() if now is None else now
        kept = [
            t for t in self.transfers
            if not (t.status == "done" and t.finished_at is not None
                    and now - t.finished_at >= max_age)
        ]
        if len(kept) == len(self.transfers):
            return False
        self.transfers = kept
        self._notify()
        return True

    # ------------------------------------------------------------------
    # 安全检查(SAFE-02): 入队前拒绝危险计划
    # ------------------------------------------------------------------
    def _check_plan(self, t: Transfer, paths: list[str]):
        """去重并检查: 源间父子嵌套、重名、源=目标、目录传入自身.

        通过后写入 t.src_paths(去重结果); 违反规则抛 BackendError.
        """
        src, dst = t.src, t.dst
        dedup: list[str] = []
        normed: list[str] = []
        for p in paths:
            np_ = src.normpath(p)
            dup = False
            for o in normed:
                if np_ == o:
                    dup = True
                    break
                if np_.startswith(o + "/") or o.startswith(np_ + "/"):
                    raise BackendError(
                        f"所选项目互相包含: {src.basename(o)} 与 {src.basename(np_)}")
            if not dup:
                normed.append(np_)
                dedup.append(p)
        t.src_paths = dedup
        if not dedup:
            return
        names = [src.basename(p) for p in dedup]
        if len(set(names)) != len(names):
            raise BackendError("所选项目中有重名, 会写入同一目标位置")

        same_fs = ((src.is_local and dst.is_local)
                   or (not src.is_local and not dst.is_local
                       and getattr(src, "same_as", lambda o: False)(dst)))
        if not same_fs:
            return
        for p in dedup:
            base = src.basename(p)
            np_ = src.normpath(p)
            target = dst.normpath(dst.join(t.dst_dir, base))
            if target == np_:
                raise BackendError(f"源与目标是同一位置: {base}")
            try:
                st = src.stat(p)
            except BackendError:
                st = None
            if st is not None and st.is_dir:
                dd = dst.normpath(t.dst_dir)
                if dd == np_ or dd.startswith(np_ + "/"):
                    raise BackendError(f"不能把目录传输到它自身内部: {base}")
        if src.is_local and dst.is_local:
            self._check_local_identity(dedup, t.dst_dir, src, dst)

    @staticmethod
    def _check_local_identity(src_paths, dst_dir, src, dst):
        """本地加严: realpath 与 (st_dev, st_ino) 识别链接/硬链接别名."""
        for p in src_paths:
            base = src.basename(p)
            target = dst.join(dst_dir, base)
            try:
                rp_src = os.path.realpath(src.normpath(p))
                rp_dst = os.path.realpath(dst.normpath(target))
                if rp_src == rp_dst:
                    raise BackendError(f"源与目标是同一文件: {base}")
                st_s = os.stat(rp_src)
                st_d = os.stat(rp_dst)
                if (st_s.st_dev, st_s.st_ino) == (st_d.st_dev, st_d.st_ino):
                    raise BackendError(f"源与目标是同一文件(链接指向它): {base}")
            except BackendError:
                raise
            except OSError:
                continue  # 源或目标尚不存在: 由后续流程正常处理

    # ------------------------------------------------------------------
    def _run(self, t: Transfer):
        """工作线程入口: 扫描 → 冲突确认 → 执行 → 统一收尾.

        冲突确认交给主线程时立即返回, worker 回到池里继续跑其他任务;
        用户答复后由 resolve_conflict 重新入队续跑, 因此停在确认框上的
        任务不会把并发额度占满(排队饿死), 也不再受对话框超时影响。
        """
        if t.plan is None:
            t.status = "running"
            t.phase = "scanning"
            self._notify()
            try:
                t.plan = self._collect(t)
            except CancelledError:
                return self._settle(t, "cancelled")
            except Exception as e:
                t.error = t.error or _reason(e)
                return self._settle(t, "error")
            if self._request_conflicts(t) is PARKED:
                return
        with t._apply_lock:
            if t.applying:
                return
            t.applying = True
        try:
            self._apply_plan(t)
            status = "partial" if t.item_errors else "done"
            if t.move:
                t.phase = "deleting_source"
                self._notify()
                if not self._delete_moved_sources(t):
                    status = "partial"
                elif any(s == "skipped" for s in dict(t.item_status).values()):
                    status = "partial"      # 有跳过: 部分源保留
            self._settle(t, status)
        except CancelledError:
            self._settle(t, "cancelled")
        except Exception as e:
            t.error = t.error or _reason(e)
            self._settle(t, "error")

    def _settle(self, t: Transfer, status: str):
        """统一收尾: 修正逐项状态、汇总 note/error, 安排主线程释放资源."""
        if t.ended_at is not None:
            return
        if t.connection_error:
            status, t.error = "error", t.connection_error
        fallback = t.error or ("已取消" if status == "cancelled" else "未完成")
        for name, state in list(t.item_status.items()):
            if state == "pending":
                t.item_status[name] = "cancelled" if status == "cancelled" else "error"
                t.item_errors.setdefault(name, fallback)
        if status == "error" and "committed" in t.item_status.values():
            status = "partial"
        skipped = sum(1 for state in t.item_status.values() if state == "skipped")
        if skipped and not t.move and "跳过" not in t.note:
            # 复制时"跳过"是用户的选择, 不算失败, 但要能解释为什么没写目标
            t.note = (t.note + "，" if t.note else "") + f"跳过 {skipped} 项(目标保持原样)"
        if t.meta_errors:
            t.note = (t.note + "，" if t.note else "") + \
                f"{t.meta_errors} 项权限/时间戳未能保留"
        t.status = status
        t.phase = "finished"
        t.parked = False
        t.speed = 0.0
        t.finished_at = time.monotonic()
        t.ended_at = time.time()
        GLib.idle_add(self._finish, t)
        return None

    def _delete_moved_sources(self, t: Transfer) -> bool:
        """移动语义: 只删除完整提交成功的顶层源.

        跳过、失败、未完成的顶层条目一律保留源; 目录内任一子项失败时该
        顶层条目也不删源。返回 True = 应删除的源全部删除成功.
        """
        ok = True
        moved = skipped = retained = del_failed = 0
        for name, state in dict(t.item_status).items():
            if state == "skipped":
                skipped += 1
                continue
            if state != "committed":
                retained += 1
                continue
            p = t.top_srcs.get(name)
            if p is None:
                continue
            if t.cancel_event.is_set():
                raise CancelledError("已取消，已复制但尚未删除的源保留")
            try:
                t.src.delete(p)
                moved += 1
                t.item_status[name] = "moved"
            except Exception as e:
                ok = False
                del_failed += 1
                msg = f"{name}: 复制成功，源删除失败 ({e})"
                t.item_status[name] = "source_delete_failed"
                t.item_errors[name] = msg
                _append_error(t, msg)
        parts = [f"已移动 {moved} 项"]
        if skipped:
            parts.append(f"跳过 {skipped} 项")
        if retained:
            parts.append(f"{retained} 项源保留(失败或未完成)")
        if del_failed:
            parts.append(f"{del_failed} 项源删除失败")
        t.note = "，".join(parts)
        return ok

    # ------------------------------------------------------------------
    # 扫描与执行计划
    # ------------------------------------------------------------------
    def _collect(self, t: Transfer) -> _Job:
        """展开源为执行计划; 单项读取失败只记录, 不牵连同批其他项."""
        src = t.src
        job = _Job()
        for p in t.src_paths:
            name = src.basename(p)
            t.top_srcs[name] = p
            t.item_status[name] = "pending"
        for p in t.src_paths:
            self._check_cancel(t)
            name = src.basename(p)
            try:
                st = src.lstat(p)
            except BackendError as e:
                self._fail_top(t, job, name, f"源无法访问: {e}")
                continue
            if st is None:
                self._fail_top(t, job, name, f"源项目已不存在: {name}")
                continue
            if st.is_link and not st.is_dir:
                job.add_item(TransferItem(p, name, 0, st.mode, st.mtime,
                                          is_link=True))
            elif st.is_dir:
                self._absorb(t, job, name, st.mode,
                             src.walk_plan(p, t.cancel_event))
            else:
                job.add_item(TransferItem(p, name, st.size, st.mode, st.mtime))
        if job.dead and not (set(t.top_srcs) - job.dead):
            raise BackendError(t.error or "没有可传输的项目")
        return job

    @staticmethod
    def _absorb(t: Transfer, job: _Job, top: str, top_mode: int, walked):
        """把 walk_plan 结果并入任务计划(相对路径加顶层目录前缀)."""
        job.dirs.append(top)
        job.dir_modes[top] = top_mode
        for d in walked.dirs:
            job.dirs.append(f"{top}/{d}")
        for rel, mode in walked.dir_modes.items():
            job.dir_modes[f"{top}/{rel}"] = mode
        for item in walked.files:
            job.add_item(TransferItem(item.src_path, f"{top}/{item.rel_path}",
                                      item.size, item.mode, item.mtime))
        for item in walked.links:
            job.add_item(TransferItem(item.src_path, f"{top}/{item.rel_path}",
                                      0, item.mode, item.mtime, is_link=True))

    def _fail_top(self, t: Transfer, job: _Job, name: str, message: str):
        """顶层条目整体不可用: 记入逐项结果并排除其全部内容."""
        job.dead.add(name)
        t.item_status[name] = "error"
        t.item_errors[name] = message
        _append_error(t, f"{name}: {message}")

    # ------------------------------------------------------------------
    # 同名冲突
    # ------------------------------------------------------------------
    def _request_conflicts(self, t: Transfer):
        """确认顶层同名冲突; 返回 PARKED 表示已交给主线程处理."""
        dst = t.dst
        conflicts = []
        for name in t.top_srcs:
            if name in t.plan.dead or name in t.plan.skipped:
                continue
            try:
                if dst.exists(dst.join(t.dst_dir, name)):
                    conflicts.append(name)
            except Exception:
                continue        # 探测失败交给写入阶段报出真实原因
        if not conflicts:
            return None
        t.phase = "waiting"
        t.conflict_names = conflicts
        self._notify()
        if self.ask_conflict_async is not None:
            t.parked = True
            self.ask_conflict_async(t, conflicts,
                                    lambda answer: self.resolve_conflict(t.id, answer))
            return PARKED
        if self.ask_conflict is None and self.ask_overwrite is None:
            return None
        # 同步钩子(headless 与测试): 沿用阻塞式等待语义
        answer = (self.ask_conflict(t, conflicts) if self.ask_conflict is not None
                  else self.ask_overwrite(conflicts))
        if answer is None:
            raise CancelledError("已取消")
        if answer == "skip":
            self._skip_names(t, conflicts)
        return None

    def resolve_conflict(self, tid: int, answer) -> bool:
        """主线程回调: 应用冲突决定, 然后让任务继续或按取消收尾."""
        t = next((x for x in self.transfers if x.id == tid), None)
        if t is None or not t.parked:
            return False
        t.parked = False
        if answer is None or t.cancel_event.is_set():
            t.error = t.connection_error or "已取消"
            self._settle(t, "error" if t.connection_error else "cancelled")
            return True
        if answer == "skip":
            self._skip_names(t, t.conflict_names)
        t.conflict_names = []
        t.future = self._pool.submit(self._run, t)
        self._notify()
        return True

    @staticmethod
    def _skip_names(t: Transfer, names):
        """跳过: 从计划中移除这些顶层条目的全部内容."""
        kill = set(names)
        t.plan.skipped |= kill
        t.plan.items = [i for i in t.plan.items if _top_of(i.rel_path) not in kill]
        t.plan.dirs = [d for d in t.plan.dirs if _top_of(d) not in kill]
        for d in list(t.plan.dir_modes):
            if _top_of(d) in kill:
                t.plan.dir_modes.pop(d, None)
        for name in kill:
            if t.item_status.get(name) == "pending":
                t.item_status[name] = "skipped"

    # ------------------------------------------------------------------
    def _apply_plan(self, t: Transfer):
        """执行计划: 建目录 → 逐项提交 → 汇总逐项结果(单项失败不牵连其他)."""
        dst = t.dst
        job = t.plan
        t.total_bytes = sum(i.size for i in job.items if not i.is_link)
        t.file_count = len(job.items)
        t.total_known = True
        t.phase = "transferring"
        self._notify()
        self._check_cancel(t)

        # 目标目录本身不可用属于计划级错误(整任务失败)
        self._mkdir_checked(dst, t.dst_dir, "目标目录")

        broken = set(job.dead)
        remaining: dict[str, int] = {}
        for item in job.items:
            top = _top_of(item.rel_path)
            if top not in broken:
                remaining[top] = remaining.get(top, 0) + 1
        for name in list(t.item_status):
            if t.item_status[name] == "pending" and name not in remaining:
                t.item_status[name] = "committed"    # 空目录等无内容条目

        failed: dict[str, list[str]] = {}
        created: set[str] = set()
        for d in job.dirs:
            self._check_cancel(t)
            top = _top_of(d)
            if top in broken:
                continue
            path = dst.join(t.dst_dir, d)
            try:
                fresh = not dst.exists(path)
            except Exception:
                fresh = False
            try:
                self._mkdir_checked(dst, path, f"创建目录 {d}")
            except BackendError as e:
                broken.add(top)
                failed.setdefault(top, []).append(str(e))
                remaining[top] = 0
                continue
            if fresh:
                created.add(d)
        for item in job.items:
            self._check_cancel(t)
            top = _top_of(item.rel_path)
            if top in broken:
                continue
            target = dst.join(t.dst_dir, item.rel_path)
            try:
                if item.is_link:
                    self._copy_link(t, item, target)
                else:
                    self._copy_file(t, item.src_path, target)
                    self._preserve(t, item, target)
                t.done_files += 1
            except CancelledError:
                raise
            except Exception as e:
                failed.setdefault(top, []).append(f"{item.rel_path}: {e}")
            remaining[top] -= 1

        for name, count in remaining.items():
            if name in broken or count > 0 or name in job.skipped:
                continue
            t.item_status[name] = "error" if failed.get(name) else "committed"
        for name, msgs in failed.items():
            summary = f"{len(msgs)} 个项目失败: " + "; ".join(msgs[:3])
            if len(msgs) > 3:
                summary += f"（共 {len(msgs)} 项）"
            t.item_status[name] = "error"
            t.item_errors[name] = summary
            _append_error(t, f"{name}: {summary}")
        for d in created:                       # 只调本任务新建目录的权限
            mode = job.dir_modes.get(d)
            if mode:
                try:
                    dst.set_metadata(dst.join(t.dst_dir, d), mode=mode)
                except BackendError:
                    t.meta_errors += 1
        # 只有"一个字节都没提交成功"才算整体失败; 有任何成品时报告部分完成
        if failed and not t.done_files \
                and not any(s == "committed" for s in t.item_status.values()):
            raise BackendError(t.error or "全部项目传输失败")

    @staticmethod
    def _check_cancel(t: Transfer):
        if t.cancel_event.is_set():
            raise CancelledError("已取消")

    @staticmethod
    def _preserve(t: Transfer, item, dst_path: str):
        """权限与修改时间尽力还原; 失败只计数, 不推翻已提交的内容."""
        if not item.mode and item.mtime is None:
            return
        try:
            t.dst.set_metadata(dst_path, mode=item.mode or None,
                               mtime=item.mtime)
        except BackendError:
            t.meta_errors += 1

    def _copy_link(self, t: Transfer, item, dst_path: str):
        """符号链接: 优先原样重建; 目标端不支持时退回按内容复制."""
        src, dst = t.src, t.dst
        target = None
        if src.supports_links:
            try:
                target = src.read_link(item.src_path)
            except BackendError as e:
                if not dst.supports_links:
                    raise BackendError(f"链接无法读取: {e}") from None
        if target is not None and dst.supports_links:
            try:
                dst.make_symlink(target, dst_path)
                return
            except BackendError as e:
                if not _UNSUPPORTED.search(str(e)):
                    raise
        self._copy_file(t, item.src_path, dst_path)

    # ------------------------------------------------------------------
    @staticmethod
    def _mkdir_checked(backend, path, what):
        try:
            backend.mkdir(path)
        except BackendError:
            # 唯一可容忍的失败是"目录已存在"; 权限/同名文件阻挡/断连必须上抛
            try:
                st = backend.stat(path)
            except BackendError:
                st = None
            if st is None or not st.is_dir:
                raise BackendError(f"{what}创建失败: {path}") from None

    # ------------------------------------------------------------------
    def _copy_file(self, t: Transfer, src_path: str, dst_path: str):
        """复制单文件: 写入目标目录的临时文件, 成功后原子提交."""
        temp = t.dst.temp_path(dst_path)
        r = w = None
        committed = False
        opened = False          # 只有真正创建过临时文件才需要清理
        try:
            r = t.src.open_read(src_path)
            w = t.dst.open_write(temp)
            opened = True
            while not t.cancel_event.is_set():
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                w.write(chunk)
                t.add_progress(len(chunk))
            if t.cancel_event.is_set():
                raise CancelledError("已取消")
            # 关闭错误必须传播(SAFE-05): SFTP 管道写的错误延迟到 close 浮出
            w.close()
            w = None
            if t.cancel_event.is_set():
                raise CancelledError("已取消")
            t.phase = "committing"
            self._notify()
            t.dst.commit_temp(temp, dst_path)
            committed = True
            t.phase = "transferring"
        except CancelledError:
            raise
        except Exception as e:
            # 只给出原因: 调用方负责拼上相对路径, 避免重复前缀
            raise BackendError(str(e) or e.__class__.__name__) from None
        finally:
            for f in (w, r):
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            if opened and not committed:
                note = t.dst.discard_temp(temp)
                if note:
                    _append_error(t, f"临时文件清理失败: {note}")


def _top_of(rel: str) -> str:
    return rel.split("/", 1)[0]


def _append_error(t: Transfer, message: str):
    t.error = f"{t.error}; {message}" if t.error else message


def _reason(e) -> str:
    return str(e) or e.__class__.__name__
