"""后端抽象基类: 本地与 SFTP 实现同一接口, 传输层无需关心方向."""
from __future__ import annotations

import os
import stat as stat_mod
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass


class BackendError(Exception):
    """后端操作失败(网络/权限/不存在等), message 可直接展示给用户."""


class CancelledError(Exception):
    """传输被用户取消."""


@dataclass
class FileEntry:
    name: str
    path: str
    size: int
    mtime: float
    is_dir: bool
    perms: str = "----------"
    is_link: bool = False
    mode: int = 0          # 原始 st_mode(0 表示未知): 供权限保留使用


@dataclass
class TransferItem:
    """执行计划中的一项: 普通文件或符号链接(rel 为相对目标根的路径)."""
    src_path: str
    rel_path: str
    size: int = 0
    mode: int = 0                     # lstat 权限位(0 表示未知/不保留)
    mtime: float | None = None
    is_link: bool = False
    link_target: str | None = None    # None -> 执行时再读取


@dataclass
class WalkPlan:
    """传输执行计划: 目录树 + 普通文件 + 符号链接 + 未解引用的字节总量."""
    dirs: list[str]
    dir_modes: dict[str, int]
    files: list[TransferItem]
    links: list[TransferItem]
    total: int


def perms_str(mode) -> str:
    try:
        return stat_mod.filemode(int(mode))
    except (ValueError, TypeError):
        return "----------"


class BaseBackend(ABC):
    is_local: bool = False
    dead: bool = False

    # ---- 基本信息 ----
    supports_links = False   # 能否读出/创建符号链接(否则按内容复制)

    @property
    @abstractmethod
    def label(self) -> str:
        """显示名, 如 '本地' / 'root@1.2.3.4'."""

    @abstractmethod
    def disconnect(self):
        """断开连接(本地后端为空操作)."""

    # ---- 目录与文件 ----
    @abstractmethod
    def list_dir(self, path: str) -> list[FileEntry]:
        """列出目录内容(不排序, 由 UI 层处理)."""

    @abstractmethod
    def stat(self, path: str) -> FileEntry | None:
        """取单个条目(跟随链接); 不存在返回 None."""

    def lstat(self, path: str) -> FileEntry | None:
        """不跟随链接的 stat; 无链接概念的后端等同 stat()."""
        return self.stat(path)

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def open_read(self, path: str):
        """返回 file-like: .read(n) -> bytes."""

    @abstractmethod
    def open_write(self, path: str):
        """返回 file-like: .write(bytes); 覆盖已存在文件."""

    @abstractmethod
    def mkdir(self, path: str): ...

    @abstractmethod
    def delete(self, path: str):
        """删除文件或目录(目录递归)."""

    def delete_to_trash(self, path: str):
        """移入回收站; 不支持的后端抛 BackendError."""
        raise BackendError("该位置不支持回收站")

    @abstractmethod
    def rename(self, old: str, new: str): ...

    # ---- 链接与元数据(能力式接口: 不支持时抛 BackendError) ----
    def read_link(self, path: str) -> str:
        """返回符号链接的目标字符串(不解析)."""
        raise BackendError("该位置不支持符号链接")

    def make_symlink(self, target: str, path: str):
        """在 path 创建指向 target 的符号链接."""
        raise BackendError("该位置不支持创建符号链接")

    def set_metadata(self, path: str, mode: int | None = None,
                     mtime: float | None = None):
        """尽力保留权限位与修改时间; 不支持时抛 BackendError."""
        raise BackendError("该位置不支持保留权限或时间戳")

    # ---- 安全提交(临时文件写入, 供传输层使用; 默认实现基于 rename) ----
    def temp_path(self, target: str) -> str:
        """为目标生成同目录任务专属临时文件路径(随机名, 不与已有文件碰撞)."""
        return self.join(self.parent(target),
                         f".fstransfer-tmp-{uuid.uuid4().hex[:12]}-{self.basename(target)}")

    def commit_temp(self, temp: str, target: str):
        """将临时文件提交为目标文件.

        语义: 尽可能原子替换; 目标已存在且后端无法原子覆盖时必须抛
        BackendError 安全失败, 绝不允许先删旧目标再提交.
        """
        self.rename(temp, target)

    def discard_temp(self, temp: str) -> str | None:
        """尽力删除本任务临时文件(非递归). 成功返回 None, 失败返回路径."""
        try:
            os.remove(temp)
        except OSError:
            return temp
        return None

    # ---- 路径工具 ----
    @abstractmethod
    def home(self) -> str: ...

    @abstractmethod
    def parent(self, path: str) -> str: ...

    @abstractmethod
    def basename(self, path: str) -> str: ...

    @abstractmethod
    def join(self, base: str, name: str) -> str: ...

    @abstractmethod
    def normpath(self, path: str) -> str: ...

    # ---- 递归收集 ----
    def walk(self, path: str, cancel_event=None):
        """兼容旧接口: (相对目录列表, [(源路径, 相对路径, 大小)], 总字节数).

        此视图把链接也当作普通条目给出(目标端会解引用读取内容).
        """
        plan = self.walk_plan(path, cancel_event)
        files = [(i.src_path, i.rel_path, i.size) for i in plan.files]
        files += [(i.src_path, i.rel_path, i.size) for i in plan.links]
        return plan.dirs, files, plan.total

    def walk_plan(self, path: str, cancel_event=None) -> WalkPlan:
        """展开目录树为执行计划.

        符号链接单独成组(lstat 语义, 不进入链接指向的目录); 普通文件与目录
        带上权限位和 mtime, 供提交后尽力还原。cancel_event 置位时抛
        CancelledError。
        """
        dirs: list[str] = []
        dir_modes: dict[str, int] = {}
        files: list[TransferItem] = []
        links: list[TransferItem] = []
        total = 0
        stack = [(path, "")]
        while stack:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("已取消")
            cur, rel = stack.pop()
            for e in self.iter_dir(cur, cancel_event):
                r = f"{rel}/{e.name}" if rel else e.name
                if e.is_dir:
                    dirs.append(r)
                    dir_modes[r] = e.mode
                    stack.append((e.path, r))
                elif e.is_link:
                    links.append(TransferItem(e.path, r, 0, e.mode, e.mtime,
                                              is_link=True))
                else:
                    files.append(TransferItem(e.path, r, e.size, e.mode, e.mtime))
                    total += e.size
        return WalkPlan(dirs, dir_modes, files, links, total)

    def iter_dir(self, path, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("已取消")
        for entry in self.list_dir(path):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("已取消")
            yield entry
