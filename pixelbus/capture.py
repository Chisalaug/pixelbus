"""抓屏与区域解析。

这一层只做三件事：
1. 把 YAML 里写的 ``{monitor: primary, rect: [0.72, 0.08, 0.26, 0.10]}`` 变成真实像素矩形；
2. 处理 Windows 的 DPI 缩放、多屏、负坐标（副屏在主屏左边时 left 是负数）；
3. 用 mss 抓一块图，返回 BGRA 的 numpy 数组。

所有坐标对外的单位都是「像素」，不对内暴露 mss 的 dict。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

try:  # mss 是硬依赖，但允许在没有它时 import 本模块（跑单测用）
    import mss as _mss
except Exception:  # pragma: no cover - 只有缺依赖时触发
    _mss = None


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

_DPI_STATE: str | None = None


def enable_dpi_awareness() -> str:
    """让进程以物理像素工作，否则 125% 缩放下 mss 抓到的是被系统放大过的图。

    返回最终生效的级别，方便日志里核对：
    ``per-monitor-v2`` / ``per-monitor`` / ``system`` / ``none`` / ``n/a``。
    """
    global _DPI_STATE
    if _DPI_STATE is not None:
        return _DPI_STATE
    if sys.platform != "win32":
        _DPI_STATE = "n/a"
        return _DPI_STATE

    try:  # Windows 10 1703+：进程级 per-monitor v2，最彻底
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            _DPI_STATE = "per-monitor-v2"
            return _DPI_STATE
    except Exception:
        pass

    try:  # Windows 8.1+
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            _DPI_STATE = "per-monitor"
            return _DPI_STATE
    except Exception:
        pass

    try:  # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
        _DPI_STATE = "system"
    except Exception:
        _DPI_STATE = "none"
    return _DPI_STATE


# --------------------------------------------------------------------------
# 区域定义
# --------------------------------------------------------------------------


class RegionError(ValueError):
    """区域写错了。带 hint 是为了让人一眼知道怎么改。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message if not hint else f"{message}（{hint}）")
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class RegionSpec:
    """一个命名区域。rect 是相对坐标，pixels 是相对该显示器的绝对像素。"""

    name: str
    monitor: str | int = "primary"
    rect: tuple[float, float, float, float] | None = None
    pixels: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.rect is not None and self.pixels is not None:
            raise RegionError(
                f"区域 {self.name!r} 同时写了 rect 和 pixels",
                "二选一：rect 用 0~1 的相对坐标，pixels 用绝对像素",
            )
        if self.rect is None and self.pixels is None:
            raise RegionError(
                f"区域 {self.name!r} 没有 rect 或 pixels",
                '例如 rect: [0.72, 0.08, 0.26, 0.10]，四个数依次是 left, top, width, height',
            )
        box = self.rect if self.rect is not None else self.pixels
        if box[2] <= 0 or box[3] <= 0:  # type: ignore[index]
            raise RegionError(
                f"区域 {self.name!r} 算出来是空矩形（宽 {box[2]}, 高 {box[3]}）",  # type: ignore[index]
                "width/height 必须大于 0；太小的区域也判定不出东西",
            )


def parse_region(name: str, raw: Any) -> RegionSpec:
    """把 YAML 里的一个 region 字典变成 RegionSpec，报错要能指到具体字段。"""
    if raw is None:
        raise RegionError(f"区域 {name!r} 是空的", "至少要有 monitor + rect")
    if not isinstance(raw, Mapping):
        raise RegionError(
            f"区域 {name!r} 应该是字典，实际是 {type(raw).__name__}",
            '写成 alarm: {monitor: primary, rect: [0.72, 0.08, 0.26, 0.10]}',
        )

    monitor = raw.get("monitor", "primary")
    rect = raw.get("rect")
    pixels = raw.get("pixels")

    if rect is not None:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise RegionError(
                f"区域 {name!r} 的 rect 必须是 4 个数，实际是 {rect!r}",
                '顺序是 [left, top, width, height]，比如 [0.72, 0.08, 0.26, 0.10]',
            )
        try:
            rect = tuple(float(v) for v in rect)
        except (TypeError, ValueError):
            raise RegionError(f"区域 {name!r} 的 rect 里有非数字：{rect!r}") from None
        if not all(-1.0 <= v <= 2.0 for v in rect):
            raise RegionError(
                f"区域 {name!r} 的 rect 超出相对坐标系：{rect}",
                "rect 是相对坐标，四个值应该在 0~1 之间",
            )

    if pixels is not None:
        if not isinstance(pixels, (list, tuple)) or len(pixels) != 4:
            raise RegionError(
                f"区域 {name!r} 的 pixels 必须是 4 个整数，实际是 {pixels!r}",
                "顺序是 [left, top, width, height]，单位是显示器内的绝对像素",
            )
        try:
            pixels = tuple(int(v) for v in pixels)
        except (TypeError, ValueError):
            raise RegionError(f"区域 {name!r} 的 pixels 里有非整数：{pixels!r}") from None

    return RegionSpec(name=name, monitor=monitor, rect=rect, pixels=pixels)


