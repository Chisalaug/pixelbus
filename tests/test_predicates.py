"""判据单测：全部用 PIL 合成图，覆盖正例、反例，以及几个真实的坑。

最核心的一条：**多字文本不能被当成单字形** —— 这是整个项目最容易翻车的地方。
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from pixelbus.predicates import (
    Frame,
    PredicateSpecError,
    available_predicates,
    build_test,
    parse_cond,
    parse_color,
)

from conftest import (
    H,
    W,
    blank,
    bgra_of,
    draw_arrow,
    draw_caret,
    draw_plus,
    draw_text,
    fill_rect,
    frame_of,
)

RED = "#d0021b"
GREEN = "#00a65a"
BLUE = "#4a90d9"


def measure(spec: dict, frame: Frame):
    return build_test(spec).measure(frame)


# --------------------------------------------------------------------------
# color_ratio
# --------------------------------------------------------------------------


class TestColorRatio:
    def test_red_block_matches(self):
        im = fill_rect(blank(), [0, 0, 96, H - 1], RED)  # 约 30% 面积
        judge = measure({"color_ratio": {"color": RED, "min": 0.25}}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.value == pytest.approx(0.30, abs=0.03)
        assert judge.detail["px"] > 0

    def test_threshold_just_above_actual_fails(self):
        im = fill_rect(blank(), [0, 0, 96, H - 1], RED)
        judge = measure({"color_ratio": {"color": RED, "min": 0.35}}, frame_of(im)).judges[0]
        assert not judge.passed
        assert "差" in judge.head

    def test_wrong_color_never_matches(self):
        im = fill_rect(blank(), [0, 0, 96, H - 1], BLUE)
        judge = measure({"color_ratio": {"color": RED, "min": 0.1}}, frame_of(im)).judges[0]
        assert judge.value == 0.0
        assert not judge.passed

    def test_bgra_channel_order_is_not_swapped(self):
        """mss 给的是 BGRA；把 R 和 B 弄反，红会变蓝，这是最隐蔽的 bug。"""
        im = fill_rect(blank(), [0, 0, 96, H - 1], RED)
        frame = Frame(image=bgra_of(im), order="bgra")
        judge = measure({"color_ratio": {"color": RED, "min": 0.25}}, frame).judges[0]
        assert judge.passed, "BGRA 转 RGB 转错了：红色被当成蓝色"
        blue_judge = measure({"color_ratio": {"color": BLUE, "min": 0.1}}, frame).judges[0]
        assert not blue_judge.passed

    def test_tolerance_absorbs_theme_jitter(self):
        im = fill_rect(blank(), [0, 0, 96, H - 1], "#b0021b")  # 和 #d0021b 差 32
        loose = measure({"color_ratio": {"color": RED, "min": 0.25, "tol": 40}}, frame_of(im)).judges[0]
        tight = measure({"color_ratio": {"color": RED, "min": 0.25, "tol": 20}}, frame_of(im)).judges[0]
        assert loose.passed
        assert not tight.passed

    def test_multiple_colors_are_unioned(self):
        im = blank()
        fill_rect(im, [0, 0, 159, H - 1], RED)
        fill_rect(im, [160, 0, W - 1, H - 1], GREEN)
        judge = measure({"color_ratio": {"color": [RED, GREEN], "min": 0.9}}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.value == pytest.approx(1.0, abs=0.01)

    def test_max_upper_bound(self):
        im = fill_rect(blank(), [0, 0, 96, H - 1], RED)
        judge = measure({"color_ratio": {"color": RED, "min": 0.1, "max": 0.2}}, frame_of(im)).judges[0]
        assert not judge.passed
        assert "超过上限" in judge.head

    def test_min_is_required(self):
        with pytest.raises(PredicateSpecError) as exc:
            build_test({"color_ratio": {"color": RED}})
        assert "min" in str(exc.value)


# --------------------------------------------------------------------------
# glyph_runs —— 单字形 vs 多字文本
# --------------------------------------------------------------------------


class TestGlyphRuns:
    def test_single_arrow_is_one_run(self):
        im = draw_arrow(blank(), "left")
        judge = measure({"glyph_runs": 1}, frame_of(im)).judges[0]
        assert judge.value == 1
        assert judge.passed

    def test_two_char_text_is_two_runs(self, font60):
        im = draw_text(blank(size=(360, H)), "正确", font60)
        judge = measure({"glyph_runs": 1}, frame_of(im)).judges[0]
        assert judge.value == 2, judge.detail
        assert not judge.passed, "两个汉字被当成单个字形了"

    def test_three_char_text_is_three_runs(self, font60):
        im = draw_text(blank(size=(420, H)), "未反应", font60)
        judge = measure({"glyph_runs": 1}, frame_of(im)).judges[0]
        assert judge.value == 3, judge.detail
        assert not judge.passed

    def test_text_can_be_asserted_explicitly(self, font60):
        im = draw_text(blank(size=(360, H)), "正确", font60)
        judge = measure({"glyph_runs": ">=2"}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.detail["runs_raw"] >= 2

    def test_text_does_not_pass_as_arrow_geometry(self, font60):
        """完整复现 watcher 的三段式判定：列簇 + 宽高比 + 质心，文本必须全挡掉。"""
        im = draw_text(blank(size=(360, H)), "正确", font60)
        result = measure(
            {
                "glyph_runs": 1,
                "aspect": [1.1, 4.5],
                "centroid_dx": "<-0.02",
            },
            frame_of(im),
        )
        assert not result.hit
        assert "glyph_runs" in result.failed

    def test_one_pixel_gap_is_a_real_gap(self):
        """两个字之间是纯背景（连淡墨都没有）→ 不能合并。"""
        im = blank()
        fill_rect(im, [20, 20, 80, 100], "black")
        fill_rect(im, [82, 20, 140, 100], "black")  # 第 81 列是纯白
        judge = measure({"glyph_runs": 2}, frame_of(im)).judges[0]
        assert judge.value == 2, judge.detail
        assert judge.passed

    def test_antialias_seam_does_not_split_arrow(self):
        """字形内部被抗锯齿撕出 1px 缝（缝里有淡墨）→ 必须还是 1 簇。"""
        im = draw_arrow(blank(), "left")
        # 在箭头中间画一条浅灰竖线：比 strong 阈值亮、比 weak 阈值暗
        px = im.load()
        for y in range(25, 96):
            px[130, y] = (140, 140, 140)
        judge = measure({"glyph_runs": {"count": 1, "ink": "dark", "threshold": 110}}, frame_of(im)).judges[0]
        assert judge.value == 1, judge.detail
        assert judge.detail["runs_raw"] >= 2, "这条缝应该先被拆成 2 簇，再靠淡墨合回去"

    def test_empty_region_has_no_runs(self):
        judge = measure({"glyph_runs": 0}, frame_of(blank())).judges[0]
        assert judge.value == 0
        assert judge.passed

    def test_speck_below_min_area_is_ignored(self):
        im = fill_rect(blank(), [300, 100, 302, 102], "black")  # 9 个像素的噪点
        judge = measure({"glyph_runs": 0}, frame_of(im)).judges[0]
        assert judge.value == 0, judge.detail

    def test_caret_adds_a_run_unless_denoised(self):
        """文本光标是 1px 竖线：它会把列簇数凭空 +1。denoise 能把它抹掉。"""
        im = draw_caret(draw_arrow(blank(), "left"), 300, height=100)
        naive = measure({"glyph_runs": 2}, frame_of(im)).judges[0]
        assert naive.value == 2, naive.detail
        assert naive.passed

        fixed = measure({"glyph_runs": {"count": 1, "denoise": 2}}, frame_of(im)).judges[0]
        assert fixed.value == 1, fixed.detail
        assert fixed.passed

    def test_light_ink_on_dark_theme(self):
        """深色皮肤 + 白箭头：auto 阈值要能自己翻过来。"""
        im = draw_arrow(blank(bg="#1e1e1e"), "left", ink="white")
        judge = measure({"glyph_runs": 1}, frame_of(im)).judges[0]
        assert judge.passed, judge.detail

    def test_min_area_filters_thin_noise_but_not_glyph(self):
        im = draw_arrow(blank(), "left")
        judge = measure({"glyph_runs": {"count": 1, "min_area": 500}}, frame_of(im)).judges[0]
        assert judge.passed


# --------------------------------------------------------------------------
# aspect
# --------------------------------------------------------------------------


class TestAspect:
    def test_horizontal_arrow_is_wide(self):
        im = draw_arrow(blank(), "left")
        judge = measure({"aspect": [1.1, 4.5]}, frame_of(im)).judges[0]
        assert judge.passed
        assert 1.8 < judge.value < 2.6, judge.detail

    def test_square_box_is_rejected(self):
        im = fill_rect(blank(), [100, 20, 159, 79], "black")  # 60×60
        judge = measure({"aspect": [1.1, 4.5]}, frame_of(im)).judges[0]
        assert judge.value == pytest.approx(1.0, abs=0.05)
        assert not judge.passed

    def test_vertical_bar_is_rejected(self):
        im = fill_rect(blank(), [150, 10, 157, 109], "black")
        judge = measure({"aspect": [1.1, 4.5]}, frame_of(im)).judges[0]
        assert judge.value < 0.2
        assert not judge.passed

    def test_no_ink_reports_zero(self):
        judge = measure({"aspect": [1.1, 4.5]}, frame_of(blank())).judges[0]
        assert judge.value == 0
        assert not judge.passed
        assert judge.head == "没有墨迹"

    def test_single_number_is_rejected(self):
        with pytest.raises(PredicateSpecError):
            build_test({"aspect": 2.0})


# --------------------------------------------------------------------------
# centroid_dx
# --------------------------------------------------------------------------


class TestCentroid:
    def test_left_arrow_points_left(self):
        im = draw_arrow(blank(), "left")
        judge = measure({"centroid_dx": "<-0.02"}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.value < -0.02

    def test_right_arrow_points_right(self):
        im = draw_arrow(blank(), "right")
        judge = measure({"centroid_dx": ">0.02"}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.value > 0.02

    def test_left_arrow_fails_right_rule(self):
        im = draw_arrow(blank(), "left")
        judge = measure({"centroid_dx": ">0.02"}, frame_of(im)).judges[0]
        assert not judge.passed

    def test_plus_is_symmetric(self):
        im = draw_plus(blank())
        judge = measure({"centroid_dx": {"min": -0.02, "max": 0.02}}, frame_of(im)).judges[0]
        assert judge.passed, judge.detail
        assert abs(judge.value) < 0.02

    def test_symmetric_glyph_is_not_an_arrow(self):
        im = draw_plus(blank())
        result = measure({"centroid_dx": "<-0.02"}, frame_of(im))
        assert not result.hit

    def test_bare_number_needs_direction(self):
        with pytest.raises(PredicateSpecError) as exc:
            build_test({"centroid_dx": 0.02})
        assert "方向" in str(exc.value)

    def test_empty_region_fails(self):
        judge = measure({"centroid_dx": "<-0.02"}, frame_of(blank())).judges[0]
        assert not judge.passed


# --------------------------------------------------------------------------
# ink_ratio
# --------------------------------------------------------------------------


class TestInkRatio:
    def test_ink_coverage(self):
        im = fill_rect(blank(), [0, 0, 63, H - 1], "black")  # 20%
        judge = measure({"ink_ratio": {"min": 0.1}}, frame_of(im)).judges[0]
        assert judge.passed
        assert judge.value == pytest.approx(0.20, abs=0.03)

    def test_blank_has_no_ink(self):
        judge = measure({"ink_ratio": {"min": 0.1}}, frame_of(blank())).judges[0]
        assert judge.value == 0
        assert not judge.passed


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


class TestDiff:
    def test_first_frame_has_no_baseline(self):
        judge = measure({"diff": {"min": 0.05}}, frame_of(blank())).judges[0]
        assert judge.value == 0
        assert judge.detail["baseline"] == "first"
        assert not judge.passed

    def test_static_screen_stays_zero(self):
        test = build_test({"diff": {"min": 0.05}})
        test.measure(frame_of(blank()))
        judge = test.measure(frame_of(blank())).judges[0]
        assert judge.value == 0
        assert judge.detail["baseline"] == "prev"

    def test_ten_percent_change(self):
        test = build_test({"diff": {"min": 0.05}})
        test.measure(frame_of(blank()))
        changed = fill_rect(blank(), [0, 0, W - 1, 11], "black")  # 10% 行
        judge = test.measure(frame_of(changed)).judges[0]
        assert judge.passed
        assert judge.value == pytest.approx(0.10, abs=0.02)

    def test_change_below_threshold(self):
        test = build_test({"diff": {"min": 0.2}})
        test.measure(frame_of(blank()))
        changed = fill_rect(blank(), [0, 0, W - 1, 11], "black")
        judge = test.measure(frame_of(changed)).judges[0]
        assert not judge.passed

    def test_reference_image_baseline(self, tmp_path):
        ref_path = tmp_path / "ref.png"
        blank().save(ref_path)
        test = build_test({"diff": {"min": 0.05, "reference": str(ref_path)}})
        judge = test.measure(frame_of(fill_rect(blank(), [0, 0, W - 1, 11], "black"))).judges[0]
        assert judge.detail["baseline"] == "ref"
        assert judge.passed

    def test_reference_image_is_downsampled_like_the_frame(self, tmp_path):
        """底图必须和当前帧用同一个采样率，否则会比到"底图的左上角"（PITFALLS 5.4）。"""
        ref_path = tmp_path / "ref.png"
        blank().save(ref_path)
        test = build_test({"diff": {"min": 0.05, "reference": str(ref_path), "downsample": 2}})
        # 一模一样的两帧，变化率必须是 0，不能因为形状不齐被截成半张图去比
        judge = test.measure(frame_of(blank())).judges[0]
        assert judge.value == pytest.approx(0.0, abs=1e-6)

    def test_missing_reference_is_friendly(self, tmp_path):
        with pytest.raises(PredicateSpecError) as exc:
            build_test({"diff": {"min": 0.05, "reference": str(tmp_path / "nope.png")}})
        assert "参考图" in str(exc.value)


# --------------------------------------------------------------------------
# 组合 / 语法
# --------------------------------------------------------------------------


class TestBuildTest:
    def test_multiple_predicates_are_anded(self):
        im = draw_arrow(blank(), "left")
        result = measure({"glyph_runs": 1, "aspect": [1.1, 4.5], "centroid_dx": "<-0.02"}, frame_of(im))
        assert result.hit
        assert result.failed == []
        assert 0.0 <= result.level <= 1.0

    def test_any_mode(self):
        im = draw_arrow(blank(), "left")
        test = build_test({"glyph_runs": 3, "centroid_dx": "<-0.02"}, mode="any")
        assert test.measure(frame_of(im)).hit

    def test_all_mode_reports_which_failed(self):
        im = draw_arrow(blank(), "left")
        result = measure({"glyph_runs": 3, "centroid_dx": "<-0.02"}, frame_of(im))
        assert not result.hit
        assert result.failed == ["glyph_runs"]

    def test_typo_suggests_real_name(self):
        with pytest.raises(PredicateSpecError) as exc:
            build_test({"color_ratios": {"color": RED, "min": 0.2}})
        assert "color_ratio" in str(exc.value)
        assert "没有叫" in str(exc.value)

    def test_empty_test_is_rejected(self):
        with pytest.raises(PredicateSpecError):
            build_test({})

    def test_non_mapping_test_is_rejected(self):
        with pytest.raises(PredicateSpecError):
            build_test(["color_ratio"])  # type: ignore[arg-type]

    def test_unknown_field_in_predicate(self):
        with pytest.raises(PredicateSpecError):
            build_test({"glyph_runs": {"count": 1, "gap": 3}})

    def test_predicates_are_registered(self):
        assert {"color_ratio", "glyph_runs", "aspect", "centroid_dx", "diff", "ocr_keyword"} <= set(
            available_predicates()
        )


class TestCondAndColor:
    def test_parse_cond_forms(self):
        assert parse_cond("<-0.02").value == pytest.approx(-0.02)
        assert parse_cond("<-0.02").test(-0.1)
        assert parse_cond([1.1, 4.5]).test(2.2)
        assert not parse_cond([1.1, 4.5]).test(1.0)
        assert parse_cond({"min": 0.3}).test(0.4)
        assert not parse_cond({"min": 0.3}).test(0.2)
        assert parse_cond({"max": 0.3}).test(0.2)
        assert parse_cond("=0.5").test(0.5)

    def test_parse_cond_rejects_nonsense(self):
        with pytest.raises(PredicateSpecError):
            parse_cond("大约 0.3")
        with pytest.raises(PredicateSpecError):
            parse_cond([4.5, 1.1])
        with pytest.raises(PredicateSpecError):
            parse_cond(0.3)

    def test_parse_color_forms(self):
        assert parse_color("#d0021b") == (208, 2, 27)
        assert parse_color("d0021b") == (208, 2, 27)
        assert parse_color("#fff") == (255, 255, 255)
        assert parse_color([208, 2, 27]) == (208, 2, 27)
        with pytest.raises(PredicateSpecError):
            parse_color("#xyz")


class TestOcrOptional:
    def test_validate_only_does_not_import_ocr(self):
        """没装 rapidocr 时，只校验 YAML 不应该炸 —— 否则编辑器里改规则都改不动。"""
        test = build_test({"ocr_keyword": {"keyword": "危急值"}}, validate_only=True)
        assert test.predicates[0].keywords == ["危急值"]

    def test_real_build_requires_extra(self):
        pytest.importorskip  # noqa: B018
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            with pytest.raises(PredicateSpecError) as exc:
                build_test({"ocr_keyword": {"keyword": "危急值"}})
            assert "pixelbus[ocr]" in str(exc.value)
        else:
            pytest.skip("这台机器装了 rapidocr，跳过缺依赖的分支")
