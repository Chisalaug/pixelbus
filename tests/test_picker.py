"""框选：解释器挑选 + 结果解析（GUI 窗口本身不在这里测）。

真窗口只能靠人点，这里钉死的是"更容易坏的那半"：**选哪个解释器、怎么把
一行 JSON 变成 PickResult**。这台机器上正好有个反例——本体解释器 import tkinter
成功但开不了 Tk 窗口（Tcl 数据被裁掉了），所以这套挑选逻辑是真被用上的。
"""

from __future__ import annotations

import json
import sys

import pytest

from pixelbus import picker


@pytest.fixture(autouse=True)
def _fresh_cache():
    picker.clear_tk_cache()
    yield
    picker.clear_tk_cache()


def test_backends_puts_env_override_first(monkeypatch):
    monkeypatch.setenv("PIXELBUS_TK_PYTHON", r"D:\fake\python.exe")
    cmds = picker.tk_backends()
    assert cmds[0] == [r"D:\fake\python.exe"]
    assert [sys.executable] in cmds, "本体解释器要在候选里，正常机器上它最快"


def test_backends_without_env_starts_with_self(monkeypatch):
    monkeypatch.delenv("PIXELBUS_TK_PYTHON", raising=False)
    assert picker.tk_backends()[0] == [sys.executable]


def test_tk_usable_false_for_interpreter_that_dies():
    assert picker.tk_usable([sys.executable, "-c", "raise SystemExit(3)"]) is False


def test_tk_usable_caches():
    cmd = [sys.executable, "-c", "raise SystemExit(1)"]
    assert picker.tk_usable(cmd) is False
    before = len(picker._cached_ok)
    picker.tk_usable(cmd)
    assert len(picker._cached_ok) == before, "同一个解释器不该被反复探"


def test_pick_region_parses_rect_and_bbox(monkeypatch):
    monkeypatch.setattr(picker, "_first_usable", lambda: [sys.executable])
    monkeypatch.setattr(
        picker, "_run_gui", lambda cmd, cfg: {"rect": [0.5, 0.25, 0.25, 0.25], "px": [480, 270]}
    )
    got = picker.pick_region("primary", grabber=_FakeGrabber(1920, 1080, 0, 0))
    assert got is not None
    assert got.rect == (0.5, 0.25, 0.25, 0.25)
    assert got.bbox == {"left": 960, "top": 270, "width": 480, "height": 270}
    assert "0.500" in got.yaml_snippet("alarm")


def test_pick_region_respects_monitor_offset(monkeypatch):
    monkeypatch.setattr(picker, "_first_usable", lambda: [sys.executable])
    monkeypatch.setattr(picker, "_run_gui", lambda cmd, cfg: {"rect": [0.0, 0.0, 0.5, 0.5]})
    got = picker.pick_region(2, grabber=_FakeGrabber(1280, 720, 1920, 0))
    assert got.bbox == {"left": 1920, "top": 0, "width": 640, "height": 360}


def test_pick_region_cancel_returns_none(monkeypatch):
    monkeypatch.setattr(picker, "_first_usable", lambda: [sys.executable])
    monkeypatch.setattr(picker, "_run_gui", lambda cmd, cfg: {"rect": None})
    assert picker.pick_region("primary", grabber=_FakeGrabber(1920, 1080, 0, 0)) is None


def test_pick_region_raises_when_no_usable_tk(monkeypatch):
    monkeypatch.setattr(picker, "_first_usable", lambda: None)
    with pytest.raises(RuntimeError) as exc:
        picker.pick_region("primary", grabber=_FakeGrabber(1920, 1080, 0, 0))
    assert "PIXELBUS_TK_PYTHON" in str(exc.value), "报错必须告诉用户还能怎么自救"


def test_run_gui_surfaces_child_error(monkeypatch):
    monkeypatch.setattr(picker, "_first_usable", lambda: [sys.executable])
    with pytest.raises(RuntimeError) as exc:
        picker._run_gui([sys.executable, "-c", "raise SystemExit(2)"], {})
    assert str(exc.value), "子进程失败不能静默变成 None（会被当成用户按了 Esc）"


def test_gui_script_is_self_contained():
    """窗口脚本要能被"外来的"Python 跑，所以不许 import pixelbus / 用相对导入。"""
    lines = [ln.strip() for ln in picker.GUI_SCRIPT.read_text(encoding="utf-8").splitlines()]
    assert not any(ln.startswith("import pixelbus") or ln.startswith("from .") for ln in lines)
    text = "\n".join(lines)
    assert "tkinter" in text
    assert 'json.dumps({"rect": None})' in text, "父进程按这一行 JSON 约定解析，别改格式"



class _FakeGrabber:
    def __init__(self, w, h, left, top):
        self._mon = {
            "left": left,
            "top": top,
            "width": w,
            "height": h,
            "is_primary": True,
            "index": 1,
        }

    def monitor(self, spec):  # noqa: ARG002 - 测试里只关心几何
        return dict(self._mon)

    def close(self):
        pass
