"""交互修复验证: activate 信号导航 / 单元格右键手势 / 弹出菜单.

运行: .venv/bin/python tests/ui_activate.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
# 隔离配置目录: 不读/不写用户的真实会话与服务器配置
os.environ["FSTRANSFOR_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fstransfer-cfg-")

from gi.repository import GLib, Gtk

from fsapp.application import Application

FAILURES = []


def check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def walk(w, out=None):
    out = out if out is not None else []
    out.append(w)
    child = w.get_first_child()
    while child:
        walk(child, out)
        child = child.get_next_sibling()
    return out


def menu_labels(model):
    labels = []
    for i in range(model.get_n_items()):
        value = model.get_item_attribute_value(i, "label", None)
        if value is not None:
            labels.append(value.get_string())
        for link_name in ("section", "submenu"):
            linked = model.get_item_link(i, link_name)
            if linked is not None:
                labels.extend(menu_labels(linked))
    return labels


def main():
    app = Application(application_id="io.github.fstransfer.Test")
    app.connect("activate", lambda a: GLib.idle_add(start, a))
    state = {"win": None, "tmpdir": None}

    def start(a):
        state["win"] = a.props.active_window
        tmpdir = tempfile.mkdtemp(prefix="fstransfer-act-")
        state["tmpdir"] = tmpdir
        os.makedirs(f"{tmpdir}/subdir")
        with open(f"{tmpdir}/file.txt", "wb") as f:
            f.write(b"x")
        win = state["win"]
        win.left.navigate(tmpdir)
        GLib.timeout_add(700, step_activate)
        return GLib.SOURCE_REMOVE

    def step_activate():
        win = state["win"]
        # 找到 subdir 的位置, emit 内建 activate 信号(等价于双击/回车)
        pos = -1
        for i in range(win.left.sort_model.get_n_items()):
            if win.left.sort_model.get_item(i).entry.name == "subdir":
                pos = i
                break
        if pos < 0:
            check(False, "找不到测试目录 subdir")
            return GLib.SOURCE_REMOVE
        # 双击文件(非目录)不应导航
        fpos = -1
        for i in range(win.left.sort_model.get_n_items()):
            if win.left.sort_model.get_item(i).entry.name == "file.txt":
                fpos = i
                break
        cwd_before = win.left.cwd
        win.left.view.emit("activate", fpos)
        check(win.left.cwd == cwd_before, "激活文件: 不导航")
        # 双击目录应导航
        win.left.view.emit("activate", pos)
        check(win.left.cwd.endswith("/subdir"), f"activate 信号进入子目录(cwd={win.left.cwd})")
        # 退回
        win.left.navigate(state["tmpdir"])
        GLib.timeout_add(500, step_cell_menu)
        return GLib.SOURCE_REMOVE

    def step_cell_menu():
        win = state["win"]
        # 每条完整高亮行都应有 button=3 的 GestureClick
        menus = []
        for w in walk(win.left.view):
            try:
                obs = w.observe_controllers()
                for i in range(obs.get_n_items()):
                    c = obs.get_item(i)
                    if isinstance(c, Gtk.GestureClick) and c.get_button() == 3:
                        menus.append((w, c))
            except Exception:
                pass
        # 排除 view 空白区域手势，只统计已绑定文件的高亮行。
        bound = [(w, c) for w, c in menus if win.left._cell_item(w) is not None]
        check(len(bound) == win.left.store.get_n_items(),
              f"每个可见高亮行都挂了右键手势({len(bound)} 行)")
        if bound:
            w, gesture = bound[0]
            item = win.left._cell_item(w)
            check(hasattr(item, "entry"),
                  f"从高亮行能取到 item({type(item).__name__})")
            try:
                pos = win.left._position_of(item)
                blank_y = float(win.left.view.get_height() - 10)
                win.left.selection.select_item(pos, True)
                win.left._view_primary_gesture.emit(
                    "released", 1, 5.0, blank_y)
                check(win.left.selection.get_selection().get_size() == 0,
                      "左键点击列表空白会清除选区")

                win.left.selection.select_item(pos, True)
                win.left._view_primary_gesture.emit(
                    "released", 1, 5.0, 5.0)
                check(win.left.selection.get_selection().get_size() == 1,
                      "点击列标题不会误清除选区")

                gesture.emit("pressed", 1, 5.0, 5.0)
                check(win.left._menu_popover is None,
                      "右键按下时不提前弹出菜单")
                gesture.emit("released", 1, 5.0, 5.0)
                check(win.left._menu_popover is not None and
                      win.left._menu_popover.props.visible,
                      "右键释放后弹出菜单")
                check(not win.manager.transfers,
                      "弹出右键菜单不会自动传输")
                win.left._menu_popover.popdown()

                win.left._view_context_gesture.emit(
                    "released", 1, 5.0, blank_y)
                check(win.left.selection.get_selection().get_size() == 0,
                      "右键点击列表空白也会清除选区")
                # 菜单为自绘 Popover+ListBox: 从行部件收集标签
                lbls = [x.get_text() for x in walk(win.left._menu_popover)
                        if isinstance(x, Gtk.Label) and x.get_text()]
                check("删除" not in lbls and "传输到对侧" not in lbls,
                      "空白区域菜单不显示文件专属操作")
                check("新建文件夹" in lbls and "刷新" in lbls,
                      "空白区域菜单保留目录级操作")
                GLib.timeout_add(300, finish)
                return GLib.SOURCE_REMOVE
            except Exception as e:
                check(False, f"弹出菜单异常: {e}")
        finish()
        return GLib.SOURCE_REMOVE

    def finish():
        import shutil
        shutil.rmtree(state.get("tmpdir") or "/nonexistent", ignore_errors=True)
        print("\n" + ("交互测试全部通过 ✅" if not FAILURES else f"{len(FAILURES)} 项失败 ❌"))
        state["win"].get_application().quit()
        return GLib.SOURCE_REMOVE

    rc = app.run([])
    if FAILURES:
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
