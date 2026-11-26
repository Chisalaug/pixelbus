"""交互式框选：全屏半透明遮罩，拖一个矩形出来。

**窗口本身在 ``_pick_gui.py`` 里，本文件只负责"挑一个能开窗的解释器"并解析结果。**

原因很实际：``import tkinter`` 成功 ≠ 能开窗。被打包/裁剪过的 Python（各种 AI
运行时自带的 relocatable Python 就是）里模块都在，但 ``tk.Tk()`` 会抛
``Can't find a usable init.tcl`` —— Tcl 的脚本数据被裁掉了。这时候只能换一个
解释器跑同一段 GUI 代码，所以 GUI 必须是独立文件、只用标准库。

坐标约定：遮罩覆盖**目标显示器**，返回的是相对该显示器的 ``rect``，和 YAML 里写的
完全是同一个东西，可以直接粘贴回去。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .capture import Grabber

__all__ = ["pick_region", "PickResult", "tk_backends", "tk_usable", "clear_tk_cache"]

GUI_SCRIPT = Path(__file__).with_name("_pick_gui.py")
PKG_DIR = Path(__file__).resolve().parent

_cached_ok: dict[str, bool] = {}


@dataclass
class PickResult:
    rect: tuple[float, float, float, float]
    bbox: dict[str, int]
    monitor: str | int

    def yaml_snippet(self, name: str, monitor: str | int = "primary") -> str:
        r = ", ".join(f"{v:.3f}" for v in self.rect)
        mon = monitor if isinstance(monitor, str) else str(monitor)
        return f"  {name}: {{monitor: {mon}, rect: [{r}]}}"


def _monitor_dict(spec: str | int, grabber: Grabber | None) -> dict[str, Any]:
    own = grabber is None
    if own:
        grabber = Grabber()
    try:
        mon = grabber.monitor(spec)
        return {
            "left": int(mon["left"]),
            "top": int(mon["top"]),
            "width": int(mon["width"]),
            "height": int(mon["height"]),
        }
    finally:
        if own and grabber is not None:
            grabber.close()


# --------------------------------------------------------------------------
# 挑解释器
# --------------------------------------------------------------------------


def tk_backends() -> list[list[str]]:
    """可用的解释器候选，按优先级排。

    ``PIXELBUS_TK_PYTHON`` 给用户一个明确的手动口子；``sys.executable`` 是首选（
    正常装的 Python 就够用）；剩下的走 ``py`` 启动器 / PATH 里的 python3。
    """
    out: list[list[str]] = []
    env = os.environ.get("PIXELBUS_TK_PYTHON")
    if env:
        out.append([env])
    out.append([sys.executable])

    launcher = shutil.which("py")
    if launcher:
        for tag in ("-3.14", "-3.13", "-3.12", "-3"):
            out.append([launcher, tag])

    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            out.append([path])
    return out


def tk_usable(cmd: Sequence[str]) -> bool:
    """真跑一次 ``tkinter.Tcl()``：只测 Tcl，不弹窗。

    这里必须测 Tcl 而不是只 ``import tkinter`` —— 裁过的 Python 里 import 会成功、
    到 Tk() 才炸，只在 import 上做判断会得到"看起来能用"的错误结论。
    """
    key = " ".join(cmd)
    if key in _cached_ok:
        return _cached_ok[key]
    code = "import tkinter; tkinter.Tcl()"
    try:
        proc = subprocess.run(
            [*cmd, "-c", code],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(PKG_DIR),
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _cached_ok[key] = ok
    return ok


def clear_tk_cache() -> None:
    _cached_ok.clear()


def _first_usable() -> list[str] | None:
    for cmd in tk_backends():
        if tk_usable(cmd):
            return list(cmd)
    return None


def _run_gui(cmd: Sequence[str], cfg: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        [*cmd, str(GUI_SCRIPT), json.dumps(cfg)],
        capture_output=True,
        text=True,
        cwd=str(PKG_DIR),
        env={**os.environ, "PYTHONPATH": str(PKG_DIR.parent)},
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"退出码 {proc.returncode}"
        raise RuntimeError(reason)
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("框选窗口没有返回结果")


# --------------------------------------------------------------------------
# 对外
# --------------------------------------------------------------------------


def pick_region(
    monitor: str | int = "primary",
    grabber: Grabber | None = None,
    title: str = "拖一个矩形，Esc 取消",
    alpha: float = 0.28,
) -> PickResult | None:
    """弹出遮罩让用户框选。返回相对坐标；用户按 Esc 则返回 None。

    找不到任何能开窗的解释器时抛 ``RuntimeError``，调用方降级成手输坐标。
    """
    if not GUI_SCRIPT.exists():  # pragma: no cover - 装坏了
        raise RuntimeError(f"找不到框选窗口脚本：{GUI_SCRIPT}")

    cmd = _first_usable()
    if cmd is None:
        raise RuntimeError(
            "这台机器上找不到能开 Tk 窗口的 Python（当前解释器可能裁掉了 Tcl 数据）。"
            "可以装一个官方 Python，或把能用的解释器路径塞进环境变量 PIXELBUS_TK_PYTHON"
        )

    mon = _monitor_dict(monitor, grabber)
    cfg = {
        "width": mon["width"],
        "height": mon["height"],
        "left": mon["left"],
        "top": mon["top"],
        "alpha": alpha,
        "title": title,
    }
    body = _run_gui(cmd, cfg)
    rect = body.get("rect")
    if rect is None:
        return None
    return PickResult(
        rect=tuple(round(float(v), 4) for v in rect),
        bbox={
            "left": mon["left"] + round(mon["width"] * rect[0]),
            "top": mon["top"] + round(mon["height"] * rect[1]),
            "width": max(1, round(mon["width"] * rect[2])),
            "height": max(1, round(mon["height"] * rect[3])),
        },
        monitor=monitor,
    )
