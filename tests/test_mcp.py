"""MCP 业务壳测试：Bus 不依赖 mcp SDK；SDK 装配只做"工具注册成功"的轻量校验。

wait_for 用真实引擎 + 合成图驱动：等待线程阻塞在条件变量上，主线程 step 出事件后
唤醒。press 用假 Controller 录按键，不真按键盘。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from conftest import bgra_of, blank, fill_rect
from pixelbus.engine import Engine
from pixelbus.mcp_server import Bus, build_server
from pixelbus.rules import loads_rules


def red_frame():
    im = blank((64, 64))
    fill_rect(im, [0, 0, 63, 63], "#d0021b")
    return bgra_of(im)


def white_frame():
    return bgra_of(blank((64, 64)))


def rules_text() -> str:
    return """
version: 1
fps: 10
regions:
  r: {rect: [0, 0, 1, 1]}
signals:
  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
states:
  hit: {when: red, stable_frames: 1}
actions: []
"""


def make_engine() -> Engine:
    return Engine(loads_rules(rules_text()))


# ---------------------------------------------------------------- 查询


def test_state_snapshot_has_states_signals_actions():
    engine = make_engine()
    engine.step(1.0, {"r": red_frame()})
    bus = Bus(engine)
    snap = bus.state()

    assert snap["app"] == "pixelbus"
    assert "hit" in snap["states"]
    assert "red" in snap["signals"]
    assert snap["actions"]["actions"] == 0
    engine.close()


def test_list_targets_reports_active_state():
    engine = make_engine()
    engine.step(1.0, {"r": red_frame()})
    bus = Bus(engine)
    targets = bus.list_targets()

    assert targets["states"]["hit"]["active"] is True
    assert targets["signals"]["red"]["hit"] is True
    engine.close()


# ---------------------------------------------------------------- wait_for


def test_wait_for_already_active_returns_immediately():
    engine = make_engine()
    engine.step(1.0, {"r": red_frame()})
    bus = Bus(engine)

    result = bus.wait_for("hit", kind="appear", timeout=1.0)
    assert result["matched"] is True
    assert result["event"].get("already_active") is True
    engine.close()


def test_wait_for_appear_later_wakes_on_event():
    engine = make_engine()
    bus = Bus(engine)
    result: dict = {}

    def waiter() -> None:
        result.update(bus.wait_for("hit", kind="appear", timeout=3.0))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)  # 先挂上等，再 step 出事件
    engine.step(1.0, {"r": red_frame()})
    t.join(timeout=3.0)

    assert result["matched"] is True
    assert result["event"]["kind"] == "appear"
    assert result["event"]["name"] == "hit"
    engine.close()


def test_wait_for_disappear_wakes_on_disappear_event():
    engine = make_engine()
    engine.step(1.0, {"r": red_frame()})
    bus = Bus(engine)
    result: dict = {}

    def waiter() -> None:
        result.update(bus.wait_for("hit", kind="disappear", timeout=3.0))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    engine.step(1.1, {"r": white_frame()})
    t.join(timeout=3.0)

    assert result["matched"] is True
    assert result["event"]["kind"] == "disappear"
    engine.close()


def test_wait_for_timeout_returns_not_matched():
    engine = make_engine()
    bus = Bus(engine)

    result = bus.wait_for("hit", kind="appear", timeout=0.2)
    assert result["matched"] is False
    assert result["reason"] == "timeout"
    engine.close()


def test_wait_for_unknown_name_reports_missing():
    engine = make_engine()
    bus = Bus(engine)

    result = bus.wait_for("nope", timeout=1.0)
    assert result["matched"] is False
    assert "nope" in result["reason"]
    engine.close()


# ---------------------------------------------------------------- press


def test_press_uses_shared_key_parser(monkeypatch):
    import pynput.keyboard as kbmod

    calls: list[str] = []

    class FakeController:
        def press(self, k):
            calls.append(f"press:{k}")

        def release(self, k):
            calls.append(f"release:{k}")

    monkeypatch.setattr(kbmod, "Controller", FakeController)
    engine = make_engine()
    bus = Bus(engine)

    result = bus.press("ctrl+s")
    assert result["ok"] is True
    assert result["keys"] == ["ctrl", "s"]
    assert calls[0].startswith("press:Key.ctrl")
    assert calls[-1].startswith("release:Key.ctrl")
    engine.close()


# ---------------------------------------------------------------- SDK 装配


def test_build_server_registers_four_tools():
    pytest.importorskip("mcp.server.mcpserver")
    engine = make_engine()
    bus = Bus(engine)
    server = build_server(bus)

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"get_state", "list_targets", "wait_for", "press"}
    engine.close()
