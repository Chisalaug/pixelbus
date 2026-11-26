"""规则的加载与报错。

重点不是"能读"，而是"读错时说人话"：带行号、带字段路径、带修改建议。
"""

from __future__ import annotations

import pytest

from pixelbus.rules import (
    RulesError,
    WhenError,
    load_rules,
    loads_rules,
    parse_when,
)

EXAMPLE = """
version: 1
fps: 10
regions:
  alarm: {monitor: primary, rect: [0.72, 0.08, 0.26, 0.10]}
signals:
  red_alarm: {region: alarm, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
  arrow_left: {region: alarm, test: {glyph_runs: 1, aspect: [1.1, 4.5], centroid_dx: "<-0.02"}}
states:
  alarm_on: {when: red_alarm, stable_frames: 3}
actions:
  - {on: alarm_on, do: {webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/x", text: "危急值弹窗"}}
"""


def expect_error(text: str, *needles: str) -> RulesError:
    with pytest.raises(RulesError) as exc:
        loads_rules(text)
    msg = str(exc.value)
    for needle in needles:
        assert needle in msg, msg
    return exc.value


class TestHappyPath:
    def test_contract_example_loads(self):
        rules = loads_rules(EXAMPLE)
        assert rules.version == 1
        assert rules.fps == 10
        assert set(rules.regions) == {"alarm"}
        assert set(rules.signals) == {"red_alarm", "arrow_left"}
        assert rules.states["alarm_on"].stable_frames == 3
        assert rules.actions[0].do["webhook"].startswith("https://")
        assert rules.warnings == []

    def test_signal_builds_runnable_test(self):
        rules = loads_rules(EXAMPLE)
        test = rules.signals["red_alarm"].build()
        assert len(test.predicates) == 1

    def test_defaults(self):
        rules = loads_rules(
            """
version: 1
regions: {r: {rect: [0, 0, 1, 1]}}
signals: {s: {region: r, test: {ink_ratio: {min: 0.01}}}}
states: {on: {when: s}}
"""
        )
        assert rules.fps == 10
        assert rules.server.port == 8787
        assert rules.states["on"].stable_frames == 1
        assert rules.actions == []

    def test_region_can_use_absolute_pixels_and_monitor_index(self):
        rules = loads_rules(
            """
version: 1
regions:
  local: {monitor: 1, pixels: [10, 20, 300, 100]}
  whole: {monitor: primary, rect: [0, 0, 1, 1]}
signals: {s: {region: local, test: {ink_ratio: {min: 0.01}}}}
states: {on: {when: s}}
"""
        )
        assert rules.regions["local"].spec.pixels == (10, 20, 300, 100)
        assert rules.regions["local"].spec.monitor == 1

    def test_yaml_file_on_disk(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text(EXAMPLE, encoding="utf-8")
        rules = load_rules(str(path))
        assert rules.path == str(path)


class TestYamlErrors:
    def test_empty_file(self):
        expect_error("", "文件是空的")

    def test_tab_indent_is_called_out(self):
        expect_error("version: 1\n\tregions: {}\n", "Tab")

    def test_broken_syntax_has_line_number(self):
        err = expect_error("version: 1\nregions:\n  alarm: [\n", "rules.yaml:")
        assert err.issues[0].line is not None


class TestSchemaErrors:
    def test_version_must_be_one(self):
        expect_error("version: 2\nregions: {}\n", "version 必须是 1")

    def test_unknown_top_level_key(self):
        expect_error(EXAMPLE + "\nfpss: 12\n", "fpss", "fps")

    def test_fps_out_of_range(self):
        expect_error(EXAMPLE.replace("fps: 10", "fps: 120"), "超出合理范围")

    def test_region_rect_wrong_length(self):
        expect_error(EXAMPLE.replace("[0.72, 0.08, 0.26, 0.10]", "[0.72, 0.08, 0.26]"), "必须是 4 个数")

    def test_region_rect_out_of_range(self):
        expect_error(EXAMPLE.replace("[0.72, 0.08, 0.26, 0.10]", "[0.72, 0.08, 2.6, 0.10]"), "超出相对坐标系")

    def test_region_needs_geometry(self):
        expect_error(
            EXAMPLE.replace("rect: [0.72, 0.08, 0.26, 0.10]", "foo: [0.72, 0.08, 0.26, 0.10]"),
            "rect 或 pixels",
        )

    def test_signal_unknown_region(self):
        expect_error(EXAMPLE.replace("region: alarm, test: {color_ratio", "region: alarmm, test: {color_ratio"), "不存在")

    def test_signal_missing_test(self):
        expect_error(
            EXAMPLE.replace(
                "red_alarm: {region: alarm, test: {color_ratio: {color: \"#d0021b\", min: 0.25}}}",
                "red_alarm: {region: alarm}",
            ),
            "没写 test",
        )

    def test_unknown_predicate_name(self):
        expect_error(EXAMPLE.replace("color_ratio", "color_ratios"), "没有叫")

    def test_predicate_param_error_is_attributed(self):
        expect_error(EXAMPLE.replace("min: 0.25", "minimum: 0.25"), "min")

    def test_state_references_unknown_signal(self):
        expect_error(EXAMPLE.replace("when: red_alarm", "when: red_alarmm"), "red_alarmm")

    def test_state_stable_frames_type(self):
        expect_error(EXAMPLE.replace("stable_frames: 3", 'stable_frames: "3"'), "应该是整数")

    def test_state_stable_frames_range(self):
        expect_error(EXAMPLE.replace("stable_frames: 3", "stable_frames: 999"), "1~300")

    def test_action_on_unknown_state(self):
        expect_error(EXAMPLE.replace("on: alarm_on", "on: alarm_onn"), "不是已定义的 state")

    def test_action_unknown_kind(self):
        expect_error(EXAMPLE.replace("webhook:", "webook:"), "webook")

    def test_webhook_must_be_http(self):
        expect_error(
            EXAMPLE.replace('"https://open.feishu.cn/open-apis/bot/v2/hook/x"', '"feishu://hook"'),
            "http(s)",
        )

    def test_press_needs_keys(self):
        expect_error(EXAMPLE.replace("webhook: \"https://open.feishu.cn/open-apis/bot/v2/hook/x\"", "press: \"\""), "按键")

    def test_all_errors_are_reported_at_once(self):
        broken = EXAMPLE.replace("fps: 10", "fps: 120").replace("stable_frames: 3", 'stable_frames: "3"')
        msg = str(expect_error(broken, "超出合理范围"))
        assert "2 处问题" in msg

    def test_warning_for_unused_region(self):
        rules = loads_rules(
            """
version: 1
regions:
  alarm: {rect: [0, 0, 0.3, 0.1]}
  unused: {rect: [0, 0, 0.3, 0.1]}
signals: {s: {region: alarm, test: {ink_ratio: {min: 0.01}}}}
states: {on: {when: s}}
"""
        )
        assert any("unused" in w.msg for w in rules.warnings)


class TestWhen:
    def test_single_name(self):
        expr = parse_when("alarm_on")
        assert expr.op == "sig" and expr.signals() == {"alarm_on"}

    def test_list_is_and(self):
        expr = parse_when(["a", "b"])
        assert expr.op == "and"
        assert expr.signals() == {"a", "b"}

    def test_and_or_precedence(self):
        expr = parse_when("a | b & c")
        assert expr.op == "or"
        assert expr.kids[1].op == "and"

    def test_not_and_parens(self):
        expr = parse_when("!(a | b) & c")
        assert expr.op == "and"
        assert expr.kids[0].op == "not"
        assert expr.kids[0].kids[0].op == "or"

    def test_roundtrip_str(self):
        assert str(parse_when("a & (b | c)")) == "a & (b | c)"

    def test_unclosed_paren(self):
        with pytest.raises(WhenError):
            parse_when("(a & b")

    def test_empty(self):
        with pytest.raises(WhenError):
            parse_when("")

    def test_garbage(self):
        with pytest.raises(WhenError):
            parse_when("a & & b")

    def test_wrong_type(self):
        with pytest.raises(WhenError):
            parse_when(3)  # type: ignore[arg-type]

    def test_expression_in_yaml_checks_names(self):
        expect_error(EXAMPLE.replace("when: red_alarm", 'when: "red_alarm & missing"'), "missing")
