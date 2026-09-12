"""本地文件系统后端."""
from __future__ import annotations

import os
import shutil
import stat as stat_mod

from gi.repository import Gio, GLib

from .base import BaseBackend, BackendError, CancelledError, FileEntry, perms_str


def _wrap(fn, *args, **kwargs):
    """OSError -> BackendError(消息可直接展示)."""
    try:
        return fn(*args, **kwargs)
    except OSError as e:
        raise BackendError(e.strerror or str(e)) from e


class LocalBackend(BaseBackend):
    is_local = True
    supports_links = True

    def __init__(self):
        self._home = os.path.expanduser("~")

    @property
    def label(self):
        return "本地"

    def list_dir(self, path, cancel_event=None):
        def go():
            out = []
            with os.scandir(path) as it:
                for d in it:
                    if cancel_event is not None and cancel_event.is_set():
                        raise CancelledError("已取消")
                    try:
                        st = d.stat(follow_symlinks=False)
                    except OSError:
                        continue  # 权限或竞争导致 stat 失败: 跳过该条
                    out.append(FileEntry(
                        name=d.name,
                        path=d.path,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        is_dir=stat_mod.S_ISDIR(st.st_mode),
                        perms=perms_str(st.st_mode),
                        is_link=d.is_symlink(),
                        mode=st.st_mode,
                    ))
            return out
        return _wrap(go)

    def iter_dir(self, path, cancel_event=None):
        return iter(self.list_dir(path, cancel_event))

    def stat(self, path):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise BackendError(e.strerror or str(e)) from e
        return FileEntry(
            name=os.path.basename(path.rstrip("/")) or path,
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            is_dir=stat_mod.S_ISDIR(st.st_mode),
            perms=perms_str(st.st_mode),
            mode=st.st_mode,
            is_link=os.path.islink(path),
        )

    def lstat(self, path):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise BackendError(e.strerror or str(e)) from e
        return FileEntry(
            name=os.path.basename(path.rstrip("/")) or path,
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            is_dir=stat_mod.S_ISDIR(st.st_mode),
            perms=perms_str(st.st_mode),
            mode=st.st_mode,
            is_link=stat_mod.S_ISLNK(st.st_mode),
        )

    def exists(self, path):
        return os.path.exists(path)

    def open_read(self, path):
        return _wrap(open, path, "rb")

    def open_write(self, path):
        return _wrap(open, path, "wb")

    def mkdir(self, path):
        _wrap(lambda: os.makedirs(path, exist_ok=True))

    def delete(self, path):
        if os.path.isdir(path) and not os.path.islink(path):
            return _wrap(shutil.rmtree, path)
        return _wrap(os.remove, path)

    def delete_to_trash(self, path):
        """移入 freedesktop 回收站(文件管理器中可找回)."""
        try:
            f = Gio.File.new_for_commandline_arg(path)
            ok = f.trash(None)
        except GLib.Error as e:
            raise BackendError(f"移入回收站失败: {e.message}") from None
        if not ok:
            raise BackendError("移入回收站失败(该文件系统可能不支持)")

    def rename(self, old, new):
        _wrap(os.replace, old, new)

    # ---- 链接与元数据 ----
    def read_link(self, path):
        return _wrap(os.readlink, path)

    def make_symlink(self, target, path):
        if os.path.lexists(path):
            raise BackendError(f"目标已存在, 无法创建链接: {path}")
        _wrap(os.symlink, target, path)

    def set_metadata(self, path, mode=None, mtime=None):
        """权限与时间戳尽力还原; 链接权限不可改(平台限制), 时间不跟随链接."""
        if mode and not os.path.islink(path):
            _wrap(os.chmod, path, stat_mod.S_IMODE(mode))
        if mtime is not None:
            def touch():
                cur = os.stat(path, follow_symlinks=False)
                os.utime(path, ns=(cur.st_atime_ns, int(round(float(mtime) * 1e9))),
                         follow_symlinks=False)
            _wrap(touch)

    def disconnect(self):
        pass

    # ---- 路径工具 ----
    def home(self):
        return self._home

    def parent(self, path):
        return os.path.dirname(path.rstrip("/")) or "/"

    def basename(self, path):
        return os.path.basename(path.rstrip("/"))

    def join(self, base, name):
        if not base or base == "/":
            return "/" + name
        return base.rstrip("/") + "/" + name

    def normpath(self, path):
        if not path:
            return "/"
        return os.path.normpath(path)