def resolve_monitor(spec: str | int, monitors: Sequence[Mapping[str, Any]]) -> dict:
    """把 monitor 说明解析成一个 mss 显示器 dict。

    支持：``primary`` / ``all``（虚拟全屏拼接）/ 整数索引（mss 约定，0 是全部）。
    """
    if not monitors:
        raise RegionError("拿不到任何显示器信息", "mss 初始化失败？先跑 pixelbus doctor")

    if isinstance(spec, str):
        low = spec.strip().lower()
        if low in ("primary", "auto", ""):
            for mon in monitors[1:]:
                if mon.get("is_primary"):
                    return dict(mon)
            # 有些平台的 monitors[1] 就是主屏；再退一步找最小的那块（通常是笔记本屏）
            if len(monitors) > 1:
                return dict(monitors[1])
            return dict(monitors[0])
        if low in ("all", "virtual", "0"):
            return dict(monitors[0])
        if low.isdigit():
            spec = int(low)
        else:
            raise RegionError(
                f"monitor={spec!r} 无法识别",
                '用 "primary"（主屏）、"all"（全部屏幕）或整数索引 1/2/3（mss 的编号）',
            )

    idx = int(spec)
    if idx < 0 or idx >= len(monitors):
        raise RegionError(
            f"monitor={idx} 超出范围，这台机器只有 {len(monitors)} 个（含索引 0 的全屏拼接）",
            "用 pixelbus doctor 看可用的显示器编号",
        )
    return dict(monitors[idx])


def rect_to_bbox(spec: RegionSpec, monitor: Mapping[str, Any]) -> dict[str, int]:
    """算这块区域在屏幕上的真实像素矩形，越界就裁掉。"""
    mw = int(monitor["width"])
    mh = int(monitor["height"])
    mx = int(monitor["left"])
    my = int(monitor["top"])

    if spec.pixels is not None:
        left, top, width, height = spec.pixels
    else:
        rl, rt, rw, rh = spec.rect  # type: ignore[misc]
        left = round(mw * rl)
        top = round(mh * rt)
        width = round(mw * rw)
        height = round(mh * rh)

    if width <= 0 or height <= 0:
        raise RegionError(
            f"区域 {spec.name!r} 算出来是空矩形（{width}x{height}）",
            "rect 的 width/height 必须大于 0，太小的区域也判定不出东西",
        )

    # 裁进显示器范围内（相对坐标写成 1.05 这种轻微越界是常见的手滑）
    x0 = max(0, min(left, mw - 1))
    y0 = max(0, min(top, mh - 1))
    x1 = max(x0 + 1, min(left + width, mw))
    y1 = max(y0 + 1, min(top + height, mh))

    return {
        "left": mx + x0,
        "top": my + y0,
        "width": x1 - x0,
        "height": y1 - y0,
    }


# --------------------------------------------------------------------------
# 抓屏
# --------------------------------------------------------------------------


@dataclass
class GrabStats:
    frames: int = 0
    grab_seconds: float = 0.0
    last_bbox: dict[str, int] = field(default_factory=dict)


# -- 分层窗口检测 ------------------------------------------------------------
#
# mss 抓屏固定用 ``SRCCOPY | CAPTUREBLT``。CAPTUREBLT 是为了把 *分层窗口*
# （WS_EX_LAYERED，比如带透明度的浮层、某些提示框/输入法候选窗）也拍进来，
# 代价是**每次 BitBlt 贵 5~6 倍**：实测 499x108 的小区域 0.47ms → 0.08ms。
#
# 所以这里按"这个 region 到底有没有被分层窗口盖住"来决定要不要加 CAPTUREBLT：
# 没被盖住就走快路径，被盖住就退回兼容路径 —— 既快又不丢内容。
#
# 枚举窗口大约 0.3ms，所以结果缓存 0.5 秒，不是每帧都枚举。

