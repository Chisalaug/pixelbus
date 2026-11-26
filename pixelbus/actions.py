"""动作分发：state 事件 → press / webhook / exec / mqtt / log / beep。

线程模型：引擎帧循环调 :meth:`ActionRunner.handle`；真正执行在内部工作线程里，
webhook / exec / mqtt 慢一点也不会卡住抓屏。单测里可以 ``sync=True`` 同步执行，
方便断言"这条动作到底有没有被调、调了什么"。

动作触发条件（全部满足才执行）：

1. 事件的名字在 ``on`` 列表里；
2. 事件类型和 ``when`` 对得上（``appear`` / ``disappear`` / ``change`` / ``value`` / ``any``）；
3. 没在这条动作自己的 cooldown 里。

**注意**：动作是"看到状态变化就做一次"，不是"状态保持期间一直循环做"。
``repeat`` 留作预留（默认 1），后续里程碑再决定语义。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .rules import ActionCfg

__all__ = ["ActionRunner", "ActionError", "action_kinds", "kind_of", "press_keys"]

# 不算是动作类型的 do 内键（正文/自定义载荷）
_NON_KIND = {"text", "payload"}
_HOT_KEYS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "meta": "cmd",
    "cmd": "cmd",
    "win": "cmd",
    "super": "cmd",
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "esc": "esc",
    "escape": "esc",
    "space": "space",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "backspace": "backspace",
    "delete": "delete",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "pagedown": "page_down",
}


def action_kinds() -> list[str]:
    return ["webhook", "press", "exec", "mqtt", "log", "beep"]


def kind_of(do: Mapping[str, Any]) -> str:
    """从 do 字典里挑出动作类型。``text`` / ``payload`` 是正文，不算类型。"""
    for key in do:
        if key not in _NON_KIND:
            return str(key)
    return "log"


def _kind_match(when: str, kind: str) -> bool:
    return when == "any" or when == kind


class ActionError(Exception):
    """动作执行失败。带一条能给人看的中文说明。"""


@dataclass
class ActionResult:
    ok: bool
    kind: str
    target: str
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 动作实现
# --------------------------------------------------------------------------


class ActionRunner:
    """收集动作定义，按事件分发。

    Parameters
    ----------
    rules:
        规则集（读 ``actions``）。
    base_dir:
        exec 的工作目录（规则文件所在目录）。
    sink:
        log 动作的落点。给一个 ``Callable[[str, dict], None]``；默认只往这里记，
        不往引擎事件流里塞。
    """

    def __init__(
        self,
        rules: Any,
        *,
        base_dir: str | None = None,
        sink: Callable[[str, dict[str, Any]], None] | None = None,
        worker: bool = True,
    ) -> None:
        self._actions: list[ActionCfg] = list(rules.actions)
        self._base_dir = base_dir
        self._sink = sink
        self._lock = threading.Lock()
        self._last_fired: dict[int, float] = {}
        self._counts = {"fired": 0, "ok": 0, "failed": 0}
        self._last_error = ""
        self._queue: "queue.Queue[_Request]" = queue.Queue()
        self._mqtt: dict[str, Any] = {"client": None, "host": "", "port": 0, "error": ""}
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        if worker:
            self._worker = threading.Thread(target=self._worker_loop, name="pixelbus-actions", daemon=True)
            self._worker.start()

    # -- 生命周期 --------------------------------------------------------
    def configure(self, rules: Any) -> None:
        """热重载：换一套动作定义，冷却记录也清掉。"""
        with self._lock:
            self._actions = list(rules.actions)
            self._last_fired.clear()

    def shutdown(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        client = self._mqtt.get("client")
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "actions": len(self._actions),
                "fired": self._counts["fired"],
                "ok": self._counts["ok"],
                "failed": self._counts["failed"],
                "last_error": self._last_error,
                "mqtt": self._mqtt.get("error", ""),
            }

    # -- 分发 ------------------------------------------------------------
    def handle(self, event: Any, sync: bool = False) -> list[ActionResult]:
        """引擎在产生事件后调用。返回本次匹配到的动作结果（sync 模式下才是真结果）。"""
        results: list[ActionResult] = []
        with self._lock:
            actions = list(self._actions)
            now = event.t
            due: list[ActionCfg] = []
            for action in actions:
                if event.name not in action.on:
                    continue
                if not _kind_match(action.when, event.kind):
                    continue
                key = id(action)
                if now - self._last_fired.get(key, -1e9) < action.cooldown:
                    continue
                self._last_fired[key] = now
                due.append(action)

        for action in due:
            req = _Request(action=action, event=event)
            if self._worker is None or sync:
                results.append(self._dispatch(req))
            else:
                self._queue.put(req)
        return results

    # -- 工作线程 --------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                req = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._dispatch(req)

    def _dispatch(self, req: _Request) -> ActionResult:
        action, event = req.action, req.event
        kind = kind_of(action.do)
        self._counts["fired"] += 1
        try:
            detail = self._run(kind, action, event)
            self._counts["ok"] += 1
            result = ActionResult(True, kind, event.name, "", detail)
        except ActionError as exc:
            self._counts["failed"] += 1
            self._last_error = str(exc)
            result = ActionResult(False, kind, event.name, str(exc), {"event": event.to_dict()})
        except Exception as exc:  # 动作里的意外不能拖垮引擎
            self._counts["failed"] += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            result = ActionResult(False, kind, event.name, self._last_error, {"event": event.to_dict()})
        return result

    # -- 各动作实现 -------------------------------------------------------
    def _run(self, kind: str, action: ActionCfg, event: Any) -> dict[str, Any]:
        if kind == "log":
            return self._log(action, event)
        if kind == "beep":
            return self._beep(action)
        if kind == "press":
            return self._press(action)
        if kind == "webhook":
            return self._webhook(action, event)
        if kind == "exec":
            return self._exec(action)
        if kind == "mqtt":
            return self._mqtt_publish(action, event)
        raise ActionError(f"不认识的动作类型：{kind!r}")

    def _log(self, action: ActionCfg, event: Any) -> dict[str, Any]:
        raw = action.do.get("log")
        message = action.text or (raw if isinstance(raw, str) and raw.strip() else "")
        text = message or f"{event.kind} {event.target}:{event.name} lvl={event.level:.2f}"
        record = {"text": text, "event": event.to_dict(), "action": list(action.on)}
        if self._sink is not None:
            self._sink(kind="log", record=record)
        else:
            print(f"[pixelbus] {text}")
        return {"text": text}

    def _beep(self, action: ActionCfg) -> dict[str, Any]:
        import sys

        if sys.platform != "win32":
            raise ActionError("beep 目前只在 Windows 上支持（winsound）")
        import winsound

        cfg = action.do.get("beep", True)
        if isinstance(cfg, Mapping):
            freq = int(cfg.get("freq", 880))
            ms = int(cfg.get("ms", 200))
        elif isinstance(cfg, (int, float)):
            freq, ms = int(cfg), 200
        else:
            freq, ms = 880, 200
        winsound.Beep(freq, ms)
        return {"freq": freq, "ms": ms}

    def _press(self, action: ActionCfg) -> dict[str, Any]:
        keys = press_keys(action.do.get("press"))
        return {"keys": keys}

    def _webhook(self, action: ActionCfg, event: Any) -> dict[str, Any]:
        url = action.do.get("webhook")
        if not isinstance(url, str) or not url:
            raise ActionError("webhook 缺少 URL")
        body = {
            "text": action.text,
            "event": event.to_dict(),
            "action": {"on": list(action.on), "kind": kind_of(action.do)},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        timeout = float(action.do.get("timeout", 5.0))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                return {"url": url, "status": status}
        except urllib.error.HTTPError as exc:
            raise ActionError(f"webhook 返回 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ActionError(f"webhook 连不上：{exc.reason}") from exc

    def _exec(self, action: ActionCfg) -> dict[str, Any]:
        cmd = action.do.get("exec")
        if not cmd:
            raise ActionError("exec 命令是空的")
        cwd = self._base_dir or None
        kwargs: dict[str, Any] = {"cwd": cwd, "close_fds": True}
        if os.name == "nt":
            # 不弹控制台窗口，也不跟着 pixelbus 一起死
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if isinstance(cmd, str):
            proc = subprocess.Popen(cmd, shell=True, **kwargs)
        else:
            proc = subprocess.Popen([str(c) for c in cmd], **kwargs)
        return {"pid": proc.pid, "cmd": list(cmd) if isinstance(cmd, list) else cmd}

    def _mqtt_publish(self, action: ActionCfg, event: Any) -> dict[str, Any]:
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ImportError as exc:
            raise ActionError("需要 paho-mqtt：pip install pixelbus[mqtt]") from exc

        cfg = action.do.get("mqtt")
        if isinstance(cfg, str):
            topic, opts = cfg, {}
        elif isinstance(cfg, Mapping):
            topic = str(cfg.get("topic", ""))
            opts = dict(cfg)
        else:
            raise ActionError("mqtt 应该是 topic 字符串或字典")
        if not topic:
            raise ActionError("mqtt 缺少 topic")
        payload = str(opts.get("payload") or action.text or json.dumps(event.to_dict(), ensure_ascii=False))
        host = str(opts.get("host", "127.0.0.1"))
        port = int(opts.get("port", 1883))
        qos = int(opts.get("qos", 0))

        client = self._mqtt.get("client")
        if client is None or self._mqtt.get("host") != host or self._mqtt.get("port") != port:
            if client is not None:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
            try:
                client = mqtt.Client()
                client.connect(host, port, keepalive=60)
                client.loop_start()
            except Exception as exc:
                self._mqtt["error"] = str(exc)
                raise ActionError(f"mqtt 连不上 {host}:{port}：{exc}") from exc
            self._mqtt = {"client": client, "host": host, "port": port, "error": ""}
        info = client.publish(topic, payload, qos=qos)
        return {"topic": topic, "qos": qos, "mid": getattr(info, "mid", None)}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


@dataclass
class _Request:
    action: ActionCfg
    event: Any


def _parse_keys(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    else:
        raise ActionError("press 应该是字符串或按键列表")
    return parts


def press_keys(raw: Any) -> list[str]:
    """独立按键入口（MCP 的 press 工具也复用它）。返回实际按下的按键名列表。"""
    try:
        from pynput.keyboard import Controller, Key
    except ImportError as exc:
        raise ActionError("需要 pynput：pip install pixelbus[keys]") from exc

    keys = _parse_keys(raw)
    if not keys:
        raise ActionError("press 没有按键")

    kb = Controller()
    mods = [k for k in keys if k in ("ctrl", "alt", "shift", "cmd")]
    chars = [k for k in keys if k not in mods]
    try:
        for m in mods:
            kb.press(_key_obj(Key, m))
        for ch in chars:
            _tap(kb, Key, ch)
    finally:
        for m in reversed(mods):
            kb.release(_key_obj(Key, m))
    return list(keys)


def _key_obj(Key: Any, name: str) -> Any:
    mapped = _HOT_KEYS.get(name)
    if mapped:
        return getattr(Key, mapped)
    if name.startswith("f") and name[1:].isdigit():
        return getattr(Key, name, name)
    if len(name) == 1:
        return name
    return getattr(Key, name, name)


def _tap(kb: Any, Key: Any, name: str) -> None:
    obj = _key_obj(Key, name)
    kb.press(obj)
    kb.release(obj)
