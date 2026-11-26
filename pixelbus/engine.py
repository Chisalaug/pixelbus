"""引擎：按 fps 抓屏 → 求值 → 边沿检测 → 去抖 → 出事件。

三条设计原则：

1. **一个区域一帧只抓一次。** 多条 signal 共用同一个 region 时不会重复截图，
   这是 15fps 下 CPU 能压到 5% 以内的主要原因。
2. **判据对象是有状态的**（``diff`` 要记住上一帧），所以热重载要重建整套，
   不能用旧对象凑合。
3. **落后就丢帧，不追。** 一次 tick 超时了直接从"现在"重新排下一次，
   否则屏幕一卡就会攒出一串补帧，CPU 打满、时间戳还全乱。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from . import __version__
from .actions import ActionRunner
from .capture import Grabber, rect_to_bbox
from .predicates import Frame, Judge, TestResult
from .rules import RuleSet, WhenExpr

__all__ = ["Event", "Engine", "Snapshot"]


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------


@dataclass
class Event:
    """一条事件。``target`` 是 signal / state，前端靠它区分"原始判据"和"组合态"。"""

    kind: str  # appear / disappear / change / value
    name: str
    target: str
    t: float
    level: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "target": self.target,
            "t": round(self.t, 4),
            "level": round(self.level, 4),
            "detail": self.detail,
        }

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.t))

    def __str__(self) -> str:
        icon = {"appear": "▲", "disappear": "▼", "change": "~", "value": "="}.get(self.kind, "?")
        return f"{self.clock()} {icon} {self.kind:<9} {self.target}:{self.name}  lvl={self.level:.2f}"


@dataclass
class _SignalState:
    hit: bool = False
    level: float = 0.0
    streak: int = 0
    judges: list[Judge] = field(default_factory=list)
    fingerprint: tuple | None = None
    since: float = 0.0
    last_event: float = -1e9
    value_at: float = -1e9
    error: str = ""


@dataclass
class _StateState:
    active: bool = False
    streak: int = 0
    miss: int = 0
    since: float = 0.0
    last_event: float = -1e9


@dataclass
class Snapshot:
    """``GET /state`` 的返回体，也是 probe / MCP 的数据源。"""

    app: str = "pixelbus"
    version: str = __version__
    fps: float = 10.0
    rules: str = ""
    t: float = 0.0
    frames: int = 0
    dropped: int = 0
    capture_dpi: str = ""
    size: tuple[int, int] = (0, 0)
    signals: dict[str, dict] = field(default_factory=dict)
    states: dict[str, dict] = field(default_factory=dict)
    regions: dict[str, dict] = field(default_factory=dict)
    last_event: dict | None = None
    actions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "version": self.version,
            "fps": self.fps,
            "rules": self.rules,
            "t": round(self.t, 4),
            "frames": self.frames,
            "dropped": self.dropped,
            "capture_dpi": self.capture_dpi,
            "signals": self.signals,
            "states": self.states,
            "regions": self.regions,
            "last_event": self.last_event,
            "actions": self.actions,
        }


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


class Engine:
    def __init__(
        self,
        rules: RuleSet,
        grabber: Any | None = None,
        on_event: Callable[[Event], None] | None = None,
        runner: ActionRunner | None = None,
    ) -> None:
        self._rules = rules
        self._grabber = grabber
        self._owns_grabber = grabber is None
        self._on_event = on_event
        self._subscribers: list[Callable[[Event], None]] = []
        base_dir = os.path.dirname(os.path.abspath(rules.path)) if rules.path else None
        self._runner = runner if runner is not None else ActionRunner(rules, base_dir=base_dir)
        self._tests: dict[str, Any] = {}
        self._bboxes: dict[str, dict[str, int]] = {}
        self._signals: dict[str, _SignalState] = {}
        self._states: dict[str, _StateState] = {}
        self._frame_no = 0
        self._dropped = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_event: Event | None = None
        self._recent: deque[Event] = deque(maxlen=500)
        self.grab_errors = 0
        self._size = (0, 0)
        self._dpi = ""
        self._region_errors: dict[str, str] = {}
        self._region_cache: dict[str, tuple[np.ndarray, dict[str, TestResult]]] = {}
        self._reuse = 0
        self._build(rules)

    # -- 构建 -------------------------------------------------------------
    def _build(self, rules: RuleSet) -> None:
        base_dir = os.path.dirname(os.path.abspath(rules.path)) if rules.path else None
        self._tests = {name: cfg.build(base_dir=base_dir) for name, cfg in rules.signals.items()}
        # 只抓"被 signal 用到的"区域：定义了一堆没用的区域不会白白消耗 CPU
        self._used_regions = sorted({cfg.region for cfg in rules.signals.values() if cfg.region})
        self._signals_by_region: dict[str, list[str]] = {}
        for name, cfg in rules.signals.items():
            if cfg.region:
                self._signals_by_region.setdefault(cfg.region, []).append(name)
        # 区域内容没变时可以直接复用上一帧的判据结果（见 step）
        self._region_cache = {}
        self._signals = {name: _SignalState() for name in rules.signals}
        self._states = {name: _StateState() for name in rules.states}
        self._bboxes = {}
        self._region_errors = {}
        self._size = (0, 0)
        self._resolve_bboxes()

    def _resolve_bboxes(self) -> None:
        """把区域解析成真实像素矩形。显示器分辨率变了就在这里重算。"""
        if self._grabber is None:
            try:
                self._grabber = Grabber()
            except Exception:
                return
        try:
            monitors = self._grabber.monitors
        except Exception:
            return
        self._region_errors = {}
        for name, region in self._rules.regions.items():
            try:
                mon = self._grabber.monitor(region.spec.monitor)
                self._bboxes[name] = rect_to_bbox(region.spec, mon)
            except Exception as exc:  # 显示器拔掉了 / 索引越界
                self._bboxes.pop(name, None)
                self._region_errors[name] = str(exc)
        if len(monitors) > 1:
            self._size = (int(monitors[1]["width"]), int(monitors[1]["height"]))
        self._dpi = getattr(self._grabber, "dpi", "")

    # -- 生命周期 ---------------------------------------------------------
    @property
    def rules(self) -> RuleSet:
        return self._rules

    @property
    def runner(self) -> ActionRunner | None:
        return self._runner

    @property
    def dpi(self) -> str:
        return self._dpi

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pixelbus-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=2.0)

    def join(self, timeout: float | None = None) -> None:
        """等抓屏线程收尾。``timeout=None`` 等于一直等（正常退出时几乎立刻返回）。"""
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        interval = 1.0 / max(0.2, self._rules.fps)
        next_at = time.perf_counter()
        while not self._stop.is_set():
            self.step()
            next_at += interval
            slack = next_at - time.perf_counter()
            if slack > 0:
                self._stop.wait(slack)
            else:
                # 已经落后：丢掉积压的帧，从现在重新排
                self._dropped += max(1, int(round(-slack / interval)))
                next_at = time.perf_counter()

    # -- 单步 -------------------------------------------------------------
    def step(self, t: float | None = None, frames: Mapping[str, np.ndarray] | None = None) -> list[Event]:
        """跑一帧。``frames`` 只在测试里用（直接喂图，不碰真实屏幕）。"""
        now = time.time() if t is None else float(t)
        events: list[Event] = []
        self._frame_no += 1

        region_frames: dict[str, np.ndarray | None] = {}
        for rname in self._used_regions:
            region = self._rules.regions.get(rname)
            if region is None:
                continue
            if frames is not None and rname in frames:
                region_frames[rname] = frames[rname]
                continue
            region_frames[rname] = self._grab(rname, region)

        if self._size == (0, 0):
            for arr in region_frames.values():
                if arr is not None:
                    self._size = (int(arr.shape[1]), int(arr.shape[0]))
                    break

        # 1) 判据求值：按区域走，整帧没变就复用上一帧的结果
        results: dict[str, TestResult] = {}
        for rname in self._used_regions:
            arr = region_frames.get(rname)
            if arr is None:
                continue
            cached = self._region_cache.get(rname)
            if cached is not None and cached[0].shape == arr.shape and np.array_equal(cached[0], arr):
                self._reuse += 1
                results.update(cached[1])
                continue
            got: dict[str, TestResult] = {}
            for name in self._signals_by_region.get(rname, ()):
                frame = Frame(arr, order="bgra", region=rname, seq=self._frame_no, t=now)
                got[name] = self._tests[name].measure(frame)
            # 真抓屏拿到的数组是 mss 每次新分配的，不会被原地改，直接存引用；
            # 测试里传进来的图有可能被就地修改，才需要复制一份。
            self._region_cache[rname] = (arr if frames is None else arr.copy(), got)
            results.update(got)

        # 2) 边沿检测：按 signal 声明顺序，保证事件顺序可预期
        for name, cfg in self._rules.signals.items():
            st = self._signals[name]
            result = results.get(name)
            if result is None:
                st.error = st.error or "抓不到这个区域（显示器变了？跑 pixelbus doctor）"
                continue
            st.error = ""
            prev_hit, prev_fp = st.hit, st.fingerprint
            st.hit = result.hit
            st.level = result.level
            st.judges = result.judges
            st.streak = st.streak + 1 if result.hit else 0
            if result.hit:
                if st.since == 0.0 or prev_hit is False:
                    st.since = now
            fp = _fingerprint(result.judges)
            st.fingerprint = fp

            bbox = self._bboxes.get(cfg.region)
            if result.hit and not prev_hit:
                if now - st.last_event >= cfg.cooldown:
                    st.last_event = now
                    events.append(Event("appear", name, "signal", now, result.level, _signal_detail(result, bbox)))
            elif prev_hit and not result.hit:
                st.last_event = now
                events.append(Event("disappear", name, "signal", now, result.level, _signal_detail(result, bbox)))
            elif result.hit and prev_hit and prev_fp is not None and fp != prev_fp:
                events.append(Event("change", name, "signal", now, result.level, _signal_detail(result, bbox)))

            if cfg.values and result.hit and now - st.value_at >= 1.0:
                if not any(e.name == name and e.kind in ("appear", "change") for e in events):
                    st.value_at = now
                    events.append(Event("value", name, "signal", now, result.level, _signal_detail(result, bbox)))

        # 2) 组合态（按声明顺序，所以 when 可以引用前面已算好的 state）
        hits: dict[str, bool] = {n: s.hit for n, s in self._signals.items()}
        for name, cfg in self._rules.states.items():
            st = self._states[name]
            raw = _eval_when(cfg.when, hits, {n: s.active for n, s in self._states.items()})
            need = max(1, cfg.stable_frames)
            if raw:
                st.streak += 1
                st.miss = 0
                if not st.active and st.streak >= need:
                    st.active = True
                    st.since = now
                    events.append(Event("appear", name, "state", now, 1.0, {"stable_frames": need, "streak": st.streak}))
            else:
                st.streak = 0
                st.miss += 1
                if st.active and st.miss >= need:
                    st.active = False
                    events.append(
                        Event("disappear", name, "state", now, 0.0, {"stable_frames": need, "held_for": round(now - st.since, 2)})
                    )
            hits[name] = st.active  # 写回帧内最新值，后面的 state 引用它才有一致性

        for event in events:
            self._emit(event)
            if self._runner is not None:
                self._runner.handle(event)
        return events

    def _grab(self, name: str, region: Any) -> np.ndarray | None:
        if self._grabber is None:
            try:
                self._grabber = Grabber()
            except Exception as exc:
                self.grab_errors += 1
                return None
        bbox = self._bboxes.get(name)
        if bbox is None:
            self._resolve_bboxes()
            bbox = self._bboxes.get(name)
        if bbox is None:
            return None
        try:
            return self._grabber.grab_bbox(bbox)
        except Exception:
            # 分辨率变了 / 副屏拔了：重算一次再试
            self.grab_errors += 1
            self._resolve_bboxes()
            bbox = self._bboxes.get(name)
            if bbox is None:
                return None
            try:
                return self._grabber.grab_bbox(bbox)
            except Exception:
                return None

    def _emit(self, event: Event) -> None:
        self._last_event = event
        self._recent.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass
        for sub in list(self._subscribers):
            try:
                sub(event)
            except Exception:
                self._subscribers.remove(sub)

    # -- 订阅 / 热重载 ----------------------------------------------------
    def add_subscriber(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def set_rules(self, rules: RuleSet) -> None:
        """热重载：整包重建判据（diff 的基线也会重置，这是刻意的）。"""
        with self._lock:
            self._rules = rules
            self._build(rules)
            if self._runner is not None:
                self._runner.configure(rules)

    def reload(self) -> RuleSet:
        from .rules import load_rules

        rules = load_rules(self._rules.path)
        self.set_rules(rules)
        return rules

    # -- 查询 -------------------------------------------------------------
    def snapshot(self, t: float | None = None) -> Snapshot:
        now = time.time() if t is None else float(t)
        signals = {}
        for name, st in self._signals.items():
            cfg = self._rules.signals.get(name)
            region = cfg.region if cfg else ""
            signals[name] = {
                "hit": st.hit,
                "level": round(st.level, 4),
                "streak": st.streak,
                "since": round(st.since, 3),
                "region": region,
                "bbox": self._bboxes.get(region),
                "judges": [
                    {"name": j.predicate, "value": round(j.value, 4), "passed": j.passed, "head": j.head, "detail": j.detail}
                    for j in st.judges
                ],
                "error": st.error or self._region_errors.get(region, ""),
            }
        states = {}
        for name, st in self._states.items():
            cfg = self._rules.states.get(name)
            states[name] = {
                "active": st.active,
                "streak": st.streak,
                "stable_frames": cfg.stable_frames if cfg else 1,
                "since": round(st.since, 3),
                "when": str(cfg.when) if cfg else "",
            }
        regions = {
            name: {"bbox": bbox, "monitor": self._rules.regions[name].spec.monitor}
            for name, bbox in self._bboxes.items()
        }
        action_status = self._runner.status() if self._runner is not None else {}
        return Snapshot(
            fps=self._rules.fps,
            rules=os.path.basename(self._rules.path) if self._rules.path else "",
            t=now,
            frames=self._frame_no,
            dropped=self._dropped,
            capture_dpi=self._dpi,
            size=self._size,
            signals=signals,
            states=states,
            regions=regions,
            last_event=self._last_event.to_dict() if self._last_event else None,
            actions=action_status,
        )

    def state_of(self, name: str) -> bool | None:
        """给 MCP / wait_for 用：先查 state，再查 signal。"""
        with self._lock:
            if name in self._states:
                return self._states[name].active
            if name in self._signals:
                return self._signals[name].hit
        return None

    def close(self) -> None:
        self.stop()
        if self._runner is not None:
            self._runner.shutdown()
        if self._owns_grabber and self._grabber is not None:
            try:
                self._grabber.close()
            except Exception:
                pass
            self._grabber = None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _fingerprint(judges: list[Judge]) -> tuple:
    """粗粒度指纹：用来判断"命中的东西变没变"，避免浮点抖动天天发 change。"""
    parts: list[Any] = []
    for j in judges:
        text = j.detail.get("text")
        if text:
            parts.append(("txt", str(text)))
        else:
            parts.append((j.predicate, round(j.value, 2)))
    return tuple(parts)


def _signal_detail(result: TestResult, regions_bbox: dict[str, int] | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "judges": [
            {"name": j.predicate, "value": round(j.value, 4), "passed": j.passed, "head": j.head}
            for j in result.judges
        ]
    }
    if result.failed:
        detail["failed"] = list(result.failed)
    for j in result.judges:
        if j.detail.get("text"):
            detail["text"] = j.detail["text"]
            break
    if regions_bbox:
        detail["bbox"] = regions_bbox
    return detail


def _eval_when(expr: WhenExpr, hits: Mapping[str, bool], states: Mapping[str, bool]) -> bool:
    if expr.op == "sig":
        if expr.name in hits:
            return bool(hits[expr.name])
        return bool(states.get(expr.name, False))
    if expr.op == "not":
        return not _eval_when(expr.kids[0], hits, states)
    if expr.op == "and":
        return all(_eval_when(k, hits, states) for k in expr.kids)
    if expr.op == "or":
        return any(_eval_when(k, hits, states) for k in expr.kids)
    raise ValueError(f"未知的 when 节点：{expr.op}")
