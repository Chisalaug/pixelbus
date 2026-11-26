"""判据：把一块图变成一串可比较的分数。

设计上只回答一个问题：**这块区域现在"像不像"某个东西**。
每个判据返回 :class:`Judge`，里面既有原始测量值（给人看、用来调阈值），
也有 ``passed``（给引擎用）。

全部判据都是纯 numpy 像素运算，没有模板图、没有 OCR（OCR 是可选加餐）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

__all__ = [
    "Frame",
    "Judge",
    "TestResult",
    "Predicate",
    "PredicateSpecError",
    "build_test",
    "available_predicates",
]


# --------------------------------------------------------------------------
# 帧
# --------------------------------------------------------------------------


def _to_rgb(image: np.ndarray, order: str) -> np.ndarray:
    """统一到 RGB 三通道，避免红色和蓝色被悄悄换掉（这是最容易踩的坑）。"""
    arr = image
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return np.repeat(arr[..., None], 3, axis=2)
    if arr.ndim != 3:
        raise ValueError(f"图像维度不对：{arr.shape}")

    ch = arr.shape[2]
    if order == "rgb":
        return arr[..., :3] if ch >= 3 else np.repeat(arr[..., :1], 3, axis=2)
    if order == "bgra" or order == "bgr":
        if ch < 3:
            return np.repeat(arr[..., :1], 3, axis=2)
        return arr[..., 2::-1] if ch == 4 else arr[..., ::-1]
    if order == "gray":
        return np.repeat(arr[..., :1], 3, axis=2)
    raise ValueError(f"不认识的通道顺序：{order!r}，只支持 bgra / bgr / rgb / gray")


@dataclass
class Ink:
    """二值墨迹遮罩。strong 是"确定的墨"，weak 是"可能算墨"（给抗锯齿留余地）。

    ``weak`` 默认是**懒算**的：只有 glyph_runs 这种需要判断"这条缝是不是抗锯齿"的判据
    才会用到它，ink_ratio / aspect / centroid_dx 都用不上。少建一张全尺寸布尔图，
    187k 像素的区域每帧就能省 0.2~0.3ms —— 15fps 下就是 0.5% CPU。
    """

    strong: np.ndarray
    weak: np.ndarray | None
    mode: str
    threshold: int
    _from: tuple = field(default=(), repr=False)
    _denoise: int = field(default=0, repr=False)

    def weak_mask(self) -> np.ndarray:
        if self.weak is None:
            if not self._from:
                raise RuntimeError("Ink 既没有 weak，也没有可用于重建 weak 的源数据")
            kind = self._from[0]
            if kind == "lt":
                weak = self._from[1] < self._from[2]
            elif kind == "ge":
                weak = self._from[1] >= self._from[2]
            elif kind == "color":
                rgb, target, tol = self._from[1], self._from[2], self._from[3]
                dist = np.abs(rgb.astype(np.int16) - target).max(axis=2)
                weak = dist <= int(tol)
            else:  # pragma: no cover - 内部不会走到
                raise RuntimeError(f"未知的 weak 重建方式：{kind}")
            if self._denoise > 1:
                weak = _open(weak, self._denoise)
            self.weak = weak
        return self.weak

    @property
    def count(self) -> int:
        return int(self.strong.sum())


@dataclass
class Frame:
    """一帧区域图像 + 懒计算缓存。

    ``order`` 决定通道顺序：mss 抓屏是 ``bgra``，PIL 读进来是 ``rgb``。
    判据内部永远用 RGB，换算出错就会把红当蓝，所以这里显式声明、显式校验。
    """

    image: np.ndarray
    order: str = "bgra"
    region: str = ""
    seq: int = 0
    t: float = 0.0
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.order not in ("bgra", "bgr", "rgb", "gray"):
            raise ValueError(f"order 只能是 bgra / bgr / rgb / gray，收到 {self.order!r}")

    # -- 基础派生量 -------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return int(self.image.shape[0]), int(self.image.shape[1])

    def rgb(self) -> np.ndarray:
        if "rgb" not in self._cache:
            self._cache["rgb"] = _to_rgb(self.image, self.order)
        return self._cache["rgb"]

    def luma(self) -> np.ndarray:
        """整数亮度（uint8），判据内部真正用的就是这个。

        用 uint16 定点乘加再右移 8 位，比 ``astype(float32)`` 再乘三个浮点系数快一半，
        误差 < 1 个灰阶 —— 阈值判定完全够用。返回 uint8 也让后面的 bincount / 比较更快。
        """
        if "luma" not in self._cache:
            rgb = self.rgb()
            acc = rgb[..., 0].astype(np.uint16) * 77
            acc += rgb[..., 1].astype(np.uint16) * 150
            acc += rgb[..., 2].astype(np.uint16) * 29
            self._cache["luma"] = (acc >> 8).astype(np.uint8)
        return self._cache["luma"]

    def gray(self) -> np.ndarray:
        """浮点灰度（0~255）。只在需要做算术的地方用（比如 diff 的绝对差）。"""
        if "gray" not in self._cache:
            self._cache["gray"] = self.luma().astype(np.float32)
        return self._cache["gray"]

    def ink(
        self,
        mode: str = "auto",
        threshold: float | None = None,
        color: str | Sequence[int] | None = None,
        tol: int = 40,
        denoise: int = 0,
    ) -> Ink:
        key = (
            "ink",
            mode,
            None if threshold is None else float(threshold),
            _color_key(color),
            int(tol),
            int(denoise),
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        ink = _build_ink(self.luma(), self.rgb(), mode, threshold, color, tol)
        if denoise and denoise > 1:
            # strong 现在就抹一遍；weak 等真要用了再抹（见 Ink.weak_mask）
            ink = Ink(
                strong=_open(ink.strong, int(denoise)),
                weak=None,
                mode=ink.mode,
                threshold=ink.threshold,
                _from=ink._from,
                _denoise=int(denoise),
            )
        self._cache[key] = ink
        return ink

    @classmethod
    def from_pil(cls, im: Any, region: str = "", seq: int = 0, t: float = 0.0) -> "Frame":
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
        return cls(image=arr, order="rgb", region=region, seq=seq, t=t)


# --------------------------------------------------------------------------
# 颜色
# --------------------------------------------------------------------------


def _color_key(color: Any) -> Any:
    if color is None:
        return None
    if isinstance(color, str):
        return color.lower()
    return tuple(int(v) for v in color)


def parse_color(color: str | Sequence[int]) -> tuple[int, int, int]:
    """``#d0021b`` / ``d0021b`` / ``(208, 2, 27)`` 都收。"""
    if isinstance(color, (list, tuple)):
        if len(color) < 3:
            raise PredicateSpecError(f"颜色 {color!r} 需要 3 个分量")
        rgb = tuple(int(v) for v in color[:3])
        if not all(0 <= v <= 255 for v in rgb):
            raise PredicateSpecError(f"颜色 {color!r} 的分量必须在 0~255")
        return rgb  # type: ignore[return-value]
    if not isinstance(color, str):
        raise PredicateSpecError(f"颜色应该写成 #rrggbb 或 [r, g, b]，收到 {color!r}")

    text = color.strip()
    if text.startswith("#"):
        text = text[1:]
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise PredicateSpecError(
            f"颜色 {color!r} 认不出来",
            '写成 "#d0021b" 这种 6 位十六进制，或者 [208, 2, 27]',
        )
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        raise PredicateSpecError(f"颜色 {color!r} 不是合法的十六进制", '例如 "#d0021b"') from None


