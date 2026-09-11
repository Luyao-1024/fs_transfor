"""原生右键菜单回归: 点击锚点、跨窗口显示、屏幕避让与紧凑分隔线.

运行: .venv/bin/python tests/ui_menu.py (需要图形显示)
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_DIR = tempfile.TemporaryDirectory(prefix="fstransfer-cfg-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = CONFIG_DIR.name

from gi.repository import Gdk, GLib, Gtk

from fsapp.application import Application

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def walk_rows(widget):
    rows = [widget] if isinstance(widget, Gtk.ListBoxRow) else []
    child = widget.get_first_child()
    while child:
        rows.extend(walk_rows(child))
        child = child.get_next_sibling()
    return rows


def main():
    app = Application(application_id="io.github.fstransfer.MenuTest")
    files = tempfile.TemporaryDirectory(prefix="fstransfer-menu-")
    for name in ("first", "second"):
        os.mkdir(os.path.join(files.name, name))
    state = {}
    cases = [("left", "anchor"), ("right", "outside"),
             ("right", "screen")]

    def start(a):
        win = a.props.active_window
        state["win"] = win
        win.unmaximize()
        win.set_default_size(800, 440)
        win.left.navigate(files.name)
        win.right.navigate(files.name)
        GLib.timeout_add(800, step)
        return GLib.SOURCE_REMOVE

    def step():
        win = state["win"]
        if not cases:
            verify_actions(win)
            print("\n" + ("菜单测试全部通过 ✅" if not FAILURES
                           else f"{len(FAILURES)} 项失败 ❌"))
            win.close()
            app.quit()
            return GLib.SOURCE_REMOVE
        side, mode = cases.pop(0)
        if mode == "screen":
            win.maximize()
        GLib.timeout_add(500, show, side, mode)
        return GLib.SOURCE_REMOVE

    def verify_actions(win):
        # 鼠标点击由 ListBox 发出 row-activated; 行的 activate 是键盘路径。
        # 两条路径都必须执行操作, 且只能执行一次。
        calls = []
        original = win.set_clipboard

        def record(*args):
            calls.append(args)
            original(*args)

        win.set_clipboard = record
        try:
            for side, label, mode, keyboard in (
                    ("left", "复制", "copy", False),
                    ("right", "剪切", "cut", False),
                    ("left", "复制", "copy", True)):
                pane = getattr(win, side)
                pane.selection.select_item(0, True)
                paths = pane._selected_paths()
                pane._popup_menu_at(40, 40)
                menu = pane._menu_popover
                row = next(r for r in walk_rows(menu)
                           if isinstance(r.get_child(), Gtk.Box)
                           and r.get_child().get_first_child().get_text() == label)
                calls.clear()
                if keyboard:
                    row.emit("activate")
                else:
                    menu.get_child().emit("row-activated", row)
                check(len(calls) == 1 and win._clip ==
                      (pane.backend, pane.cwd, paths, mode),
                      f"{side} {label}: {'键盘' if keyboard else '鼠标'}激活执行一次操作")
                check(win._context_menu is None,
                      "菜单操作执行后自动关闭")
                win.close_context_menu()
        finally:
            win.set_clipboard = original

    def show(side, mode):
        win = state["win"]
        pane = getattr(win, side)
        pane.selection.select_item(0, True)
        x, y = ((40, 40) if mode == "anchor" else
                (pane.view.get_width() - 10, pane.view.get_height() - 10))
        ok, before = pane.view.compute_bounds(win)
        check(ok, "文件视图坐标有效")
        pane._popup_menu_at(x, y)
        GLib.timeout_add(350, verify, pane, mode, x, y, before)
        return GLib.SOURCE_REMOVE

    def verify(pane, mode, x, y, before):
        win = state["win"]
        menu = pane._menu_popover
        popup = menu.get_surface()
        check(isinstance(popup, Gdk.Popup) and popup is not win.get_surface()
              and menu.get_mapped(), "菜单使用独立原生弹出表面")
        check(win.get_content() is win.toast_overlay
              and win.left.view.get_mapped() and win.right.view.get_mapped(),
              "菜单打开时主界面与两侧列表仍显示")
        ok, after = pane.view.compute_bounds(win)
        check(ok and after.origin.x == before.origin.x
              and after.origin.y == before.origin.y,
              "菜单打开不改变文件视图位置")
        ok, rect = menu.get_pointing_to()
        check(ok and rect.x == int(x) and rect.y == int(y),
              "菜单锚点对应右键点击位置")
        px, py = popup.get_position_x(), popup.get_position_y()
        pw, ph = popup.get_width(), popup.get_height()
        print(f"  {mode}: popup=({px},{py}) {pw}x{ph}, "
              f"window={win.get_width()}x{win.get_height()}", flush=True)
        if mode == "anchor":
            # 原生表面包含阴影, 容许主题边距; 不应偏移半个菜单宽度。
            check(abs(px - (before.origin.x + x)) <= 24
                  and abs(py - (before.origin.y + y)) <= 24,
                  "菜单在点击位置附近展开")
        elif mode == "outside":
            check(px + pw > win.get_width() + 10
                  or py + ph > win.get_height() + 10,
                  "窗口边缘的菜单可以超出应用边界")
        else:
            check(px >= -24 and py >= -24
                  and px + pw <= win.get_surface().get_width() + 24
                  and py + ph <= win.get_surface().get_height() + 24,
                  "最大化时屏幕边缘菜单自动避让")
        rows = walk_rows(menu)
        check(len(rows) == 10, "文件菜单操作完整")
        separators = [r for r in rows if isinstance(r.get_child(), Gtk.Separator)]
        check(len(separators) == 1 and 0 < separators[0].get_height() <= 12,
              "分隔线紧凑, 没有整行空白")
        state["position"] = (px, py, pw, ph)
        GLib.timeout_add(250, dismiss, menu)
        return GLib.SOURCE_REMOVE

    def dismiss(menu):
        popup = menu.get_surface()
        check(state["position"] == (popup.get_position_x(), popup.get_position_y(),
                                    popup.get_width(), popup.get_height()),
              "菜单显示后位置稳定")
        menu.popdown()
        check(menu.get_parent() is None and state["win"]._context_menu is None,
              "关闭菜单后释放挂载, 可再次打开")
        GLib.timeout_add(150, step)
        return GLib.SOURCE_REMOVE

    app.connect("activate", lambda a: GLib.idle_add(start, a))
    try:
        app.run([])
    finally:
        files.cleanup()
        CONFIG_DIR.cleanup()
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
