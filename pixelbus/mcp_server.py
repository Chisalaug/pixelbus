"""MCP server：把引擎状态和动作暴露给 AI Agent（M4）。

工具：

- ``get_state``：当前完整快照（states / signals / last_event / actions）；
- ``list_targets``：只列规则里有哪些 state / signal，以及当前是否命中；
- ``wait_for``：阻塞等某个 state / signal 出现 / 消失 / 变化，带超时；
- ``press``：模拟键盘按键（复用 :func:`pixelbus.actions.press_keys`）。

SDK 是可选 extra（``pixelbus[mcp]``）。没装 mcp 时 CLI 会提示装哪个 extra；
本模块的 :class:`Bus` 部分不依赖 mcp，单测可以直接挂在合成引擎上跑，不用起
stdio 服务。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .actions import ActionError, press_keys
from .engine import Engine, Event
from .rules import RulesError, load_rules

__all__ = ["Bus", "build_server", "run_mcp"]


_VALID_KINDS = {"appear", "disappear", "change", "value", "any"}


# --------------------------------------------------------------------------
# 业务壳（不依赖 MCP SDK）
# --------------------------------------------------------------------------


class Bus:
    """引擎的薄封装：MCP 工具函数全部委托到这里，方便单测直接断言。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._cond = threading.Condition()
        self._pending: list[Event] = []
        self._unsubscribe = engine.add_subscriber(self._on_event)

    def _on_event(self, event: Event) -> None:
        with self._cond:
            self._pending.append(event)
            if len(self._pending) > 200:
                self._pending = self._pending[-200:]
            self._cond.notify_all()

    def close(self) -> None:
        try:
            self._unsubscribe()
        except Exception:
            pass

    # -- 工具实现 ---------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """完整快照：判据分数、state 是否成立、最近事件、动作统计。"""
        return self._engine.snapshot().to_dict()

    def list_targets(self) -> dict[str, Any]:
        """只列名字和当前命中状态，不带每条判据的细节。"""
        snap = self._engine.snapshot()
        return {
            "states": {
                name: {"active": s["active"], "since": s["since"]}
                for name, s in snap.states.items()
            },
            "signals": {
                name: {"hit": s["hit"], "level": s["level"]}
                for name, s in snap.signals.items()
            },
        }

    def wait_for(self, name: str, kind: str = "appear", timeout: float = 10.0) -> dict[str, Any]:
        """阻塞等某个 state / signal 满足条件。

        先看一眼当前状态：已经命中（appear）或已经消失（disappear）就立刻返回；
        否则订阅事件队列，等到匹配事件或超时。
        """
        if kind not in _VALID_KINDS:
            kind = "appear"
        current = self._engine.state_of(name)
        if current is None:
            return {"matched": False, "reason": f"规则里没有 {name!r} 这个 state 或 signal"}

        already = None
        if kind in ("appear", "any") and current:
            already = {"kind": "appear", "name": name, "target": "state", "already_active": True}
        elif kind == "disappear" and not current:
            already = {"kind": "disappear", "name": name, "target": "state", "already_inactive": True}
        if already is not None:
            return {"matched": True, "event": already}

        deadline = time.time() + max(0.0, float(timeout))
        with self._cond:
            self._pending.clear()
            while True:
                for event in self._pending:
                    if event.name == name and (kind == "any" or event.kind == kind):
                        return {"matched": True, "event": event.to_dict()}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"matched": False, "reason": "timeout", "timeout": timeout}
                self._cond.wait(remaining)

    def press(self, keys: str | list[str]) -> dict[str, Any]:
        """模拟按键；缺 pynput 或按键为空时返回结构化错误，不抛异常给 MCP 层。"""
        try:
            pressed = press_keys(keys)
            return {"ok": True, "keys": pressed}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# MCP 装配（SDK 懒加载）
# --------------------------------------------------------------------------


def _import_mcp_server():
    """mcp 2.x 把 FastMCP 改名成 MCPServer；v1 还在 mcp.server.fastmcp。两边都兼容。"""
    try:
        from mcp.server.mcpserver import MCPServer  # type: ignore

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # type: ignore

        return FastMCP


def build_server(bus: Bus):
    """把 Bus 的四个方法注册成 MCP 工具。不调用 ``run``，方便测试列出工具。"""
    Server = _import_mcp_server()
    server = Server(
        "pixelbus",
        instructions="把屏幕状态变成可订阅的事件流。工具只做状态查询、等待和按键，不录像、不注入页面。",
    )

    @server.tool()
    def get_state() -> dict[str, Any]:
        """当前完整快照：每个 signal 的命中/分数/判据指标，每个 state 是否成立，最近一条事件和动作统计。"""
        return bus.state()

    @server.tool()
    def list_targets() -> dict[str, Any]:
        """只列规则里有哪些 state / signal，以及当前是否命中（不带判据细节）。"""
        return bus.list_targets()

    @server.tool()
    def wait_for(name: str, kind: str = "appear", timeout: float = 10.0) -> dict[str, Any]:
        """阻塞等待某个 state/signal 出现/消失/变化。kind: appear / disappear / change / value / any；timeout 单位秒。"""
        return bus.wait_for(name, kind=kind, timeout=timeout)

    @server.tool()
    def press(keys: str | list[str]) -> dict[str, Any]:
        """模拟键盘按键，例如 "ctrl+s" 或 ["tab", "enter"]。需要 pixelbus[keys]。"""
        return bus.press(keys)

    return server


def run_mcp(rules_path: str, transport: str = "stdio") -> int:
    """CLI 入口：加载规则 → 起引擎线程 → 跑 MCP 服务。"""
    try:
        Server = _import_mcp_server()
    except ImportError:
        print("需要 mcp SDK：pip install pixelbus[mcp]")
        return 2

    try:
        rules = load_rules(rules_path)
    except RulesError as exc:
        print(str(exc))
        return 1
    except FileNotFoundError:
        print(f"找不到规则文件：{rules_path}")
        return 1

    engine = Engine(rules)
    bus = Bus(engine)
    engine.start()
    try:
        server = build_server(bus)
        server.run(transport=transport)
    finally:
        bus.close()
        engine.close()
    return 0
