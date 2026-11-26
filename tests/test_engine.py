"""引擎：边沿、去抖、组合、cooldown、热重载。

全部用"喂图"的方式跑（``Engine.step(frames=...)``），不碰真实屏幕，
所以这些用例在 CI 上也是确定的。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixelbus.capture import RegionSpec, rect_to_bbox
from pixelbus.engine import Engine, Event
from pixelbus.rules import loads_rules

from conftest import H, W, bgra_of, blank, fill_rect

RED = "#d0021b"
SIZE = (400, 120)


def red_frame(ratio: float = 0.3, size=SIZE) -> np.ndarray:
    im = blank(size=size)
    fill_rect(im, [0, 0, int(size[0] * ratio) - 1, size[1] - 1], RED)
    return bgra_of(im)


def white_frame(size=SIZE) -> np.ndarray:
    return bgra_of(blank(size=size))


def make_rules(signals_yaml: str, states_yaml: str, fps: float = 10, extra: str = "") -> object:
    text = f"""
version: 1
fps: {fps}
regions:
  r: {{rect: [0, 0, 1, 1]}}
  r2: {{rect: [0, 0, 1, 1]}}
signals:
{signals_yaml}
states:
{states_yaml}
{extra}
"""
    return loads_rules(text)


SIMPLE_SIGNAL = """
  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
"""


def kinds(events, target=None):
    """只取某类对象的 kind —— signal 和 state 会同一帧一起报，混着比没意义。"""
    return [e.kind for e in events if target is None or e.target == target]


def simple_engine(states="  on: {when: red}", fps=10, signal=SIMPLE_SIGNAL, extra="") -> Engine:
    rules = make_rules(signal, states, fps=fps, extra=extra)
    return Engine(rules, grabber=FakeGrabber())


class FakeGrabber:
    """假抓屏器：只提供 bbox 解析和"一帧默认画面"，抓屏次数会被数下来。"""

    dpi = "fake"

    def __init__(self, size=SIZE) -> None:
        self.size = size
        self.frame = white_frame(size)
        self.calls = 0
        self.fail = False
        self.monitors = [
            {"left": 0, "top": 0, "width": size[0], "height": size[1]},
            {"left": 0, "top": 0, "width": size[0], "height": size[1], "is_primary": True},
        ]

    def monitor(self, spec) -> dict:
        return self.monitors[1]

    def grab_bbox(self, bbox) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise RuntimeError("模拟抓屏失败")
        h, w = bbox["height"], bbox["width"]
        return self.frame[:h, :w].copy()

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 边沿
# --------------------------------------------------------------------------


class TestSignalEdges:
    def test_appear_then_disappear(self):
        engine = simple_engine()
        events = engine.step(1.0, {"r": red_frame()})
        assert kinds(events, "signal") == ["appear"], "第一帧就命中要立刻报"
        events = engine.step(1.1, {"r": white_frame()})
        assert kinds(events, "signal") == ["disappear"]
        assert events[0].name == "red" and events[0].target == "signal"

    def test_no_duplicate_events_while_stable(self):
        engine = simple_engine()
        engine.step(1.0, {"r": red_frame()})
        assert engine.step(1.1, {"r": red_frame()}) == []
        assert engine.step(1.2, {"r": red_frame()}) == []
        assert engine.step(1.3, {"r": red_frame()}) == []

    def test_event_dict_shape(self):
        engine = simple_engine()
        event = engine.step(1.0, {"r": red_frame()})[0]
        body = event.to_dict()
        assert set(body) == {"kind", "name", "target", "t", "level", "detail"}
        assert body["detail"]["judges"][0]["name"] == "color_ratio"
        assert "bbox" in body["detail"]


# --------------------------------------------------------------------------
# 去抖
# --------------------------------------------------------------------------


class TestDebounce:
    def test_three_frames_required(self):
        engine = simple_engine(states="  on: {when: red, stable_frames: 3}")
        engine.step(1.0, {"r": red_frame()})  # signal 立刻命中，state 还在数
        assert engine.state_of("on") is False
        engine.step(1.1, {"r": red_frame()})
        assert engine.state_of("on") is False, "两帧不够"
        events = engine.step(1.2, {"r": red_frame()})
        assert kinds(events, "state") == ["appear"], "第三帧才报"
        assert engine.state_of("on") is True

    def test_single_miss_resets_streak(self):
        """计数中途掉一帧就归零 —— 否则"闪两下、停一下、再闪一下"也会被当成稳定出现。"""
        engine = simple_engine(states="  on: {when: red, stable_frames: 3}")
        engine.step(1.0, {"r": red_frame()})
        engine.step(1.1, {"r": red_frame()})
        engine.step(1.2, {"r": white_frame()})
        engine.step(1.3, {"r": red_frame()})
        engine.step(1.4, {"r": red_frame()})
        assert engine.state_of("on") is False
        engine.step(1.5, {"r": red_frame()})  # 从头数满三帧
        assert engine.state_of("on") is True

    def test_disappear_needs_stable_frames_too(self):
        engine = simple_engine(states="  on: {when: red, stable_frames: 2}")
        engine.step(1.0, {"r": red_frame()})
        engine.step(1.1, {"r": red_frame()})
        assert engine.state_of("on") is True
        assert kinds(engine.step(1.2, {"r": white_frame()}), "state") == [], "一帧没命中，还撑着"
        events = engine.step(1.3, {"r": white_frame()})
        assert kinds(events, "state") == ["disappear"]

    def test_stable_frames_one_is_immediate(self):
        engine = simple_engine(states="  on: {when: red, stable_frames: 1}")
        events = engine.step(1.0, {"r": red_frame()})
        assert {e.name for e in events if e.kind == "appear"} == {"red", "on"}
        assert engine.state_of("on") is True


# --------------------------------------------------------------------------
# 组合
# --------------------------------------------------------------------------


TWO_SIGNALS = """
  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
  ink: {region: r2, test: {ink_ratio: {min: 0.02}}}
