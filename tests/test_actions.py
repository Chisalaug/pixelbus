"""动作分发：触发条件、cooldown、各类动作的调用边界。

真按键 / 真发 HTTP 都在测试里替身化：press 换假 Controller，webhook 打到本地起的
临时 HTTP 服务，exec 跑一个写文件的小命令。mqtt 没装依赖就断言"明确报缺依赖"，
不真连 broker。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import bgra_of, blank, fill_rect
from pixelbus.actions import ActionRunner, ActionError, kind_of
from pixelbus.engine import Engine, Event
from pixelbus.rules import ActionCfg, loads_rules


def red_frame():
    im = blank((64, 64))
    fill_rect(im, [0, 0, 63, 63], "#d0021b")
    return bgra_of(im)


def white_frame():
    return bgra_of(blank((64, 64)))


def rules_with(actions: str) -> str:
    return f"""
version: 1
fps: 10
regions:
  r: {{rect: [0, 0, 1, 1]}}
signals:
  red: {{region: r, test: {{color_ratio: {{color: "#d0021b", min: 0.25}}}}}}
states:
  hit: {{when: red, stable_frames: 1}}
actions:
{actions}
"""


# ---------------------------------------------------------------- 触发条件


def test_log_action_fires_on_state_appear():
    sink: list[dict] = []
    rules = loads_rules(rules_with('  - {"on": hit, "do": {log: "告警弹窗"}}'))
    runner = ActionRunner(rules, sink=lambda kind, record: sink.append(record), worker=False)
    engine = Engine(rules, runner=runner)
    engine.step(1.0, {"r": red_frame()})

    assert len(sink) == 1
    assert sink[0]["text"] == "告警弹窗"
    assert sink[0]["event"]["name"] == "hit"
    assert runner.status()["fired"] == 1 and runner.status()["ok"] == 1
    engine.close()


def test_when_disappear_only_fires_on_disappear():
    sink: list[str] = []
    rules = loads_rules(rules_with('  - {"on": hit, "when": disappear, "do": {log: "消失"}}'))
    runner = ActionRunner(rules, sink=lambda kind, record: sink.append(record["text"]), worker=False)
    engine = Engine(rules, runner=runner)

    engine.step(1.0, {"r": red_frame()})
    engine.step(1.1, {"r": white_frame()})

    assert sink == ["消失"], "appear 不该触发 when=disappear 的动作"
    engine.close()


def test_action_cooldown_blocks_second_fire():
    sink: list[str] = []
    rules = loads_rules(rules_with('  - {"on": hit, "cooldown": 30, "do": {log: "重复告警"}}'))
    runner = ActionRunner(rules, sink=lambda kind, record: sink.append(record["text"]), worker=False)

    runner.handle(Event("appear", "hit", "state", 10.0), sync=True)
    runner.handle(Event("appear", "hit", "state", 11.0), sync=True)
    assert sink == ["重复告警"], "11s 还在 30s cooldown 里，第二条必须被压住"
    runner.shutdown()


def test_action_matches_multiple_targets():
    sink: list[str] = []
    cfg = ActionCfg(on=("hit", "other"), do={"log": "共用"}, when="appear", text="共用")
    runner = ActionRunner(loads_rules(rules_with("")), sink=lambda kind, record: sink.append(record["text"]), worker=False)
    runner._actions = [cfg]
    runner.handle(Event("appear", "other", "state", 1.0), sync=True)
    assert sink == ["共用"]
    runner.shutdown()


# ---------------------------------------------------------------- press


def test_press_parses_combo_and_records_keys(monkeypatch):
    import pynput.keyboard as kbmod

    calls: list[tuple[str, object]] = []

    class FakeController:
        def press(self, k):
            calls.append(("press", k))

        def release(self, k):
            calls.append(("release", k))

    monkeypatch.setattr(kbmod, "Controller", FakeController)
    rules = loads_rules(rules_with('  - {"on": hit, "do": {press: "ctrl+s"}}'))
    runner = ActionRunner(rules, worker=False)

    runner.handle(Event("appear", "hit", "state", 1.0), sync=True)
    names = [str(k) for _, k in calls]
    assert names[0].startswith("Key.ctrl"), "先按住修饰键"
    assert "s" in names
    assert names[-1].startswith("Key.ctrl"), "最后松开修饰键"
    runner.shutdown()


# ---------------------------------------------------------------- webhook


class _CaptureHandler(BaseHTTPRequestHandler):
    body: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        type(self).body.append(json.loads(payload.decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_server():
    _CaptureHandler.body = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_webhook_posts_event_payload(http_server):
    port = http_server.server_address[1]
    rules = loads_rules(
        rules_with(f'  - {{"on": hit, "do": {{webhook: "http://127.0.0.1:{port}/hook", text: "危急值"}}}}')
    )
    runner = ActionRunner(rules, worker=False)
    runner.handle(Event("appear", "hit", "state", 1.0, level=1.0), sync=True)
    assert len(_CaptureHandler.body) == 1
    body = _CaptureHandler.body[0]
    assert body["text"] == "危急值"
    assert body["event"]["kind"] == "appear"
    assert body["action"]["on"] == ["hit"]
    assert runner.status()["ok"] == 1
    runner.shutdown()


def test_webhook_failure_is_reported_not_raised():
    rules = loads_rules(
        rules_with('  - {"on": hit, "do": {webhook: "http://127.0.0.1:1/hook"}}')
    )
    runner = ActionRunner(rules, worker=False)
    results = runner.handle(Event("appear", "hit", "state", 1.0), sync=True)
    assert results and results[0].ok is False
    assert "连不上" in results[0].message
    assert runner.status()["failed"] == 1
    runner.shutdown()


# ---------------------------------------------------------------- exec


def test_exec_runs_command_list(tmp_path):
    marker = tmp_path / "ran.txt"
    rules = loads_rules(rules_with(""))
    cfg = ActionCfg(
        on=("hit",),
        do={"exec": [sys.executable, "-c", f"open(r'{marker}', 'w').write('ok')"]},
        when="appear",
    )
    runner = ActionRunner(rules, base_dir=str(tmp_path), worker=False)
    runner._actions = [cfg]
    runner.handle(Event("appear", "hit", "state", 1.0), sync=True)
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert marker.exists()
    runner.shutdown()


# ---------------------------------------------------------------- mqtt / 缺依赖


def test_mqtt_missing_dependency_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    rules = loads_rules(rules_with('  - {"on": hit, "do": {mqtt: {topic: "pixelbus/alarm"}}}'))
    runner = ActionRunner(rules, worker=False)
    results = runner.handle(Event("appear", "hit", "state", 1.0), sync=True)
    msg = results[0].message
    assert "paho-mqtt" in msg
    assert "pixelbus[mqtt]" in msg
    runner.shutdown()


def test_unknown_kind_is_rejected():
    rules = loads_rules(rules_with('  - {"on": hit, "do": {log: "x"}}'))
    rules.actions[0].do = {"rocket": "x"}
    runner = ActionRunner(rules, worker=False)
    results = runner.handle(Event("appear", "hit", "state", 1.0), sync=True)
    assert results[0].ok is False
    runner.shutdown()


def test_kind_of_skips_text_and_payload():
    assert kind_of({"webhook": "u", "text": "x"}) == "webhook"
    assert kind_of({"payload": "x"}) == "log"


# ---------------------------------------------------------------- 引擎集成


def test_engine_default_runner_reports_actions_in_snapshot():
    rules = loads_rules(rules_with('  - {"on": hit, "do": {log: "ok"}}'))
    runner = ActionRunner(rules, worker=False)
    engine = Engine(rules, runner=runner)
    engine.step(1.0, {"r": red_frame()})
    snap = engine.snapshot()
    assert snap.actions["actions"] == 1
    assert snap.actions["fired"] >= 1
    engine.close()
