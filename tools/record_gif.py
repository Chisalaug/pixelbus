"""录一段目标区域的 GIF，给 demos/README 用。

    python tools/record_gif.py demos/alarm-popup/rules.yaml \
        --region alarm --seconds 6 --out demos/alarm-popup/demo.gif

引擎在后台按 fps 求值，逐帧录下指定区域；只要有任一 state 成立，左上角角标变红
显示 ``HIT``，否则灰色 ``idle``。录完用 Pillow 拼成循环 GIF。

素材是真实屏幕：先把目标界面调到要演示的状态，再跑这个命令。GIF 只裁区域、
不录全屏，也不存原始截图。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixelbus.engine import Engine
from pixelbus.rules import load_rules


def _bgra_to_pil(arr: np.ndarray) -> Image.Image:
    rgb = arr[..., [2, 1, 0]]
    return Image.fromarray(rgb, "RGB")


class _NullRunner:
    """录 GIF 时用的空动作执行器：能接住引擎的调用，但一条动作都不发。"""

    def handle(self, event, sync: bool = False) -> None:  # noqa: D102
        return None

    def configure(self, rules) -> None:  # noqa: D102
        return None

    def status(self) -> dict:  # noqa: D102
        return {}

    def shutdown(self) -> None:  # noqa: D102
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="录一块区域的 demo GIF")
    ap.add_argument("rules", help="demo 的 rules.yaml 路径")
    ap.add_argument("--region", required=True, help="要录的 region 名")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--out", required=True, help="输出 GIF 路径")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--scale", type=float, default=1.0, help="输出缩放（<1 缩小，减小 GIF 体积）")
    ap.add_argument("--warmup", type=float, default=1.0, help="预热秒数（不计入 GIF）")
    ap.add_argument("--no-actions", action="store_true", help="不执行动作（录的时候别真去 beep / 发 webhook）")
    args = ap.parse_args(argv)

    rules = load_rules(args.rules)
    if args.region not in rules.regions:
        print(f"规则里没有区域 {args.region!r}，可用：{', '.join(rules.regions)}")
        return 1

    engine = Engine(rules, runner=_NullRunner() if args.no_actions else None)
    interval = 1.0 / max(0.2, args.fps)

    def pump(seconds: float) -> None:
        for _ in range(max(1, int(seconds / interval))):
            engine.step()
            time.sleep(interval)

    print(f"预热 {args.warmup}s，把目标界面调到要演示的状态 ...", flush=True)
    pump(args.warmup)

    frames: list[Image.Image] = []
    steps = max(1, int(args.seconds / interval))
    print(f"录制 {steps} 帧 @ {args.fps:g}fps ...", flush=True)

    region_cfg = rules.regions[args.region]
    for i in range(steps):
        t0 = time.perf_counter()
        engine.step()
        arr = engine._grab(args.region, region_cfg)  # 同 bench 一样复用内部抓屏口子
        if arr is None:
            continue
        img = _bgra_to_pil(arr)
        if abs(args.scale - 1.0) > 1e-3:
            img = img.resize(
                (max(1, int(img.width * args.scale)), max(1, int(img.height * args.scale)))
            )

        snap = engine.snapshot()
        active = any(s["active"] for s in snap.states.values())
        # 角标画在图上会盖住内容（报表里正好压住"11"那行），所以在上面留一条 20px 的条子
        strip = 20
        canvas = Image.new("RGB", (img.width, img.height + strip), (28, 30, 34))
        canvas.paste(img, (0, strip))
        draw = ImageDraw.Draw(canvas)
        label = "HIT" if active else "idle"
        color = (208, 2, 27) if active else (90, 90, 90)
        draw.rectangle([0, 0, 56, strip - 2], fill=color)
        draw.text((7, 4), label, fill=(255, 255, 255))
        draw.text((64, 4), f"pixelbus · {args.region}", fill=(150, 152, 158))
        frames.append(canvas)

        slack = t0 + interval - time.perf_counter()
        if slack > 0:
            time.sleep(slack)

    engine.close()

    if not frames:
        print("没抓到任何帧，GIF 未生成（区域解析失败？跑 pixelbus doctor 看看）")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / args.fps),
        loop=0,
        optimize=True,
    )
    print(f"已保存 {out} · {len(frames)} 帧 · {frames[0].width}×{frames[0].height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