"""


class TestWhen:
    def test_and_expression(self):
        engine = simple_engine(states='  both: {when: "red & ink", stable_frames: 1}', signal=TWO_SIGNALS)
        engine.step(1.0, {"r": red_frame(), "r2": white_frame()})
        engine.step(1.1, {"r": red_frame(), "r2": white_frame()})
        assert engine.state_of("both") is False
        events = engine.step(1.2, {"r": red_frame(), "r2": red_frame()})
        assert [e.name for e in events if e.kind == "appear" and e.target == "state"] == ["both"]

    def test_not_expression(self):
        engine = simple_engine(states='  quiet: {when: "!red", stable_frames: 1}')
        whites = white_frame()
        events = engine.step(1.0, {"r": whites})
        assert [e.name for e in events if e.kind == "appear" and e.target == "state"] == ["quiet"]
        engine.step(1.1, {"r": red_frame()})
        assert engine.state_of("quiet") is False

    def test_state_depends_on_state(self):
        engine = simple_engine(
            states="""  first: {when: red, stable_frames: 1}
  second: {when: first, stable_frames: 1}""",
        )
        event = engine.step(1.0, {"r": red_frame()})
        assert ("appear", "first") in [(e.kind, e.name) for e in event]
        assert engine.state_of("second") is True, "同帧内 state 应该能级联"
        assert engine.step(1.1, {"r": red_frame()}) == []

    def test_list_when_is_and(self):
        engine = simple_engine(states="  both: {when: [red, ink], stable_frames: 1}", signal=TWO_SIGNALS)
        engine.step(1.0, {"r": red_frame(), "r2": white_frame()})
        engine.step(1.1, {"r": red_frame(), "r2": white_frame()})
        assert engine.state_of("both") is False


# --------------------------------------------------------------------------
# cooldown / change / value
# --------------------------------------------------------------------------


class TestCooldownAndValues:
    def test_cooldown_suppresses_second_appear(self):
        engine = simple_engine(
            signal="""  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}, cooldown: 2.0}"""
        )
        assert kinds(engine.step(1.0, {"r": red_frame()}), "signal") == ["appear"]
        assert engine.step(1.1, {"r": red_frame()}) == []
        assert kinds(engine.step(1.2, {"r": white_frame()}), "signal") == ["disappear"], "消失不受 cooldown 限制"
        assert kinds(engine.step(1.3, {"r": red_frame()}), "signal") == [], "2 秒冷却内的第二次命中要被吞掉"
        assert engine.state_of("red") is True

    def test_cooldown_expires(self):
        engine = simple_engine(
            signal="""  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}, cooldown: 2.0}"""
        )
        engine.step(1.0, {"r": red_frame()})
        engine.step(1.2, {"r": white_frame()})
        engine.step(3.5, {"r": white_frame()})
        events = engine.step(3.6, {"r": red_frame()})
        assert kinds(events, "signal") == ["appear"], "冷却已过，允许再报"

    def test_change_fires_when_value_moves(self):
        engine = simple_engine()
        engine.step(1.0, {"r": red_frame(0.3)})
        engine.step(1.1, {"r": red_frame(0.3)})
        events = engine.step(1.2, {"r": red_frame(0.6)})
        assert [e.kind for e in events] == ["change"]
        assert events[0].detail["judges"][0]["value"] == pytest.approx(0.6, abs=0.02)

    def test_value_events_are_rate_limited(self):
        engine = simple_engine(
            signal="""  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}, values: true}"""
        )
        engine.step(1.0, {"r": red_frame()})
        engine.step(1.1, {"r": red_frame()})
        value_events = [e for e in engine.step(2.5, {"r": red_frame()}) if e.kind == "value"]
        assert len(value_events) == 1
        assert value_events[0].target == "signal"


# --------------------------------------------------------------------------
# 抓屏次数 / 失败 / 快照 / 热重载
# --------------------------------------------------------------------------


class TestGrabAndSnapshot:
    def test_one_grab_per_region_even_with_many_signals(self):
        """两条 signal 共用一个区域 → 一帧只该抓一次，这是 CPU 预算的关键。"""
        grabber = FakeGrabber()
        rules = make_rules(
            """
  a: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
  b: {region: r, test: {ink_ratio: {min: 0.02}}}
