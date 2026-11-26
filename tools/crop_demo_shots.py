"""把两张全景截图裁成三个 demo 的命中/空闲参考图，并按规则里的区域写 target.json。

目标区域不靠肉眼估：直接读 rules.yaml 的 ``rect``（相对主显示器），乘 1920×1080
得到像素框。实测这样算出来的框和 orientations 完全对得上——legacy-report 的
baseline.png 在这套坐标系下平移搜索，最佳对齐误差是 0.00。
"""

import json
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixelbus.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent / "demos"
SRC = ROOT / "_mock" / "source"

# 全景截图里三个窗口的位置（左、上、右、下）
# board 左边留了 4px 余量：三个窗口是并排贴着的，按 956 裁会把左邻窗口的边缘带进来
WINDOWS = {
    "alarm-popup": (0, 0, 954, 532),
    "canvas-board": (958, 0, 1906, 532),
    "legacy-report": (0, 536, 1906, 1070),
}

SCREEN = (1920, 1080)


def target_box(demo: str, region: str) -> tuple[int, int, int, int]:
    rules = load_rules(ROOT / demo / "rules.yaml")
    spec = rules.regions[region].spec
    if spec.pixels:
        l, t, w, h = spec.pixels
    else:
        l, t, w, h = spec.rect
        l *= SCREEN[0]
        t *= SCREEN[1]
        w *= SCREEN[0]
        h *= SCREEN[1]
    return int(round(l)), int(round(t)), int(round(w)), int(round(h))


def main() -> None:
    for demo, win in WINDOWS.items():
        rules = load_rules(ROOT / demo / "rules.yaml")
        region = next(iter(rules.regions))
        full_hit = Image.open(SRC / "hit_full.png").convert("RGB")
        full_idle = Image.open(SRC / "idle_full.png").convert("RGB")

        out_dir = ROOT / demo / "shots"
        out_dir.mkdir(parents=True, exist_ok=True)
        for state, full in (("hit", full_hit), ("idle", full_idle)):
            full.crop(win).save(out_dir / f"{state}.png")

        x, y, w, h = target_box(demo, region)
        meta = {
            "source": {"width": full_idle.width, "height": full_idle.height},
            "screen": {"width": SCREEN[0], "height": SCREEN[1]},
            "region": region,
            "window_in_source": {"x": win[0], "y": win[1], "w": win[2] - win[0], "h": win[3] - win[1]},
            "target_in_source": {"x": x, "y": y, "w": w, "h": h},
            "target_in_crop": {"x": x - win[0], "y": y - win[1], "w": w, "h": h},
        }
        (out_dir / "target.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{demo:14s} region={region:6s} target={x},{y},{w},{h} -> crop-local "
              f"{x - win[0]},{y - win[1]}")


if __name__ == "__main__":
    main()
