"""用 demo 的 hit/idle 真实截图合成演示 GIF（不碰屏幕，随时可重跑）。

    python tools/make_demo_gif.py                 # 三个 demo 全做
    python tools/make_demo_gif.py alarm-popup     # 只做一个

这不是"实时录屏"。它把 ``shots/idle.png`` 和 ``shots/hit.png``（真实截图裁出来的
窗口图）按 idle → hit → idle 的顺序拼成循环 GIF，命中帧左上角打个红色 HIT 角标。
要录自己屏幕上的真实过程，用 ``tools/record_gif.py``。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

DEMOS = Path(__file__).resolve().parent.parent / "demos"
DEMO_NAMES = ["alarm-popup", "canvas-board", "legacy-report"]

MAX_WIDTH = 760
FRAME_MS = 160


def _badge(img: Image.Image, hit: bool) -> Image.Image:
    draw = ImageDraw.Draw(img)
    text = "HIT" if hit else "idle"
    color = (208, 2, 27) if hit else (90, 90, 90)
    draw.rectangle([0, 0, 58, 20], fill=color)
    draw.text((7, 5), text, fill=(255, 255, 255))
    return img


def build(demo: str) -> Path | None:
    shots = DEMOS / demo / "shots"
    hit_path, idle_path = shots / "hit.png", shots / "idle.png"
    if not hit_path.exists() or not idle_path.exists():
        print(f"{demo}: 缺 shots/hit.png 或 shots/idle.png，跳过")
        return None

    idle = Image.open(idle_path).convert("RGB")
    hit = Image.open(hit_path).convert("RGB")

    scale = min(1.0, MAX_WIDTH / idle.width)
    size = (int(idle.width * scale), int(idle.height * scale))
    frames = [_badge(idle.resize(size), False), _badge(idle.resize(size), False)]
    frames += [_badge(hit.resize(size), True)] * 3
    frames.append(_badge(idle.resize(size), False))

    out = DEMOS / demo / "demo.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:],
                   duration=FRAME_MS, loop=0, optimize=True)
    print(f"{demo}: {out} · {len(frames)} 帧 · {size[0]}×{size[1]} · {out.stat().st_size / 1024:.0f}KB")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用真实截图合成 demo GIF")
    ap.add_argument("demos", nargs="*", default=None)
    args = ap.parse_args(argv)
    done = 0
    for name in args.demos or DEMO_NAMES:
        if build(name):
            done += 1
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
