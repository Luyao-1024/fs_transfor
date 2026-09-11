"""Gio.ListStore 列表项: 包装 FileEntry."""
from gi.repository import GObject

from .backend.base import FileEntry


class FileItem(GObject.Object):
    __gtype_name__ = "FsFileItem"

    def __init__(self, entry: FileEntry):
        super().__init__()
        self.entry = entry
