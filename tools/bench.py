"""性能基准：默认 1080p / 3 个区域 / 15fps，报 CPU 占用和常驻内存。

    python tools/bench.py                     # 跑 10 秒
    python tools/bench.py --seconds 30 --fps 15

CPU 用 ``time.process_time()`` 的增量除以墙钟，所以 100% = 单核满载。
内存用 psapi 的 WorkingSetSize（Windows）或 /proc/self/statm（Linux）。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixelbus.engine import Engine
from pixelbus.rules import loads_rules


def rss_mb() -> float:
    if platform.system() == "Windows":
        # ctypes.windll 的默认签名会把 HANDLE 当 int 截断，必须显式声明 argtypes，
        # 否则 GetProcessMemoryInfo 静默失败、返回全 0（这个坑踩过一次）
        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int

        info = PMC()
        info.cb = ctypes.sizeof(PMC)
        ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(info), info.cb)
        if not ok:
            return float("nan")
        return info.WorkingSetSize / 1024 / 1024
    try:
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except OSError:
        return float("nan")


def build_rules(fps: int) -> object:
    text = f"""
version: 1
fps: {fps}
name: bench
regions:
  alarm: {{rect: [0.72, 0.08, 0.26, 0.10]}}
  center: {{rect: [0.35, 0.35, 0.30, 0.30]}}
  status: {{rect: [0.02, 0.80, 0.30, 0.10]}}
signals:
  red_alarm: {{region: alarm, test: {{color_ratio: {{color: "#d0021b", min: 0.25}}}}}}
  ink_mid: {{region: center, test: {{ink_ratio: {{min: 0.05}}}}}}
  moved: {{region: status, test: {{diff: {{min: 0.04, downsample: 2}}}}}}
states:
  alarm_on: {{when: red_alarm, stable_frames: 3}}
  busy: {{when: ink_mid | moved, stable_frames: 2}}
"""
    return loads_rules(text, path="bench.yaml")


def _synthetic(args) -> int:
    """只测判据本身：合成三块和 1080p 下尺寸一致的图，每帧抖一个像素（打掉"整帧未变"的复用）。

    这个数字不受当前屏幕在播什么影响，用来判断"引擎自身"够不够快。
    """
    from pixelbus.predicates import Frame  # noqa: F401  (确保判据模块加载)

    rules = build_rules(args.fps)
    engine = Engine(rules)
    w, h = 1920, 1080
    frames: dict[str, np.ndarray] = {}
    for name, region in rules.regions.items():
        spec = region.spec
        if spec.rect:
            fw = max(8, int(round(spec.rect[2] * w)))
            fh = max(8, int(round(spec.rect[3] * h)))
        else:
            fw, fh = 400, 100
        img = np.full((fh, fw, 4), 245, dtype=np.uint8)
        img[..., 3] = 255
        # 画点"字"和"红块"，免得所有判据都在纯白上跑（纯白最容易走短路）
        img[fh // 4 : fh // 2, fw // 5 : fw // 2, :3] = (20, 20, 20)
        img[fh // 2 : fh * 3 // 4, fw // 3 : fw // 3 + 40, :3] = (27, 2, 208)  # BGRA 的蓝红反过来
        frames[name] = img

    n = max(30, int(args.seconds * args.fps))
    tick = 0

    def jitter() -> None:
        """每帧改一个像素的亮度 —— 让"整帧没变就复用"的快路径失效，测最坏情况。"""
        nonlocal tick
        tick += 1
        for arr in frames.values():
            hh, ww = arr.shape[:2]
            arr[(tick * 7) % hh, (tick * 13) % ww, 0] = (tick * 37) % 255

    for i in range(n):  # 预热
        jitter()
        engine.step(float(i), frames)

    cpu0 = time.process_time()
    t0 = time.perf_counter()
    for i in range(n):
        jitter()
        engine.step(float(i), frames)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    per_frame = cpu / n * 1000
    # 百分比 = (毫秒/帧 × 每秒帧数) / 1000 × 100
    cpu_pct = per_frame * args.fps / 10.0
    print(f"pixelbus 判据基准 · {n} 帧 · 每帧 {per_frame:.2f}ms CPU（墙钟 {wall / n * 1000:.2f}ms）")
    print(f"  折算到 {args.fps}fps：CPU {cpu_pct:.1f}%（单核 100%，不含抓屏）")
    print(f"  内存 {rss_mb():.1f}MB")
    return 0 if cpu_pct < 5.0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pixelbus 性能基准")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--warmup", type=float, default=1.5, help="预热秒数（不计入统计）")
    ap.add_argument(
        "--mode",
        choices=["capture", "synthetic"],
        default="capture",
        help="capture=真抓屏（含 GDI 开销）；synthetic=只测判据本身（不受屏幕内容干扰）",
    )
    args = ap.parse_args(argv)

    if args.mode == "synthetic":
        return _synthetic(args)

    if platform.system() != "Windows":
        print("提示：非 Windows 平台的抓屏开销差异较大，数字仅供参考。", file=sys.stderr)

    rules = build_rules(args.fps)
    engine = Engine(rules)
    print(f"pixelbus 基准 · {platform.system()} · {sys.version.split()[0]} · {rules.fps}fps · {len(rules.regions)} 区域")
    print(f"  预热 {args.warmup}s ...", flush=True)
    interval = 1.0 / max(0.2, args.fps)
    n_warm = int(args.warmup / interval)
    for _ in range(n_warm):
        engine.step()
        time.sleep(interval)

    # 逐帧计时，而不是用"整段墙钟 ÷ 帧数"：
    # 后者会把调度抖动、别的进程抢 CPU 都算进来，同一台机器能测出 3% 和 7% 两个值。
    steps = int(args.seconds / interval)
    t0 = time.perf_counter()
    cpu = 0.0
    peak = rss_mb()
    for i in range(steps):
        wake = t0 + (i + 1) * interval
        slack = wake - time.perf_counter()
        if slack > 0:
            time.sleep(slack)
        c0 = time.process_time()
        engine.step()
        cpu += time.process_time() - c0
        if i % 8 == 0:
            peak = max(peak, rss_mb())
    wall = time.perf_counter() - t0
    per_frame = cpu / steps * 1000
    cpu_pct = per_frame * args.fps / 10.0

    print()
    print(f"  {steps} 帧 / {wall:.2f}s = {steps / wall:.1f}fps（目标 {args.fps}）")
    print(f"  CPU  每帧 {per_frame:.2f}ms → {cpu_pct:.1f}%（单核为 100%，含抓屏）")
    print(f"  内存 峰值 {peak:.1f}MB / 结束 {rss_mb():.1f}MB")
    grabber = getattr(engine, "_grabber", None)
    if grabber is not None:
        print(f"  抓屏 快路径 {grabber.fast_grabs} 次 / 兼容 {grabber.compat_grabs} 次 —— {grabber.layer_note}")
    print(f"  丢帧 {engine._dropped}，抓屏失败 {engine.grab_errors}")
    ok = cpu_pct < 5.0 and peak < 120
    print("  结论：", "达标 (<5% CPU, <120MB)" if ok else "未达标")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
