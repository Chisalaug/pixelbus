"""用 demo 自己的真机截图校验判据：命中态要过，空闲态不能过，还要有余量。

每个 demo 的 ``shots/`` 里有 hit.png / idle.png（真实截图裁出来的窗口图）和
target.json（目标区域在该裁剪图里的位置）。本工具把规则里的 signal 判据直接
灌进这两块区域，打印分数并在"命中过、空闲不过"时返回 0。

    python tools/check_demo.py                # 三个 demo 全过一遍
    python tools/check_demo.py alarm-popup    # 只查一个
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixelbus.predicates import Frame, build_test
from pixelbus.rules import load_rules

DEMOS = Path(__file__).resolve().parent.parent / "demos"
DEMO_NAMES = ["alarm-popup", "canvas-board", "legacy-report"]


def _load_crop(demo: str, state: str) -> tuple[np.ndarray, dict]:
    shots = DEMOS / demo / "shots"
    img = np.asarray(Image.open(shots / f"{state}.png").convert("RGB"))
    meta = json.loads((shots / "target.json").read_text(encoding="utf-8"))
    t = meta["target_in_crop"]
    box = (t["x"], t["y"], t["x"] + t["w"], t["y"] + t["h"])
    l, top, r, b = box
    return img[top:b, l:r], meta


def check(demo: str) -> bool:
    rules = load_rules(DEMOS / demo / "rules.yaml")
    signal_name, signal_cfg = next(iter(rules.signals.items()))
    # base_dir 必须给：diff 的 reference 是相对 YAML 所在目录解析的
    test = build_test(
        signal_cfg.test,
        mode=signal_cfg.mode or "all",
        base_dir=str(DEMOS / demo),
    )

    print(f"\n== {demo} · signal {signal_name} ==")
    ok = True
    scores: dict[str, float] = {}
    for state in ("hit", "idle"):
        try:
            crop, meta = _load_crop(demo, state)
        except FileNotFoundError:
            print(f"  跳过：shots/{state}.png 不存在（真实截图没放进来）")
            return True
        res = test.measure(Frame(crop, order="rgb", region=signal_name))
        scores[state] = res.level
        print(f"  {state:4s} {crop.shape[1]}×{crop.shape[0]}  level={res.level:.4f}  hit={res.hit}")
        for j in res.judges:
            print(f"        {j.predicate:11s} value={j.value:9.4f} passed={j.passed}  ({j.head})")
        want = state == "hit"
        if res.hit != want:
            ok = False

    if "hit" in scores and "idle" in scores:
        gap = scores["hit"] - scores["idle"]
        print(f"  余量 {gap:.4f}")
        if gap < 0.2:
            ok = False
            print("  余量太小（<0.2），换台机器/换字体就会翻车，把阈值往两个分数中间挪")
    print("  →", "通过" if ok else "不通过")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用真机截图校验 demo 判据")
    ap.add_argument("demos", nargs="*", default=None, help=f"要查的 demo，默认全部：{DEMO_NAMES}")
    args = ap.parse_args(argv)

    names = args.demos or DEMO_NAMES
    results = {name: check(name) for name in names}
    bad = [n for n, v in results.items() if not v]
    print("\n汇总：", "全部通过" if not bad else f"不通过：{', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
