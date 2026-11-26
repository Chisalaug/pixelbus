"""三个 demo 规则必须能通过契约校验（不真抓屏，只加载 YAML）。

阈值是用真机 probe 调出来的，跑测试的时候屏幕不一定有那三个窗口，所以这里只校验
"结构合法、名字对得上、几处容易改坏的地方还改没改坏"。
"""

from pathlib import Path

import pytest

from pixelbus.rules import load_rules

DEMOS = Path(__file__).resolve().parent.parent / "demos"


@pytest.mark.parametrize(
    "dir_name, state, signal",
    [
        ("alarm-popup", "alarm_on", "red_alarm"),
        ("canvas-board", "arrow_present", "arrow_seen"),
        ("legacy-report", "report_dirty", "table_changed"),
    ],
)
def test_demo_rules_load(dir_name: str, state: str, signal: str) -> None:
    rules = load_rules(DEMOS / dir_name / "rules.yaml")
    assert signal in rules.signals
    assert state in rules.states
    assert rules.actions, f"{dir_name} 至少要有一条动作"

    region = rules.signals[signal].region
    rect = rules.regions[region].spec.rect
    assert 0.0 <= rect[0] < 1.0 and 0.0 <= rect[1] < 1.0
    assert 0.0 < rect[2] <= 1.0 and 0.0 < rect[3] <= 1.0


def test_demo_thresholds_are_calibrated_not_placeholder() -> None:
    """占位阈值（0.25 / 0.04 那种随手写的）不许再回到 demo 里。"""
    alarm = load_rules(DEMOS / "alarm-popup" / "rules.yaml")
    assert alarm.signals["red_alarm"].test["color_ratio"]["min"] == pytest.approx(0.40)

    report = load_rules(DEMOS / "legacy-report" / "rules.yaml")
    diff = report.signals["table_changed"].test["diff"]
    assert diff["min"] == pytest.approx(0.15)
    # 粉底新行和米黄纸的灰度只差 8，容差取 12 就什么都测不到（PITFALLS 5.4）
    assert diff["pixel_tol"] <= 8


def test_board_demo_pins_ink_threshold() -> None:
    """白板是近白底 + 浅灰格线：阈值不写死，Otsu 会把格线当墨迹（PITFALLS 5.5）。"""
    board = load_rules(DEMOS / "canvas-board" / "rules.yaml")
    test = board.signals["arrow_seen"].test
    assert test["glyph_runs"]["ink"] == "dark"
    assert test["glyph_runs"]["threshold"] == 120
    for name in ("aspect", "ink_ratio"):
        assert test[name]["threshold"] == 120


def test_report_demo_has_baseline_image() -> None:
    """diff 靠固定底图，底图丢了整条规则就跑不起来。"""
    base = DEMOS / "legacy-report" / "baseline.png"
    assert base.is_file(), "demos/legacy-report/baseline.png 必须在仓库里"
    from PIL import Image

    with Image.open(base) as im:
        assert im.size[0] > 100 and im.size[1] > 50
