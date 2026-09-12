"""UX-01：面板历史、最近路径、快速筛选和导航动作。"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG_HOME = tempfile.mkdtemp(prefix="fstransfer-navigation-config-")
os.environ["FSTRANSFOR_CONFIG_HOME"] = CONFIG_HOME

from gi.repository import GLib, Gtk

from fsapp.application import Application


FAILURES = []


def check(condition, message):
    print(("ok: " if condition else "FAIL: ") + message, flush=True)
    if not condition:
        FAILURES.append(message)


def model_names(pane):
    return [pane.sort_model.get_item(i).entry.name
            for i in range(pane.sort_model.get_n_items())]


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
        win = application.props.active_window
        root = Path(tempfile.mkdtemp(prefix="fstransfer-navigation-"))
        for name in ("one", "two", "three"):
            (root / name).mkdir()
        (root / "alpha.txt").write_text("alpha", encoding="utf-8")
        (root / "beta.txt").write_text("beta", encoding="utf-8")
        state.update(win=win, root=root)
        GLib.timeout_add(500, exercise_history)
        return GLib.SOURCE_REMOVE

    def exercise_history():
        win, root = state["win"], state["root"]
        pane = win.left
        pane.connect_local(str(root))
        pane.navigate(str(root / "one"))
        pane.navigate(str(root / "two"))
        check(pane._history[-3:] == [str(root), str(root / "one"), str(root / "two")],
              "当前面板按访问顺序维护导航历史")
        check(win.right._history != pane._history, "左右面板导航历史互相独立")
        check(pane.back_btn.get_sensitive() and not pane.forward_btn.get_sensitive(),
              "历史按钮敏感状态准确")
        pane.go_back()
        check(pane.cwd == str(root / "one") and pane.forward_btn.get_sensitive(),
              "后退回到上一目录并启用前进")
        pane.go_forward()
        check(pane.cwd == str(root / "two"), "前进恢复下一目录")
        pane.go_back()
        pane.navigate(str(root / "three"))
        check(pane._history[-1] == str(root / "three") and
              str(root / "two") not in pane._history[pane._history_index + 1:],
              "后退后访问新目录会丢弃旧前进分支")
        check(len(pane._recent_paths) == len(set(pane._recent_paths)) and
              str(root / "two") in pane._recent_paths,
              "最近路径去重并保留访问记录")
        pane._rebuild_recent_popover()
        recent_labels = [w.get_text() for w in walk(pane.recent_btn.get_popover())
                         if isinstance(w, Gtk.Label)]
        check(str(root / "two") in recent_labels, "最近路径菜单可打开历史目录")
        pane.navigate(str(root))
        GLib.timeout_add(500, exercise_filter)
        return GLib.SOURCE_REMOVE

    def exercise_filter():
        win, root = state["win"], state["root"]
        pane = win.left
        beta_pos = model_names(pane).index("beta.txt")
        pane.selection.select_item(beta_pos, True)
        pane.show_filter()
        pane.filter_entry.set_text("ALPHA")
        GLib.timeout_add(300, verify_filter)
        return GLib.SOURCE_REMOVE

    def verify_filter():
        win, root = state["win"], state["root"]
        pane = win.left
        check(model_names(pane) == ["alpha.txt"], "筛选名称不区分大小写")
        check(pane.cwd == str(root), "筛选不会改变真实目录")
        check(not pane._selected_paths(), "筛选变化会清除隐藏的旧选区")
        pane.filter_entry.set_text("missing-name")
        GLib.timeout_add(300, verify_no_match)
        return GLib.SOURCE_REMOVE

    def verify_no_match():
        win, root = state["win"], state["root"]
        pane = win.left
        check(not model_names(pane) and pane.empty_label.get_text() == "没有匹配的项目",
              "无匹配结果显示明确提示")
        pane.hide_filter()
        check(set(model_names(pane)) == {"one", "two", "three", "alpha.txt", "beta.txt"},
              "关闭筛选恢复完整目录列表")

        ws = win.active_workspace
        ws.activate_pane(pane)
        ws.activate_action(f"{ws.wid}.parent", None)
        check(pane.cwd == str(root.parent), "导航动作作用于当前活动面板")
        state["location_activated"] = ws.activate_action(f"{ws.wid}.location", None)
        GLib.timeout_add(100, verify_focus_path)
        return GLib.SOURCE_REMOVE

    def verify_focus_path():
        win, pane = state["win"], state["win"].left
        check(state["location_activated"] is not False and
              pane.path_entry.get_text() == str(state["root"].parent),
              "Ctrl+L 对应动作已注册且保持当前路径")
        ws = win.active_workspace
        ws.activate_action(f"{ws.wid}.filter", None)
        GLib.timeout_add(100, verify_focus_filter)
        return GLib.SOURCE_REMOVE

    def verify_focus_filter():
        pane = state["win"].left
        check(pane.filter_revealer.get_reveal_child() and pane.filter_btn.get_active(),
              "Ctrl+F 对应动作可打开筛选栏")
        GLib.timeout_add(100, finish)
        return GLib.SOURCE_REMOVE

    def finish():
        shutil.rmtree(state.get("root") or "/nonexistent", ignore_errors=True)
        shutil.rmtree(CONFIG_HOME, ignore_errors=True)
        print("\n" + ("导航效率测试全部通过 ✅" if not FAILURES
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
