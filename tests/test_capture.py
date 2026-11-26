"""区域解析的纯数学部分：不需要真的抓屏，CI 上也能跑。

真实抓屏只做一次冒烟（拿不到屏幕就 skip）。
"""

from __future__ import annotations

import pytest

from pixelbus.capture import (
    Grabber,
    RegionError,
    RegionSpec,
    parse_region,
    rect_to_bbox,
    resolve_monitor,
)

MON_PRIMARY = {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True}
MON_ALL = {"left": 0, "top": 0, "width": 3840, "height": 1080}
MON_SECOND = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
MON_LEFT_NEG = {"left": -1920, "top": -200, "width": 1920, "height": 1080}


class TestParseRegion:
    def test_relative_rect(self):
        spec = parse_region("alarm", {"monitor": "primary", "rect": [0.72, 0.08, 0.26, 0.10]})
        assert spec.rect == (0.72, 0.08, 0.26, 0.10)
        assert spec.pixels is None

    def test_absolute_pixels(self):
        spec = parse_region("bar", {"pixels": [10, 20, 300, 100]})
        assert spec.pixels == (10, 20, 300, 100)

    def test_rect_and_pixels_conflict(self):
        with pytest.raises(RegionError) as exc:
            parse_region("x", {"rect": [0, 0, 1, 1], "pixels": [0, 0, 1, 1]})
        assert "二选一" in str(exc.value)

    def test_missing_geometry(self):
        with pytest.raises(RegionError):
            parse_region("x", {"monitor": "primary"})

    def test_rect_wrong_length(self):
        with pytest.raises(RegionError) as exc:
            parse_region("x", {"rect": [0, 0, 1]})
        assert "4 个数" in str(exc.value)

    def test_rect_out_of_range(self):
        with pytest.raises(RegionError) as exc:
            parse_region("x", {"rect": [0, 0, 5, 1]})
        assert "0~1" in str(exc.value)

    def test_zero_size_is_rejected(self):
        with pytest.raises(RegionError) as exc:
            parse_region("x", {"rect": [0.5, 0.5, 0, 0]})
        assert "空矩形" in str(exc.value)

    def test_not_a_mapping(self):
        with pytest.raises(RegionError):
            parse_region("x", [0, 0, 1, 1])

    def test_default_monitor_is_primary(self):
        spec = parse_region("x", {"rect": [0, 0, 1, 1]})
        assert spec.monitor == "primary"


MONITORS = [MON_ALL, MON_PRIMARY, MON_SECOND]


class TestResolveMonitor:
    def test_primary_picks_is_primary(self):
        assert resolve_monitor("primary", MONITORS)["width"] == 1920
        assert resolve_monitor("PRIMARY", MONITORS)["left"] == 0

    def test_all_is_index_zero(self):
        assert resolve_monitor("all", MONITORS)["width"] == 3840

    def test_integer_index(self):
        assert resolve_monitor(2, MONITORS)["left"] == 1920

    def test_index_out_of_range(self):
        with pytest.raises(RegionError) as exc:
            resolve_monitor(5, MONITORS)
        assert "超出范围" in str(exc.value)

    def test_garbage_name(self):
        with pytest.raises(RegionError) as exc:
            resolve_monitor("second-screen", MONITORS)
        assert "无法识别" in str(exc.value)


class TestBBox:
    def test_relative_math(self):
        spec = RegionSpec("alarm", rect=(0.72, 0.08, 0.26, 0.10))
        bbox = rect_to_bbox(spec, MON_PRIMARY)
        assert bbox == {"left": 1382, "top": 86, "width": 499, "height": 108}

    def test_second_monitor_is_offset(self):
        spec = RegionSpec("x", monitor=2, rect=(0, 0, 0.5, 0.5))
        bbox = rect_to_bbox(spec, MON_SECOND)
        assert bbox["left"] == 1920
        assert bbox["width"] == 960

    def test_negative_origin_is_respected(self):
        """副屏在主屏左边时 left 是负数，别把它 abs 掉。"""
        spec = RegionSpec("x", rect=(0, 0, 0.5, 0.5))
        bbox = rect_to_bbox(spec, MON_LEFT_NEG)
        assert bbox["left"] == -1920
        assert bbox["top"] == -200

    def test_slight_overflow_is_clipped(self):
        spec = RegionSpec("x", rect=(0.9, 0.9, 0.2, 0.2))
        bbox = rect_to_bbox(spec, MON_PRIMARY)
        assert bbox["left"] + bbox["width"] <= 1920
        assert bbox["top"] + bbox["height"] <= 1080

    def test_absolute_pixels_pass_through(self):
        spec = RegionSpec("x", pixels=(10, 20, 300, 100))
        bbox = rect_to_bbox(spec, MON_PRIMARY)
        assert bbox == {"left": 10, "top": 20, "width": 300, "height": 100}


class TestLiveGrab:
    """真抓一帧，确认 mss + DPI 在这台机器上能用。"""

    def test_grab_monitor(self):
        try:
            with Grabber() as g:
                monitors = g.monitors
                assert monitors
                arr = g.grab_monitor("primary")
        except Exception as exc:  # 无头 CI / 没权限
            pytest.skip(f"抓屏不可用：{exc}")
        assert arr.shape[2] == 4
        assert arr.dtype.name == "uint8"
        assert arr.shape[0] > 0 and arr.shape[1] > 0

    def test_grab_region_matches_bbox(self):
        try:
            with Grabber() as g:
                spec = RegionSpec("probe", rect=(0.5, 0.5, 0.2, 0.2))
                bbox = rect_to_bbox(spec, g.monitor("primary"))
                arr = g.grab(spec)
        except Exception as exc:
            pytest.skip(f"抓屏不可用：{exc}")
        assert arr.shape[0] == bbox["height"]
        assert arr.shape[1] == bbox["width"]