# --------------------------------------------------------------------------
# 二值化
# --------------------------------------------------------------------------


def _otsu(gray: np.ndarray) -> int:
    """大津法阈值。图里只有"底"和"墨"的时候，比拍一个固定阈值稳得多。

    纯黑白（无灰阶过渡）的图上，类间方差在一整段区间里都是最大值 ——
    直接取 argmax 会落到 0（等于"墨"本身的灰度），所以这里取等高分段的**中点**，
    这样阈值才真正落在底和墨之间。
    """
    if gray.dtype == np.uint8:
        values = gray.ravel()  # 已经是 0~255 的字节，别再复制一遍
    else:
        values = np.clip(gray, 0, 255).astype(np.uint8).ravel()
    # 直方图不需要 20 万个样本：抽到 6.5 万个，阈值几乎不变，bincount 快 3 倍
    if values.size > 65536:
        values = values[:: max(2, values.size // 65536)]
    hist = np.bincount(values, minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 128
    omega = np.cumsum(hist) / total
    mu = np.cumsum(hist * np.arange(256)) / total
    mu_t = mu[-1]
    sigma = (mu_t * omega - mu) ** 2 / np.maximum(omega * (1.0 - omega), 1e-9)
    sigma = sigma[:255]  # t=255 把全部像素判成一类，无意义
    best = float(np.max(sigma))
    if not np.isfinite(best) or best <= 0:
        return 128
    plateau = np.flatnonzero(sigma >= best * 0.98)
    return int(plateau[len(plateau) // 2])


def _shift_mask(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    r_y, r_x = abs(dy), abs(dx)
    pad = ((r_y, r_y), (r_x, r_x))
    padded = np.pad(mask, pad, mode="constant", constant_values=False)
    h, w = mask.shape
    return padded[r_y + dy : r_y + dy + h, r_x + dx : r_x + dx + w]


def _erode(mask: np.ndarray, k: int) -> np.ndarray:
    """二值腐蚀：去掉比 k 还细的东西（1px 光标、孤立噪点）。"""
    if k <= 1:
        return mask
    r = k // 2
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy == 0 and dx == 0:
                continue
            out &= _shift_mask(mask, dy, dx)
    return out


def _dilate(mask: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return mask
    r = k // 2
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy == 0 and dx == 0:
                continue
            out |= _shift_mask(mask, dy, dx)
    return out


def _open(mask: np.ndarray, k: int) -> np.ndarray:
    """先腐蚀再膨胀 = 开运算，尺寸基本不变，只把细碎的东西抹掉。"""
    return _dilate(_erode(mask, k), k)


def _build_ink(
    gray: np.ndarray,
    rgb: np.ndarray,
    mode: str,
    threshold: float | None,
    color: str | Sequence[int] | None,
    tol: int,
) -> Ink:
    if mode == "color":
        if color is None:
            raise PredicateSpecError('ink mode=color 时必须给 color，例如 "#000000"')
        target = np.array(parse_color(color), dtype=np.int16)
        dist = np.abs(rgb.astype(np.int16) - target).max(axis=2)
        strong = dist <= int(tol)
        return Ink(
            strong=strong,
            weak=None,
            mode=mode,
            threshold=int(tol),
            _from=("color", rgb, target, int(tol) + 20),
        )

    if mode == "dark":
        thr = 110 if threshold is None else int(threshold)
        return Ink(
            strong=gray < thr, weak=None, mode=mode, threshold=thr, _from=("lt", gray, min(255, thr + 45))
        )

    if mode == "light":
        thr = 160 if threshold is None else int(threshold)
        return Ink(
            strong=gray > thr, weak=None, mode=mode, threshold=thr, _from=("ge", gray, max(0, thr - 45))
        )

    if mode != "auto":
        raise PredicateSpecError(
            f"ink 模式 {mode!r} 不存在",
            '可选："auto"（默认，自动判断浅底深字/深底浅字）、"dark"、"light"、"color"',
        )

    thr = _otsu(gray) if threshold is None else int(threshold)
    dark = gray < thr
    dark_ratio = float(dark.sum()) / max(1, gray.size)
    # 墨迹永远是少数派：底通常占大头
    if dark_ratio <= 0.5:
        strong = dark
        rule = ("lt", gray, min(255, thr + 45))
    else:
        strong = ~dark
        rule = ("ge", gray, max(0, thr - 45))
    return Ink(strong=strong, weak=None, mode="auto", threshold=thr, _from=rule)


# --------------------------------------------------------------------------
# 几何
# --------------------------------------------------------------------------


@dataclass
class Box:
    x0: int
    y0: int
    w: int
    h: int

    @property
    def x1(self) -> int:
        return self.x0 + self.w - 1

    @property
    def y1(self) -> int:
        return self.y0 + self.h - 1

    @property
    def aspect(self) -> float:
        return self.w / float(max(1, self.h))

    def as_list(self) -> list[int]:
        return [self.x0, self.y0, self.w, self.h]


def bbox_of(mask: np.ndarray, min_area: int = 1) -> Box | None:
    """墨迹外接框。把抗锯齿噪点当回事的话，先靠 min_area 把它们剔掉。"""
    if mask.size == 0:
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    if int(mask.sum()) < max(1, min_area):
        return None
    return Box(
        x0=int(cols[0]),
        y0=int(rows[0]),
        w=int(cols[-1] - cols[0] + 1),
        h=int(rows[-1] - rows[0] + 1),
    )


def centroid_offset(mask: np.ndarray, box: Box) -> float:
    """墨迹质心相对外接框中心的水平偏移，按框宽归一化。左偏为负。"""
    sub = mask[box.y0 : box.y1 + 1, box.x0 : box.x1 + 1]
    cols = sub.sum(axis=0, dtype=np.float64)
    total = cols.sum()
    if total <= 0:
        return 0.0
    cx = float((cols * np.arange(cols.size)).sum() / total)
    center = (box.w - 1) / 2.0
    return (cx - center) / float(box.w)


@dataclass
class RunInfo:
    runs: int
    runs_raw: int
    gap_max: int
    box: Box | None
    px: int


def count_runs(ink: Ink, min_area: int = 1, min_width: int = 1, gap_px: int = 0, bridge_max: int = 1) -> RunInfo:
    """数「竖着的列簇」。单字形 = 1 簇；多字文本 = 多簇。

    这是整个项目最关键的一步：反馈文字 "正确"、"未反应" 必须被数成 2、3 簇，
    而不能和单个箭头混为一谈。做法是列投影 + 两级阈值：

    - 只用 strong 遮罩定簇，噪点不会凭空多出一簇；
    - 两个簇之间如果是纯背景（weak 也没有墨），就不合并 —— 字间距保住了；
    - 如果是抗锯齿留下的一两条缝（weak 里有淡淡的墨），才合并 —— 箭头不会裂成两半。

    实测：60px 字号的「正确」两字之间是 **2 列**空隙，且其中一列有淡墨。
    所以 ``bridge_max`` 默认 1：宁可让 2 列的字间距分开（文本 → 多簇，判对），
    也不要把它合并成一个字形（那就会把"弹窗里的字"当成"单个箭头"）。
    单个字形真被撕开了，就把 ``gap_px`` / ``bridge_max`` 调大 ——
    probe 会打出 ``runs_raw`` 和 ``gap_max``，照着调。
    """
    box = bbox_of(ink.strong, min_area=min_area)
    if box is None:
        return RunInfo(runs=0, runs_raw=0, gap_max=0, box=None, px=int(ink.strong.sum()))

    rows = slice(box.y0, box.y1 + 1)
    strong_cols = ink.strong[rows, box.x0 : box.x1 + 1].any(axis=0)
    weak_cols = ink.weak_mask()[rows, box.x0 : box.x1 + 1].any(axis=0)

    segs: list[list[int]] = []
    i, n = 0, strong_cols.size
    while i < n:
        if not strong_cols[i]:
            i += 1
            continue
        j = i
        while j < n and strong_cols[j]:
            j += 1
        segs.append([i, j - 1])
        i = j

    if not segs:
        return RunInfo(runs=0, runs_raw=0, gap_max=0, box=box, px=int(ink.strong.sum()))

    merged = [list(segs[0])]
    for seg in segs[1:]:
        gap = seg[0] - merged[-1][1] - 1
        faint = bool(weak_cols[merged[-1][1] + 1 : seg[0]].any()) if gap > 0 else False
        if gap <= gap_px or (gap <= bridge_max and faint):
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))

    gap_max = 0
    for a, b in zip(segs, segs[1:]):
        gap_max = max(gap_max, b[0] - a[1] - 1)

    if min_width > 1:
        merged = [m for m in merged if (m[1] - m[0] + 1) >= min_width]

    return RunInfo(
        runs=len(merged),
        runs_raw=len(segs),
        gap_max=gap_max,
        box=box,
        px=int(ink.strong.sum()),
    )


# --------------------------------------------------------------------------
# 比较式
# --------------------------------------------------------------------------

_OPS = ("==", ">=", "<=", "!=", ">", "<")
_OP_ALIASES = {"=": "==", "=>": ">=", "=<": "<="}


@dataclass
class Cond:
    """``"<-0.02"`` / ``">=0.25"`` / ``[1.1, 4.5]`` 这种人类写法。"""

    kind: str  # compare / range
    op: str = ""
    value: float = 0.0
    hi: float = 0.0
    raw: str = ""

    def test(self, x: float) -> bool:
        if self.kind == "range":
            return self.value <= x <= self.hi
        if self.op == "==":
            return abs(x - self.value) <= 1e-9
        if self.op == "!=":
            return abs(x - self.value) > 1e-9
        if self.op == ">=":
            return x >= self.value
        if self.op == "<=":
            return x <= self.value
        if self.op == ">":
            return x > self.value
        if self.op == "<":
            return x < self.value
        raise AssertionError(self.op)

    def headroom(self, x: float) -> str:
        """给 probe 打印"离阈值还有多远"，一眼看出该往哪边调。"""
        if self.kind == "range":
            if x < self.value:
                return f"低于下限 {self.value - x:.3f}"
            if x > self.hi:
                return f"超过上限 {x - self.hi:.3f}"
            return f"区间内（下余 {x - self.value:.3f} / 上余 {self.hi - x:.3f}）"
        delta = x - self.value
        if self.op in (">=", ">"):
            return f"{'够' if self.test(x) else '差'} {delta:+.3f}"
        if self.op in ("<=", "<"):
            return f"{'够' if self.test(x) else '差'} {delta:+.3f}"
        return f"{'等' if self.test(x) else '不等'}"

    def level(self, x: float) -> float:
        """0~1 的进度条：贴近阈值 ≈0.5，远离阈值（通过方向）≈1。"""
        if self.kind == "range":
            if self.test(x):
                span = max(self.hi - self.value, 1e-6)
                mid = (self.hi + self.value) / 2
                return float(min(1.0, 0.75 + 0.25 * min(1.0, abs(x - mid) / (span / 2 + 1e-9))))
            return 0.0
        scale = abs(self.value) if abs(self.value) > 1e-9 else 1.0
        ratio = abs(x - self.value) / scale
        if self.test(x):
            return float(min(1.0, 0.5 + 0.5 * min(1.0, ratio)))
        return float(max(0.0, 0.5 - 0.5 * min(1.0, ratio)))


def parse_cond(spec: Any, *, name: str = "判据", default_op: str | None = None) -> Cond:
    if isinstance(spec, bool):
        raise PredicateSpecError(f"{name} 的值不能是 true/false，写具体数字或比较式")
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        if default_op is None:
            raise PredicateSpecError(
                f"{name} 直接给数字太含糊",
                '写成 ">=0.25" 或 "<-0.02"，让人一眼知道方向',
            )
        return Cond(kind="compare", op=default_op, value=float(spec), raw=str(spec))
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        lo, hi = float(spec[0]), float(spec[1])
        if lo > hi:
            raise PredicateSpecError(f"{name} 的区间 {spec} 下限比上限大", "写成 [下限, 上限]")
        return Cond(kind="range", value=lo, hi=hi, raw=f"[{lo}, {hi}]")
    if isinstance(spec, str):
        text = spec.replace(" ", "")
        for op in _OPS:
            if text.startswith(op):
                rest = text[len(op) :]
                try:
                    return Cond(kind="compare", op=op, value=float(rest), raw=spec)
                except ValueError:
                    raise PredicateSpecError(
                        f"{name} 的比较式 {spec!r} 里不是数字", '例如 "<-0.02"'
                    ) from None
        alias = text[0] if text and text[0] in _OP_ALIASES else ""
        if alias:
            return parse_cond(_OP_ALIASES[alias] + text[1:], name=name, default_op=default_op)
        try:
            val = float(text)
        except ValueError:
            raise PredicateSpecError(
                f"{name} 的写法 {spec!r} 看不懂",
                '支持 ">=0.25"、"<-0.02"、[1.1, 4.5] 这三种',
            ) from None
        if default_op is None:
            raise PredicateSpecError(
                f"{name} 的 {spec!r} 缺少比较方向",
                '补上方向，比如 ">=0.25" 或 "<=0.25"',
            )
        return Cond(kind="compare", op=default_op, value=val, raw=spec)
    if isinstance(spec, Mapping):
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is None and hi is None:
            raise PredicateSpecError(f"{name} 的字典里要有 min 或 max")
        # 只给 min 就退化成 ">= min"，只给 max 退化成 "<= max"，
        # 这样 headroom / level 打印出来才说得清"离阈值多远"
        if lo is not None and hi is None:
            return parse_cond(f">={float(lo)}", name=name)
        if hi is not None and lo is None:
            return parse_cond(f"<={float(hi)}", name=name)
        lo_f, hi_f = float(lo), float(hi)
        if lo_f > hi_f:
            raise PredicateSpecError(f"{name} 的 min 大于 max（{lo_f} > {hi_f}）")
        return Cond(kind="range", value=lo_f, hi=hi_f, raw=f"min={lo} max={hi}")
    raise PredicateSpecError(f"{name} 的值 {spec!r} 类型不支持", "用数字、字符串比较式或 [lo, hi]")


# --------------------------------------------------------------------------
# 判据基类
# --------------------------------------------------------------------------

PREDICATES: dict[str, type] = {}


class PredicateSpecError(ValueError):
    """YAML 里给判据的参数不对。hint 会一起打到用户脸上。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message if not hint else f"{message}（{hint}）")
        self.message = message
        self.hint = hint


@dataclass
class Judge:
    predicate: str
    value: float
    passed: bool
    level: float
    detail: dict[str, Any] = field(default_factory=dict)
    head: str = ""

    def text(self, digits: int = 3) -> str:
        return f"{self.predicate}={self.value:.{digits}f}{'' if self.passed else ' ✗'}"


def register(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name  # type: ignore[attr-defined]
        PREDICATES[name] = cls
        return cls

    return deco


class Predicate:
    """所有判据的共同父类，子类实现 :meth:`measure`。"""

    name = ""
    #: 判据是"连续量"还是"布尔量"，probe 用来决定怎么打印
    numeric = True

    def __init__(self, spec: Any = None, *, validate_only: bool = False, base_dir: str | None = None) -> None:
        self.spec = spec
        self.validate_only = validate_only
        self.base_dir = base_dir
        self._parse(spec)

    # 子类在 __init__ 里通过构造函数参数完成建对象；这里留个空实现
    def _parse(self, spec: Any) -> None:  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def measure(self, frame: Frame) -> Judge:  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# color_ratio：颜色占比
# --------------------------------------------------------------------------


@register("color_ratio")
class ColorRatio(Predicate):
    """区域里占了多少比例的某种颜色。

    ``{color: "#d0021b", min: 0.25}``
    """

    def _parse(self, spec: Any) -> None:
        if spec is None:
            spec = {}
        if isinstance(spec, str):
            spec = {"color": spec, "min": 0.05}
        if not isinstance(spec, Mapping):
            raise PredicateSpecError(
                "color_ratio 要写成字典",
                '例如 {color: "#d0021b", min: 0.25}',
            )
        color = spec.get("color")
        if color is None:
            raise PredicateSpecError("color_ratio 缺少 color", '例如 color: "#d0021b"')
        # 单个颜色 / [r,g,b] / 多个颜色的列表 都收
        if _is_rgb(color):
            items: list[Any] = [color]
        elif isinstance(color, (list, tuple)):
            items = list(color)
        else:
            items = [color]
        colors = [np.array(parse_color(item), dtype=np.int16) for item in items]
        if not colors:
            raise PredicateSpecError("color_ratio 的 color 是空的")

        if "min" in spec or "max" in spec:
            self.cond = parse_cond({"min": spec.get("min"), "max": spec.get("max")}, name="color_ratio 的 min/max")
        else:
            raise PredicateSpecError(
                "color_ratio 要给 min（或 max）",
                '例如 min: 0.25 表示"这块区域里 25% 以上是这种颜色"',
            )
        self.tol = int(spec.get("tol", 40))
        if not (0 <= self.tol <= 255):
            raise PredicateSpecError(f"color_ratio 的 tol={self.tol} 超出 0~255")
        self._colors = colors

    def measure(self, frame: Frame) -> Judge:
        rgb = frame.rgb()
        # 逐通道比区间，而不是先整块转 int16 再 abs().max()
        # （"各通道都在 ±tol 内" 和 "最大通道差 <= tol" 是等价的）
        tol = self.tol
        hit = np.zeros(rgb.shape[:2], dtype=bool)
        for target in self._colors:
            ok: np.ndarray | None = None
            for ch in range(3):
                c = int(target[ch])
                lo = max(0, c - tol)
                hi = min(255, c + tol)
                band = rgb[..., ch]
                cond = band >= np.uint8(lo)
                if hi < 255:
                    cond &= band <= np.uint8(hi)
                ok = cond if ok is None else (ok & cond)
            if ok is not None:
                hit |= ok
        ratio = float(hit.mean()) if hit.size else 0.0
        passed = self.cond.test(ratio)
        return Judge(
            predicate="color_ratio",
            value=ratio,
            passed=passed,
            level=self.cond.level(ratio),
            head=self.cond.headroom(ratio),
            detail={
                "ratio": round(ratio, 4),
                "px": int(hit.sum()),
                "total": int(hit.size),
                "tol": self.tol,
            },
        )


@register("ink_ratio")
class InkRatio(Predicate):
    """区域里"有墨"的像素占比。比 color_ratio 更通用：不看具体颜色，只看有没有字。"""

    def _parse(self, spec: Any) -> None:
        opts: dict[str, Any] = {}
        if spec is None:
            pass
        elif isinstance(spec, bool):
            opts = {"min": float(spec)}
        elif isinstance(spec, (int, float)):
            opts = {"min": float(spec)}
        elif isinstance(spec, (str, list, tuple)):
            self.cond = parse_cond(spec, name="ink_ratio", default_op=">=")
        elif isinstance(spec, Mapping):
            opts = dict(spec)
        else:
            raise PredicateSpecError("ink_ratio 的参数看不懂", "例如 ink_ratio: {min: 0.03}")

        if not hasattr(self, "cond"):
            if "min" in opts or "max" in opts:
                self.cond = parse_cond({"min": opts.get("min"), "max": opts.get("max")}, name="ink_ratio")
            else:
                self.cond = parse_cond({"min": 0.05}, name="ink_ratio")

        self.ink_mode = opts.get("ink", "auto")
        self.threshold = opts.get("threshold")
        self.ink_color = opts.get("color")
        self.denoise = int(opts.get("denoise", 0) or 0)
        unknown = set(opts) - {"min", "max", "ink", "threshold", "color", "denoise"}
        if unknown:
            raise PredicateSpecError(f"ink_ratio 里有不认识的字段：{sorted(unknown)}")

    def measure(self, frame: Frame) -> Judge:
        ink = frame.ink(self.ink_mode, self.threshold, self.ink_color, denoise=self.denoise)
        ratio = float(ink.strong.mean()) if ink.strong.size else 0.0
        passed = self.cond.test(ratio)
        return Judge(
            predicate="ink_ratio",
            value=ratio,
            passed=passed,
            level=self.cond.level(ratio),
            head=self.cond.headroom(ratio),
            detail={"ink_px": ink.count, "total": int(ink.strong.size), "threshold": ink.threshold},
        )


def _is_rgb(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 3 and all(
        isinstance(v, (int, float)) for v in value
    )


# --------------------------------------------------------------------------
# glyph_runs：列簇数（单字形 vs 多字文本）
# --------------------------------------------------------------------------


@register("glyph_runs")
class GlyphRuns(Predicate):
    """数竖着的列簇。``glyph_runs: 1`` = 只允许一个字形。

    想表达"至少两簇（也就是多字文本）"就写 ``glyph_runs: ">=2"``。
    """

    numeric = False

    def _parse(self, spec: Any) -> None:
        if isinstance(spec, Mapping):
            opts = dict(spec)
            if "count" in opts:
                self.cond = parse_cond(opts.pop("count"), name="glyph_runs.count", default_op="==")
            elif "runs" in opts:
                self.cond = parse_cond(opts.pop("runs"), name="glyph_runs.runs", default_op="==")
            else:
                self.cond = parse_cond(1, name="glyph_runs", default_op="==")
            self.min_area = int(opts.get("min_area", 40))
            self.min_width = int(opts.get("min_width", 1))
            self.gap_px = int(opts.get("gap_px", 0))
            self.bridge_max = int(opts.get("bridge_max", 1))
            self.ink_mode = opts.get("ink", "auto")
            self.threshold = opts.get("threshold")
            self.ink_color = opts.get("color")
            self.denoise = int(opts.get("denoise", 0) or 0)
            unknown = set(opts) - {
                "min_area",
                "min_width",
                "gap_px",
                "bridge_max",
                "ink",
                "threshold",
                "color",
                "denoise",
            }
            if unknown:
                raise PredicateSpecError(
                    f"glyph_runs 里有不认识的字段：{sorted(unknown)}",
                    "可用：count/runs、min_area、min_width、gap_px、bridge_max、ink、threshold、color、denoise",
                )
        else:
            self.cond = parse_cond(spec, name="glyph_runs", default_op="==")
            self.min_area = 40
            self.min_width = 1
            self.gap_px = 0
            self.bridge_max = 1
            self.ink_mode = "auto"
            self.threshold = None
            self.ink_color = None
            self.denoise = 0

    def measure(self, frame: Frame) -> Judge:
        ink = frame.ink(self.ink_mode, self.threshold, self.ink_color, denoise=self.denoise)
        info = count_runs(
            ink, min_area=self.min_area, min_width=self.min_width, gap_px=self.gap_px, bridge_max=self.bridge_max
        )
        value = float(info.runs)
        passed = self.cond.test(value)
        return Judge(
            predicate="glyph_runs",
            value=value,
            passed=passed,
            level=self.cond.level(value),
            head=self.cond.headroom(value),
            detail={
                "runs": info.runs,
                "runs_raw": info.runs_raw,
                "gap_max": info.gap_max,
                "box": info.box.as_list() if info.box else None,
                "ink_px": info.px,
                "threshold": ink.threshold,
            },
        )


# --------------------------------------------------------------------------
# aspect：外接框宽高比
# --------------------------------------------------------------------------


@register("aspect")
class Aspect(Predicate):
    """墨迹外接框的宽 / 高。箭头 ≈2.6，正方的字 ≈1.0。"""

    def _parse(self, spec: Any) -> None:
        if isinstance(spec, Mapping):
            opts = dict(spec)
            if "min" in opts or "max" in opts:
                self.cond = parse_cond({"min": opts.pop("min", None), "max": opts.pop("max", None)}, name="aspect")
            else:
                raise PredicateSpecError("aspect 要写 [下限, 上限] 或 {min: ..., max: ...}")
            self.min_area = int(opts.pop("min_area", 40))
            self.ink_mode = opts.pop("ink", "auto")
            self.threshold = opts.pop("threshold", None)
            self.ink_color = opts.pop("color", None)
            self.denoise = int(opts.pop("denoise", 0) or 0)
            if opts:
                raise PredicateSpecError(f"aspect 里有不认识的字段：{sorted(opts)}")
        else:
            self.cond = parse_cond(spec, name="aspect")
            if self.cond.kind != "range":
                raise PredicateSpecError(
                    "aspect 要写成区间",
                    '例如 aspect: [1.1, 4.5]（宽高比落在这个范围里算命中）',
                )
            self.min_area = 40
            self.ink_mode = "auto"
            self.threshold = None
            self.ink_color = None
            self.denoise = 0

    def measure(self, frame: Frame) -> Judge:
        ink = frame.ink(self.ink_mode, self.threshold, self.ink_color, denoise=self.denoise)
        box = bbox_of(ink.strong, min_area=self.min_area)
        if box is None:
            return Judge(
                predicate="aspect",
                value=0.0,
                passed=False,
                level=0.0,
                head="没有墨迹",
                detail={"box": None, "ink_px": ink.count},
            )
        value = box.aspect
        passed = self.cond.test(value)
        return Judge(
            predicate="aspect",
            value=value,
            passed=passed,
            level=self.cond.level(value),
            head=self.cond.headroom(value),
            detail={"box": box.as_list(), "ink_px": ink.count},
        )


# --------------------------------------------------------------------------
# centroid_dx：质心水平偏移
# --------------------------------------------------------------------------


@register("centroid_dx")
class CentroidDX(Predicate):
    """墨迹质心相对外接框中心的水平偏移，按框宽归一化。

    ``"<-0.02"`` 表示墨偏左（箭头朝左）；``">0.02"`` 朝右；对称字形 ≈0。
    """

    def _parse(self, spec: Any) -> None:
        if isinstance(spec, Mapping):
            opts = dict(spec)
            if "min" in opts or "max" in opts:
                self.cond = parse_cond({"min": opts.pop("min", None), "max": opts.pop("max", None)}, name="centroid_dx")
            elif "lt" in opts:
                self.cond = parse_cond(f"<{float(opts.pop('lt'))}", name="centroid_dx")
            elif "gt" in opts:
                self.cond = parse_cond(f">{float(opts.pop('gt'))}", name="centroid_dx")
            else:
                raise PredicateSpecError(
                    'centroid_dx 要写 "<-0.02" 这种比较式',
                    "或者 {min: -1.0, max: -0.02}",
                )
            self.min_area = int(opts.pop("min_area", 40))
            self.ink_mode = opts.pop("ink", "auto")
            self.threshold = opts.pop("threshold", None)
            self.ink_color = opts.pop("color", None)
            self.denoise = int(opts.pop("denoise", 0) or 0)
            if opts:
                raise PredicateSpecError(f"centroid_dx 里有不认识的字段：{sorted(opts)}")
        else:
            self.cond = parse_cond(spec, name="centroid_dx")
            if self.cond.kind == "range":
                pass
            elif not self.cond.op:
                raise PredicateSpecError('centroid_dx 要写比较式，例如 "<-0.02"')
            self.min_area = 40
            self.ink_mode = "auto"
            self.threshold = None
            self.ink_color = None
            self.denoise = 0

    def measure(self, frame: Frame) -> Judge:
        ink = frame.ink(self.ink_mode, self.threshold, self.ink_color, denoise=self.denoise)
        box = bbox_of(ink.strong, min_area=self.min_area)
        if box is None:
            return Judge(
                predicate="centroid_dx",
                value=0.0,
                passed=False,
                level=0.0,
                head="没有墨迹",
                detail={"box": None, "ink_px": ink.count},
            )
        value = centroid_offset(ink.strong, box)
        passed = self.cond.test(value)
        return Judge(
            predicate="centroid_dx",
            value=value,
            passed=passed,
            level=self.cond.level(value),
            head=self.cond.headroom(value),
            detail={"box": box.as_list(), "ink_px": ink.count},
        )


# --------------------------------------------------------------------------
# diff：和上一帧 / 参考图的变化
# --------------------------------------------------------------------------


@register("diff")
class Diff(Predicate):
    """变化率。默认跟上一帧比（"屏幕这块有没有动"）；也可以跟一张参考图比。

    ``{min: 0.05}`` 表示至少 5% 的像素变了。
    """

    def _parse(self, spec: Any) -> None:
        if spec is None:
            spec = {"min": 0.05}
        if isinstance(spec, bool):
            raise PredicateSpecError("diff 不要用 true/false", "写 {min: 0.05} 这种变化率")
        if isinstance(spec, (int, float)):
            spec = {"min": float(spec)}
        if isinstance(spec, str) or (isinstance(spec, (list, tuple)) and len(spec) == 2):
            self.cond = parse_cond(spec, name="diff", default_op=">=")
            self.pixel_tol = 12
            self.downsample = 2
            self.reference = None
            self._ref = None
            self._prev = None
            return
        if not isinstance(spec, Mapping):
            raise PredicateSpecError("diff 要写成字典", "例如 {min: 0.05, pixel_tol: 12}")
        proc = dict(spec)
        if "min" in proc or "max" in proc:
            self.cond = parse_cond({"min": proc.pop("min", None), "max": proc.pop("max", None)}, name="diff")
        else:
            self.cond = parse_cond({"min": 0.05}, name="diff")
        self.pixel_tol = int(proc.pop("pixel_tol", 12))
        self.downsample = max(1, int(proc.pop("downsample", 2)))
        self.reference = proc.pop("reference", None)
        self._ref: np.ndarray | None = None
        self._prev: np.ndarray | None = None

        if self.reference is not None and not self.validate_only:
            self._ref = self._load_reference(str(self.reference), self.base_dir)
            if self._ref is None:
                raise PredicateSpecError(
                    f"diff 的参考图读不到：{self.reference}",
                    "路径相对 YAML 文件所在目录；也支持 PNG/JPG",
                )

    def _load_reference(self, path: str, base_dir: str | None = None) -> np.ndarray | None:
        if base_dir and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        if not os.path.exists(path):
            return None
        try:
            from PIL import Image  # 只有用到参考图才依赖 PIL
        except ImportError:
            raise PredicateSpecError(
                "diff 用参考图需要 Pillow",
                "pip install pillow，或者去掉 reference 改为和上一帧比",
            ) from None
        im = Image.open(path).convert("RGB")
        gray = self._gray_of(np.asarray(im, dtype=np.uint8))
        # 参考图也要按同样的采样率降采样，否则拿全分辨率底图和降采样后的当前帧比，
        # 会比到"底图的左上角那一块"，变化率直接飙到 0.5 以上（见 PITFALLS 5.5）
        if self.downsample > 1:
            gray = gray[:: self.downsample, :: self.downsample]
        return gray

    def _gray_of(self, rgb: np.ndarray) -> np.ndarray:
        rgb = np.clip(rgb, 0, 255).astype(np.float32)
        return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114

    def _current(self, frame: Frame) -> np.ndarray:
        gray = frame.gray()
        if self.downsample > 1:
            gray = gray[:: self.downsample, :: self.downsample]
        return gray

    def measure(self, frame: Frame) -> Judge:
        cur = self._current(frame)
        base = self._ref if self._ref is not None else self._prev
        if base is None:
            ratio = 0.0
            baseline = "first"
        else:
            if base.shape != cur.shape:
                base = base[: cur.shape[0], : cur.shape[1]]
                cur = cur[: base.shape[0], : base.shape[1]]
            ratio = float((np.abs(cur - base) > self.pixel_tol).mean())
            baseline = "ref" if self._ref is not None else "prev"
        self._prev = cur
        passed = self.cond.test(ratio)
        return Judge(
            predicate="diff",
            value=ratio,
            passed=passed,
            level=self.cond.level(ratio),
            head=self.cond.headroom(ratio),
            detail={"ratio": round(ratio, 4), "baseline": baseline, "pixel_tol": self.pixel_tol},
        )

    def reset(self) -> None:
        self._prev = None


# --------------------------------------------------------------------------
# ocr_keyword：可选 OCR
# --------------------------------------------------------------------------


@register("ocr_keyword")
class OcrKeyword(Predicate):
    """区域里有指定关键词（可选 extra：rapidocr）。做兜底用，别拿它当主力。"""

    numeric = False
    _engine_cache: dict[tuple, Any] = {}

    def _parse(self, spec: Any) -> None:
        if isinstance(spec, str):
            spec = {"keyword": spec}
        if isinstance(spec, (list, tuple)):
            spec = {"keyword": list(spec)}
        if not isinstance(spec, Mapping):
            raise PredicateSpecError("ocr_keyword 要写成字典", '例如 {keyword: ["危急值"], min_conf: 0.6}')
        proc = dict(spec)
        kw = proc.pop("keyword", proc.pop("keywords", None))
        if not kw:
            raise PredicateSpecError("ocr_keyword 缺少 keyword", '例如 keyword: "危急值"')
        self.keywords = [kw] if isinstance(kw, str) else [str(k) for k in kw]
        self.min_conf = float(proc.pop("min_conf", 0.5))
        self.require_all = bool(proc.pop("all", True))
        self.scale = float(proc.pop("scale", 2.0))
        self.lang = proc.pop("lang", "ch")
        if proc:
            raise PredicateSpecError(f"ocr_keyword 里有不认识的字段：{sorted(proc)}")
        if not self.validate_only:
            self._ensure_engine()

    def _ensure_engine(self) -> None:
        key = (self.lang,)
        if key in self._engine_cache:
            return
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError:
            raise PredicateSpecError(
                "用了 ocr_keyword 但没装 OCR 依赖",
                'pip install "pixelbus[ocr]"（就是 rapidocr-onnxruntime）',
            ) from None
        self._engine_cache[key] = RapidOCR()

    def measure(self, frame: Frame) -> Judge:
        self._ensure_engine()
        engine = self._engine_cache[(self.lang,)]
        import cv2  # rapidocr 自带

        rgb = frame.rgb()[:, :, ::-1]  # rapidocr 要 BGR
        if self.scale != 1.0:
            rgb = cv2.resize(rgb, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_CUBIC)
        result, _ = engine(rgb)
        texts: list[tuple[str, float]] = []
        for item in result or []:
            box, text, conf = item[0], item[1], float(item[2])
            if conf >= self.min_conf:
                texts.append((str(text), conf))
        joined = " ".join(t for t, _ in texts)
        hits = [k for k in self.keywords if k in joined]
        if self.require_all:
            passed = len(hits) == len(self.keywords) and bool(hits)
        else:
            passed = bool(hits)
        conf = max((c for _, c in texts), default=0.0)
        return Judge(
            predicate="ocr_keyword",
            value=float(len(hits)),
            passed=passed,
            level=1.0 if passed else 0.0,
            head=f"命中 {len(hits)}/{len(self.keywords)}",
            detail={"text": joined[:200], "hits": hits, "max_conf": round(conf, 3), "lines": len(texts)},
        )


# --------------------------------------------------------------------------
# 组合
# --------------------------------------------------------------------------


@dataclass
class TestResult:
    hit: bool
    judges: list[Judge]
    level: float
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{j.predicate}={j.value:.3f}{'✓' if j.passed else '✗'}" for j in self.judges]
        return "  ".join(parts)


class TestSet:
    """一条 signal 的全部判据。默认全都要过（AND），也能配成任一（OR）。"""

    def __init__(self, predicates: list[Predicate], mode: str = "all") -> None:
        if not predicates:
            raise PredicateSpecError("test 里至少要有一条判据")
        self.predicates = predicates
        self.mode = mode

    def measure(self, frame: Frame) -> TestResult:
        judges = [p.measure(frame) for p in self.predicates]
        if self.mode == "any":
            hit = any(j.passed for j in judges)
            level = max((j.level for j in judges if j.passed), default=0.0)
        else:
            hit = all(j.passed for j in judges)
            level = min(j.level for j in judges) if judges else 0.0
        return TestResult(
            hit=hit,
            judges=judges,
            level=float(level),
            failed=[j.predicate for j in judges if not j.passed],
        )


def available_predicates() -> list[str]:
    return sorted(PREDICATES)


def build_test(
    test: Mapping[str, Any],
    *,
    validate_only: bool = False,
    mode: str = "all",
    base_dir: str | None = None,
) -> TestSet:
    """把 YAML 里的 ``test:`` 字典变成可执行的 TestSet。

    平铺多条判据，默认全部要过（AND）；signal 里写 ``mode: any`` 就变成任一命中。
    只认识 :data:`PREDICATES` 里的名字，写错会抛 :class:`PredicateSpecError`，
    并提示最接近的合法名字（拼错 key 是最高频的报错）。
    """
    if not isinstance(test, Mapping):
        raise PredicateSpecError(
            f"test 应该是字典，实际是 {type(test).__name__}",
            '例如 test: {color_ratio: {color: "#d0021b", min: 0.25}}',
        )
    if not test:
        raise PredicateSpecError("test 是空的", "至少写一条判据")

    preds: list[Predicate] = []
    for key, spec in test.items():
        if key in ("all", "any"):
            raise PredicateSpecError(
                f"test 里不该再嵌套 {key}",
                "直接平铺多条判据（默认 AND），要 OR 就在 signal 上写 mode: any",
            )
        cls = PREDICATES.get(key)
        if cls is None:
            hint = _closest(key, available_predicates())
            raise PredicateSpecError(
                f"没有叫 {key!r} 的判据",
                f'是不是想写 {hint!r}？可用判据：{", ".join(available_predicates())}',
            )
        try:
            preds.append(cls(spec, validate_only=validate_only, base_dir=base_dir))
        except PredicateSpecError as exc:
            raise PredicateSpecError(f"{key}: {exc.message}", exc.hint) from None
    return TestSet(preds, mode=mode)


def _closest(word: str, candidates: Iterable[str]) -> str:
    import difflib

    match = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.5)
    return match[0] if match else "color_ratio"
