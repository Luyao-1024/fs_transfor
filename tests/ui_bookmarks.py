"""路径收藏：本地/SSH 分组、持久化和收藏菜单控件。"""
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG_HOME = tempfile.mkdtemp(prefix="fstransfer-bookmarks-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = CONFIG_HOME

from gi.repository import GLib, Gtk

from fsapp import config
from fsapp.application import Application

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    state = {}

    def start(application):
        state["win"] = application.props.active_window
        state["path"] = tempfile.mkdtemp(prefix="saved-path-")
        GLib.timeout_add(600, exercise)
        return GLib.SOURCE_REMOVE

    def exercise():
        win = state["win"]
        pane = win.left
        pane.navigate(state["path"])
        win.right.navigate(state["path"])
        GLib.timeout_add(500, verify_local)
        return GLib.SOURCE_REMOVE

    def verify_local():
        win = state["win"]
        pane = win.left
        pane._set_current_bookmarked(True, "测试目录")
        check(pane._bookmark_paths() == [state["path"]],
              "本地路径加入独立收藏分组")
        check(pane._bookmark_entries()[0]["name"] == "测试目录",
              "收藏路径可以保存自定义名称")
        check(pane.bookmark_btn.get_icon_name() == "starred-symbolic",
              "当前路径已收藏时显示实心收藏图标")
        check(win.right.bookmark_btn.get_icon_name() == "starred-symbolic",
              "同分组的其他面板立即同步收藏状态")

        server_a = {"name": "服务器 A", "host": "a.example", "port": 22,
                    "username": "alice"}
        server_b = {"name": "服务器 B", "host": "b.example", "port": 22,
                    "username": "alice"}
        config.set_bookmark_paths(win.settings, server_a, ["/srv/a"])
        config.set_bookmark_paths(win.settings, server_b, ["/srv/b"])
        check(config.bookmark_paths(win.settings, server_a) == ["/srv/a"],
              "服务器 A 只读取自己的收藏")
        check(config.bookmark_paths(win.settings, server_b) == ["/srv/b"],
              "服务器 B 只读取自己的收藏")
        check(config.bookmark_paths(win.settings, None) == [state["path"]],
              "SSH 收藏不会混入本地收藏")
        check(config.bookmark_entries(
                  {"bookmarks": {config.bookmark_scope_key(server_a): ["/old"]}},
                  server_a) == [{"path": "/old", "name": ""}],
              "旧版纯路径收藏可自动迁移")

        win.on_bookmarks_changed()
        loaded = config.load_settings()
        check(config.bookmark_paths(loaded, server_a) == ["/srv/a"] and
              config.bookmark_paths(loaded, None) == [state["path"]],
              "各收藏分组已持久化到设置文件")

        pane._rebuild_bookmark_popover()
        labels = [w.get_text() for w in walk(pane.bookmark_btn.get_popover())
                  if isinstance(w, Gtk.Label)]
        check("本地收藏" in labels and "测试目录" in labels,
              "收藏弹出菜单显示当前分组和自定义名称")
        check(state["path"] not in labels,
              "收藏列表不再用过长路径作为行标题")
        pane._rename_bookmark(state["path"], "新名称")
        check(pane._bookmark_entries()[0]["name"] == "新名称",
              "已有收藏可以重命名")

        pane._set_current_bookmarked(False)
        check(not pane._bookmark_paths() and
              pane.bookmark_btn.get_icon_name() == "non-starred-symbolic",
              "可以取消收藏当前路径")
        GLib.timeout_add(100, finish)
        return GLib.SOURCE_REMOVE

    def finish():
        shutil.rmtree(state.get("path") or "/nonexistent", ignore_errors=True)
        shutil.rmtree(CONFIG_HOME, ignore_errors=True)
        print("\n" + ("收藏测试全部通过 ✅" if not FAILURES
                       else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    app.connect("activate", lambda application: GLib.idle_add(start, application))
    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