""",
            "  on: {when: a}",
        )
        engine = Engine(rules, grabber=grabber)
        engine.step(1.0)
        engine.step(1.1)
        assert grabber.calls == 2, "两个 signal 共用区域，却抓了多次"

    def test_grab_failure_does_not_crash(self):
        grabber = FakeGrabber()
        grabber.fail = True
        engine = Engine(make_rules(SIMPLE_SIGNAL, "  on: {when: red}"), grabber=grabber)
        engine.step(1.0)
        engine.step(1.1)
        snap = engine.snapshot()
        assert snap.signals["red"]["error"]
        assert engine.grab_errors >= 1
        assert engine.state_of("red") is False

    def test_snapshot_has_everything(self):
        engine = simple_engine()
        engine.step(1.0, {"r": red_frame()})
        snap = engine.snapshot()
        assert snap.fps == 10
        assert set(snap.signals) == {"red"}
        assert set(snap.states) == {"on"}
        assert snap.signals["red"]["bbox"] == {"left": 0, "top": 0, "width": 400, "height": 120}
        body = snap.to_dict()
        assert body["app"] == "pixelbus"
        assert body["signals"]["red"]["judges"][0]["name"] == "color_ratio"

    def test_state_of_unknown_name(self):
        assert simple_engine().state_of("nope") is None

    def test_hot_reload_rebuilds_state(self):
        engine = simple_engine()
        engine.step(1.0, {"r": red_frame()})
        engine.step(1.1, {"r": red_frame()})
        assert engine.state_of("red") is True
        engine.set_rules(make_rules(SIMPLE_SIGNAL, "  on: {when: red}"))
        assert engine.state_of("red") is False, "重载后应该回到干净状态"

    def test_reload_resets_diff_baseline(self):
        rules = make_rules(
            "  moved: {region: r, test: {diff: {min: 0.05}}}",
            "  on: {when: moved}",
        )
        engine = Engine(rules, grabber=FakeGrabber())
        engine.step(1.0, {"r": white_frame()})
        engine.set_rules(rules)
        # 重载后第一帧只是新基线（diff 恒为 0），第二帧才看得出变化
        engine.step(1.1, {"r": white_frame()})
        assert engine.state_of("moved") is False
        engine.step(1.2, {"r": red_frame(0.6)})
        assert engine.state_of("moved") is True


class TestLifecycle:
    def test_start_stop_is_idempotent(self):
        engine = Engine(make_rules(SIMPLE_SIGNAL, "  on: {when: red}"), grabber=FakeGrabber())
        engine.start()
        engine.start()
        engine.stop()
        engine.stop()
        assert engine.snapshot().frames >= 1

    def test_close_closes_owned_grabber_only(self):
        grabber = FakeGrabber()
        engine = Engine(make_rules(SIMPLE_SIGNAL, "  on: {when: red}"), grabber=grabber)
        engine.close()
        assert grabber.calls == 0  # 借来的 grabber 不该被关掉