WS_EX_LAYERED = 0x00080000
_LAYERED_TTL = 0.5
_layered_cache: tuple[float, list[tuple[int, int, int, int]]] = (0.0, [])


def _enum_layered_windows() -> list[tuple[int, int, int, int]]:
    """返回所有可见分层窗口的屏幕矩形 (left, top, right, bottom)。"""
    if sys.platform != "win32":
        return []
    import time as _time

    rects: list[tuple[int, int, int, int]] = []
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if not (ex & WS_EX_LAYERED):
                return True
            # 全局 alpha = 0 的分层窗口是"完全透明、点击穿透"的那种浮层（输入法穿透层、
            # 录屏遮罩的空壳），它根本不会出现在画面上，没必要为它降级到兼容路径。
            colorref = ctypes.c_ulong(0)
            alpha = ctypes.c_ubyte(255)
            flags = ctypes.c_ulong(0)
            if user32.GetLayeredWindowAttributes(
                hwnd, ctypes.byref(colorref), ctypes.byref(alpha), ctypes.byref(flags)
            ):
                if (flags.value & 0x00000002) and alpha.value == 0:  # LWA_ALPHA
                    return True
            rc = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                if rc.right - rc.left > 4 and rc.bottom - rc.top > 4:
                    rects.append((rc.left, rc.top, rc.right, rc.bottom))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, None)
    except Exception:
        return []
    _ = _time  # 保持 import 一致（下面用 time）
    return rects


def layered_rects(force: bool = False) -> list[tuple[int, int, int, int]]:
    """带 TTL 缓存的分层窗口矩形表。"""
    import time as _time

    global _layered_cache
    now = _time.perf_counter()
    if not force and now - _layered_cache[0] < _LAYERED_TTL:
        return _layered_cache[1]
    rects = _enum_layered_windows()
    _layered_cache = (now, rects)
    return rects


def overlaps_layered(bbox: Mapping[str, int]) -> bool:
    left = int(bbox["left"])
    top = int(bbox["top"])
    right = left + int(bbox["width"])
    bottom = top + int(bbox["height"])
    for l, t, r, b in layered_rects():
        if l < right and r > left and t < bottom and b > top:
            return True
    return False


def screen_has_hidden_layers(grab, monitors: Sequence[Mapping[str, int]]) -> bool | None:
    """实测：关掉 CAPTUREBLT 会不会少拍到东西。

    三次全屏抓图：fast → compat → fast。
    - 第 1、3 张不一样说明屏幕自己在动，这次测不准，返回 None；
    - 一样就说明画面静止，此时只要 compat 与 fast 不同，就是真有分层窗口内容被漏掉。

    只在启动和每隔几十秒做一次，成本可以忽略（三次全屏约 13ms）。
    """
    if not monitors:
        return None
    mon = dict(monitors[0])
    a = grab(mon)
    b = grab(mon, compat=True)
    c = grab(mon)
    if not np.array_equal(a, c):
        return None
    return bool(np.array_equal(a, b))


