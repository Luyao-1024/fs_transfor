"""传输调度: 统一块复制 + 递归目录 + 滚动速度计算 + 临时文件安全提交.

方向无关: 本地↔本地 / 本地↔远端 / 远端↔远端 走同一套流式复制;
文件先写入目标目录的任务专属临时文件, 成功后原子提交 —— 取消/失败
只清理临时文件, 已有目标文件全程不变; 移动语义在核心层: 只有完整
提交的项目才删除源(跳过/失败/取消一律保留源).
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib

from .backend.base import BackendError, CancelledError

CHUNK = 512 * 1024      # 复制块大小
SPEED_WINDOW = 2.0      # 速度滚动窗口(秒)
MAX_WORKERS = 2         # 并行传输数

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
        self.ask_overwrite = None           # UI 注册: (names) -> 'overwrite'|'skip'|None
        self.ask_conflict = None            # (transfer, names), 可取消的 UI 等待
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
                if t.future is not None and t.future.cancel():
                    t.status = "error" if t.connection_error else "cancelled"
                    t.error = t.connection_error
                    t.finished_at = time.monotonic()
                    t.ended_at = time.time()
                    self._finish(t)
                self._notify()
                return True
        return False

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
        t.status = "running"
        t.phase = "scanning"
        self._notify()
        try:
            self._execute(t)
            status = "done"
            if t.move:
                t.phase = "deleting_source"
                self._notify()
                if not self._delete_moved_sources(t):
                    status = "partial"
                elif any(s == "skipped" for s in dict(t.item_status).values()):
                    status = "partial"   # 有跳过: 部分源保留
            t.status = status
        except CancelledError:
            t.status = "cancelled"
        except Exception as e:
            t.status = "error"
            t.error = str(e) or e.__class__.__name__
        finally:
            if t.connection_error:
                t.status = "error"
                t.error = t.connection_error
            for name, status in list(t.item_status.items()):
                if status == "pending":
                    t.item_status[name] = "cancelled" if t.status == "cancelled" else "error"
                    t.item_errors[name] = t.error or "未完成"
            if t.status == "error" and "committed" in t.item_status.values():
                t.status = "partial"
            t.speed = 0.0
            t.finished_at = time.monotonic()
            t.ended_at = time.time()
            GLib.idle_add(self._finish, t)

    def _delete_moved_sources(self, t: Transfer) -> bool:
        """移动语义: 仅删除已完整提交的顶层源; 目录内任一子项跳过/失败
        时整个源条目已在冲突/异常路径保留. 返回 True=全部删除成功."""
        ok = True
        moved = skipped = del_failed = 0
        for name, st in dict(t.item_status).items():
            if st == "skipped":
                skipped += 1
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
                t.error = f"{t.error}; {msg}" if t.error else msg
        parts = [f"已移动 {moved} 项"]
        if skipped:
            parts.append(f"跳过 {skipped} 项")
        if del_failed:
            parts.append(f"{del_failed} 项源删除失败")
        t.note = "，".join(parts)
        return ok

    def _execute(self, t: Transfer):
        src, dst = t.src, t.dst

        dirs, files, total, top = self._collect(t)
        self._check_conflicts(t, files, dirs, top)

        t.total_bytes = sum(size for _, _, size in files)
        t.file_count = len(files)
        t.total_known = True
        t.phase = "transferring"
        self._notify()
        if t.cancel_event.is_set():
            raise CancelledError("已取消")

        # 确保目标目录本身存在(面板 cwd 正常时已存在, 这里兜底)
        self._mkdir_checked(dst, t.dst_dir, "目标目录")
        for d in dirs:
            if t.cancel_event.is_set():
                raise CancelledError("已取消")
            self._mkdir_checked(dst, dst.join(t.dst_dir, d), f"创建目录 {d}")

        remaining = {}
        for _, rel, _ in files:
            name = rel.split("/", 1)[0]
            remaining[name] = remaining.get(name, 0) + 1
        for name, status in list(t.item_status.items()):
            if status == "pending" and name not in remaining:
                t.item_status[name] = "committed"
        for src_path, rel, _size in files:
            if t.cancel_event.is_set():
                raise CancelledError("已取消")
            self._copy_file(t, src_path, dst.join(t.dst_dir, rel))
            t.done_files += 1
            name = rel.split("/", 1)[0]
            remaining[name] -= 1
            if remaining[name] == 0:
                t.item_status[name] = "committed"

        for name, st in dict(t.item_status).items():
            if st == "pending":
                t.item_status[name] = "committed"

    # ------------------------------------------------------------------
    def _collect(self, t: Transfer):
        """展开源: (相对目录, [(绝对路径, 相对路径, 大小)], 总字节, 顶层名)."""
        src = t.src
        dirs: list[str] = []
        files: list[tuple[str, str, int]] = []
        total = 0
        top: list[str] = []
        for p in t.src_paths:
            name = src.basename(p)
            t.top_srcs[name] = p
            t.item_status[name] = "pending"
        for p in t.src_paths:
            if t.cancel_event.is_set():
                raise CancelledError("已取消")
            st = src.stat(p)
            if st is None:
                raise BackendError(f"源项目已不存在: {p}")
            base = src.basename(p)
            top.append(base)
            t.top_srcs[base] = p
            t.item_status[base] = "pending"
            if st.is_dir:
                dirs.append(base)   # 顶层目录自身也要在目标端创建(空目录不再假成功)
                ds, fs, tt = src.walk(p, cancel_event=t.cancel_event)
                dirs.extend(f"{base}/{d}" for d in ds)
                files.extend((s, f"{base}/{r}", z) for s, r, z in fs)
                total += tt
            else:
                files.append((p, base, st.size))
                total += st.size
        return dirs, files, total, top

    def _check_conflicts(self, t, files, dirs, top):
        if self.ask_overwrite is None and self.ask_conflict is None:
            return
        dst = t.dst
        conflicts = []
        for name in top:
            try:
                if dst.exists(dst.join(t.dst_dir, name)):
                    conflicts.append(name)
            except Exception:
                pass
        if not conflicts:
            return
        t.phase = "waiting"
        self._notify()
        answer = (self.ask_conflict(t, conflicts) if self.ask_conflict is not None
                  else self.ask_overwrite(conflicts))
        if answer is None:
            raise CancelledError("已取消")
        if answer == "skip":
            skip = set(conflicts)
            files[:] = [(s, r, z) for s, r, z in files if r.split("/", 1)[0] not in skip]
            dirs[:] = [d for d in dirs if d.split("/", 1)[0] not in skip]
            for name in skip:
                if t.item_status.get(name) == "pending":
                    t.item_status[name] = "skipped"

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
        try:
            r = t.src.open_read(src_path)
            w = t.dst.open_write(temp)
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
            raise BackendError(f"{src_path}: {e}") from None
        finally:
            for f in (w, r):
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            if not committed:
                note = t.dst.discard_temp(temp)
                if note:
                    msg = f"临时文件清理失败: {note}"
                    t.error = f"{t.error}; {msg}" if t.error else msg
