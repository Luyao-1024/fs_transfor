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


def perms_str(mode) -> str:
    try:
        return stat_mod.filemode(int(mode))
    except (ValueError, TypeError):
        return "----------"


class BaseBackend(ABC):
    is_local: bool = False
    dead: bool = False

    # ---- 基本信息 ----
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
        """取单个条目; 不存在返回 None."""

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
        """展开目录树.

        返回 (相对目录列表, [(绝对源路径, 相对路径, 大小)], 总字节数).
        相对路径用于在目标端重建目录结构.
        cancel_event: 可选 threading.Event, 置位时抛 CancelledError.
        """
        dirs: list[str] = []
        files: list[tuple[str, str, int]] = []
        total = 0
        stack = [(path, "")]
        while stack:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("已取消")
            cur, rel = stack.pop()
            for e in self.list_dir(cur):
                r = f"{rel}/{e.name}" if rel else e.name
                if e.is_dir:
                    dirs.append(r)
                    stack.append((e.path, r))
                else:
                    files.append((e.path, r, e.size))
                    total += e.size
        return dirs, files, total
