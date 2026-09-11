"""本地文件系统后端."""
from __future__ import annotations

import os
import shutil
import stat as stat_mod

from gi.repository import Gio, GLib

from .base import BaseBackend, BackendError, FileEntry, perms_str


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OSError as e:
        raise BackendError(e.strerror or str(e)) from e


class LocalBackend(BaseBackend):
    is_local = True

    def __init__(self):
        self._home = os.path.expanduser("~")

    @property
    def label(self):
        return "本地"

    def list_dir(self, path):
        def go():
            out = []
            with os.scandir(path) as it:
                for d in it:
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
                    ))
            return out
        return _wrap(go)

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