class Grabber:
    """mss 的薄封装。一次进程只开一个，抓图结果固定是 BGRA。"""

    def __init__(self) -> None:
        if _mss is None:
            raise RuntimeError(
                '缺少 mss，先装：pip install "pixelbus"（或 pip install mss）'
            )
        self.dpi = enable_dpi_awareness()
        cls = getattr(_mss, "MSS", None)  # mss>=10 推荐 MSS()，老版本只有 mss()
        self._sct = cls() if cls is not None else _mss.mss()  # type: ignore[attr-defined]
        # 找 mss 内部拼 flag 用的模块级 CAPTUREBLT；找不到就永远走兼容路径
        self._blt_mod: Any | None = None
        self._blt_saved = 0
        self.fast_grabs = 0
        self.compat_grabs = 0
        self._pin_compat = bool(os.environ.get("PIXELBUS_CAPTURE_COMPAT"))
        self._fast_ok = True
        self._probe_at = 0.0
        self._probe_interval = 30.0
        self._probe_backoff = 5.0
        self.layer_note = "未检测"
        if sys.platform == "win32":
            try:
                import mss.windows.gdi as _gdi

                if hasattr(_gdi, "CAPTUREBLT"):
                    self._blt_mod = _gdi
                    self._blt_saved = _gdi.CAPTUREBLT
            except Exception:  # pragma: no cover - 只在 mss 改了内部结构时
                self._blt_mod = None
        if self._blt_mod is not None and not self._pin_compat:
            # 在构造函数里就先测一次：别把 3 次全屏抓图的开销塞进第一帧，
            # 否则引擎一启动就会丢帧（实测会丢掉 2 帧）。
            self._probe_fast_path()

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    def __enter__(self) -> "Grabber":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 显示器 -----------------------------------------------------------
    @property
    def monitors(self) -> list[dict]:
        return [dict(m) for m in self._sct.monitors]

    def monitor(self, spec: str | int) -> dict:
        return resolve_monitor(spec, self.monitors)

    def describe(self) -> list[dict[str, Any]]:
        """给 doctor / probe 用的显示器清单。"""
        out = []
        for i, mon in enumerate(self.monitors):
            out.append(
                {
                    "index": i,
                    "left": mon.get("left"),
                    "top": mon.get("top"),
                    "width": mon.get("width"),
                    "height": mon.get("height"),
                    "primary": bool(mon.get("is_primary")),
                }
            )
        return out

    # -- 快路径自检 -------------------------------------------------------
    def _due_probe(self) -> bool:
        import time as _time

        return _time.perf_counter() >= self._probe_at

    def _probe_fast_path(self) -> None:
        import time as _time

        self._probe_at = _time.perf_counter() + self._probe_interval
        try:
            verdict = screen_has_hidden_layers(self._grab, self.monitors[1:])
        except Exception:
            verdict = None
        if verdict is None:
            # 桌面在动（视频、闪烁光标、动画），这一轮测不出来：退避重试，
            # 不然每 5 秒做 3 次全屏抓图，反而拖慢主循环、丢帧。
            self._probe_backoff = min(60.0, max(5.0, self._probe_backoff * 2))
            self.layer_note = f"画面在动，暂时测不准（保持当前模式，{self._probe_backoff:.0f}s 后重试）"
            self._probe_at = _time.perf_counter() + self._probe_backoff
            return
        if verdict:
            self._fast_ok = True
            self._probe_backoff = 5.0
            self.layer_note = "快路径安全（关 CAPTUREBLT 不影响画面）"
        else:
            if self._fast_ok:
                self.layer_note = "检测到分层窗口，已退回兼容模式（慢 5~6 倍）"
            self._fast_ok = False
            self._probe_backoff = 30.0

    # -- 抓图 -------------------------------------------------------------
    def grab_bbox(self, bbox: Mapping[str, int]) -> np.ndarray:
        """抓一块矩形，返回 (h, w, 4) 的 BGRA uint8。

        默认走"不带 CAPTUREBLT"的快路径（小区域快 5~6 倍）；但只要实测发现
        关掉 CAPTUREBLT 会少拍到东西（分层窗口），就永久退回兼容路径。
        见 :func:`screen_has_hidden_layers`。
        """
        region = {
            "left": int(bbox["left"]),
            "top": int(bbox["top"]),
            "width": int(bbox["width"]),
            "height": int(bbox["height"]),
        }
        return self._grab(region)

    def _grab(self, region: Mapping[str, int], compat: bool = False) -> np.ndarray:
        mod = self._blt_mod
        fast = mod is not None and not compat and not self._pin_compat and self._fast_ok
        if mod is not None and not compat and not self._pin_compat and self._due_probe():
            self._probe_fast_path()
            fast = self._fast_ok
        if fast:
            mod.CAPTUREBLT = 0
        try:
            shot = self._sct.grab(dict(region))
        finally:
            if fast:
                mod.CAPTUREBLT = self._blt_saved
        if fast:
            self.fast_grabs += 1
        else:
            self.compat_grabs += 1
        arr = np.asarray(shot)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return arr
        # 极少数平台给 RGBX，补一下 alpha 免得下游到处判断
        if arr.ndim == 3 and arr.shape[2] == 3:
            return np.dstack([arr, np.full(arr.shape[:2], 255, np.uint8)])
        raise RuntimeError(f"mss 返回了意外的形状：{arr.shape}")

    def grab(self, spec: RegionSpec) -> np.ndarray:
        mon = self.monitor(spec.monitor)
        return self.grab_bbox(rect_to_bbox(spec, mon))

    def grab_monitor(self, spec: str | int) -> np.ndarray:
        mon = self.monitor(spec)
        return self.grab_bbox(mon)
