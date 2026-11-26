"""HTTP 口子：/health、/state、/events(SSE)、/reload。

用 ``TestClient``（httpx）跑：不真起 uvicorn、不碰真实屏幕，引擎全靠喂图驱动。
"""

from __future__ import annotations

import json
import time

import pytest

from conftest import bgra_of, blank, fill_rect
from pixelbus.engine import Engine
from pixelbus.rules import loads_rules
from pixelbus.server import Broker, create_app, sse_payload

SIMPLE = """
version: 1
fps: 10
regions:
  r: {rect: [0, 0, 1, 1]}
signals:
  red: {region: r, test: {color_ratio: {color: "#d0021b", min: 0.25}}}
states:
  hit: {when: red, stable_frames: 1}
actions:
  - {"on": hit, "do": {log: "命中"}}
"""


def red_frame():
    im = blank((64, 64))
    fill_rect(im, [0, 0, 63, 63], "#d0021b")
    return bgra_of(im)


def white_frame():
    return bgra_of(blank((64, 64)))


def make_engine(path=None):
    return Engine(loads_rules(SIMPLE, path=str(path) if path else None))


@pytest.fixture()
def rules_file(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(SIMPLE, encoding="utf-8")
    return path


@pytest.fixture()
def engine(rules_file):
    return make_engine(rules_file)


@pytest.fixture()
def client(engine):
    from fastapi.testclient import TestClient

    with TestClient(create_app(engine)) as c:
        yield c


# ---------------------------------------------------------------- 基础端点


def test_health_shape(client):
    body = client.get("/health").json()
    assert body["app"] == "pixelbus", "probe_owner 就靠这个字段认人，改名会破单实例提示"
    assert body["version"]
    assert body["uptime"] >= 0
    assert body["subscribers"] == 0
    assert body["rules"] == "rules.yaml"


def test_state_shape(client):
    body = client.get("/state").json()
    assert {"t", "frames", "signals", "states", "regions", "dropped"} <= set(body)
    assert body["signals"]["red"]["hit"] is False
    assert body["states"]["hit"]["active"] is False


def test_state_reflects_engine(client, engine):
    engine.step(1.0, {"r": red_frame()})
    body = client.get("/state").json()
    assert body["signals"]["red"]["hit"] is True
    assert body["states"]["hit"]["active"] is True
    assert body["signals"]["red"]["judges"][0]["value"] > 0.25


def test_index_lists_endpoints(client):
    body = client.get("/").json()
    assert body["app"] == "pixelbus"
    assert "/events" in body["endpoints"]["events"]


# ---------------------------------------------------------------- reload


def test_reload_ok(client, engine, rules_file):
    rules_file.write_text(SIMPLE.replace("min: 0.25", "min: 0.15"), encoding="utf-8")
    body = client.post("/reload").json()
    assert body["ok"] is True
    assert body["signals"] == 1
    assert engine.rules.signals["red"].test["color_ratio"]["min"] == 0.15


def test_reload_reports_issues(client, rules_file):
    rules_file.write_text(SIMPLE.replace("region: r", "region: rr"), encoding="utf-8")
    resp = client.post("/reload")
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["issues"], "得说清哪里错了，不能只给一句 failed"
    assert "rr" in body["issues"][0]["message"]
    # where 是点分路径，别被当成序列逐字符拆开（"s/i/g..."）
    assert body["issues"][0]["where"] == "signals.red.region"
    assert body["issues"][0]["line"] is not None


def test_reload_without_path_is_refused():
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_engine())) as c:  # path 为空
        resp = c.post("/reload")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------- SSE 编码


def test_sse_payload_format():
    text = sse_payload({"kind": "appear", "id": 7, "name": "red", "t": 1.0})
    assert text.endswith("\n\n")
    assert "event: appear" in text
    assert "id: 7" in text
    data = [ln for ln in text.splitlines() if ln.startswith("data: ")][0][6:]
    assert json.loads(data)["name"] == "red"


def test_sse_payload_keeps_chinese():
    text = sse_payload({"kind": "change", "id": 1, "detail": {"text": "危急值弹窗"}})
    assert "危急值" in text, "ensure_ascii 关掉，中文别给转成 \\uXXXX"


# ---------------------------------------------------------------- 扇出


def test_broker_fans_out_and_bumps_id():
    broker = Broker()
    q1, q2 = broker.subscribe(), broker.subscribe()
    assert broker.count == 2
    engine = make_engine()
    event = engine.step(1.0, {"r": red_frame()})[0]
    broker.publish(event)
    assert q1.get_nowait()["id"] == 1
    assert q2.get_nowait()["id"] == 1
    broker.publish(event)
    assert q1.get_nowait()["id"] == 2
    broker.unsubscribe(q1)
    broker.unsubscribe(q2)
    assert broker.count == 0


def test_broker_drops_oldest_when_client_lags():
    broker = Broker()
    q = broker.subscribe()
    event = make_engine().step(1.0, {"r": red_frame()})[0]
    for _ in range(600):  # 队列上限 512
        broker.publish(event)
    latest = None
    while not q.empty():
        latest = q.get_nowait()
    assert latest is not None and latest["id"] == 600, "慢客户端只该掉旧事件，不能把发布方卡住"


def test_broker_history_capped_by_replay():
    broker = Broker(replay=2)
    event = make_engine().step(1.0, {"r": red_frame()})[0]
    for _ in range(5):
        broker.publish(event)
    assert len(broker.history()) == 2
    assert broker.history()[-1]["id"] == 5


# ---------------------------------------------------------------- 真连一次流


def _read_until_data(it, deadline_s=3.0) -> list[str]:
    deadline = time.time() + deadline_s
    lines: list[str] = []
    while time.time() < deadline:
        line = next(it)
        lines.append(line)
        if line == "" and any(ln.startswith("data: ") for ln in lines):
            break
    return lines


def test_events_stream_live(client, engine):
    with client.stream("GET", "/events?replay=0") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        it = resp.iter_lines()
        assert next(it) == "retry: 3000"
        engine.step(1.0, {"r": red_frame()})
        joined = "\n".join(_read_until_data(it))
        assert "event: appear" in joined, "连上之后必须能收到活事件"


def test_events_filter_by_kind(client, engine):
    with client.stream("GET", "/events?kinds=disappear&replay=0") as resp:
        it = resp.iter_lines()
        next(it)  # retry
        engine.step(1.0, {"r": red_frame()})       # appear，应被过滤
        engine.step(1.1, {"r": white_frame()})     # disappear，这条要留下
        joined = "\n".join(_read_until_data(it))
        assert "event: disappear" in joined
        assert "event: appear" not in joined


def test_events_replay_sends_history(client, engine):
    engine.step(1.0, {"r": red_frame()})  # 先攒一条 appear 进历史
    with client.stream("GET", "/events?replay=5") as resp:
        it = resp.iter_lines()
        next(it)
        joined = "\n".join(_read_until_data(it))
        assert "appear" in joined, "刚连上应该先把历史补给你，不然调阈值时容易错过事件"


# ---------------------------------------------------------------- 真起 uvicorn


def test_server_thread_serves_and_stops():
    import socket
    import urllib.request
    import json as _json

    from pixelbus.server import ServerThread

    # 先占一个空闲端口，再放掉，拿端口号用（避免和 8787 撞车）
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    srv = ServerThread(create_app(make_engine()), port=port, log_level="error").start()
    try:
        with urllib.request.urlopen(f"{srv.url}/health", timeout=3) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        assert body["app"] == "pixelbus"
        assert body["pid"]
    finally:
        srv.stop()
    # 停了之后端口就该放开
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    probe.bind(("127.0.0.1", port))
    probe.close()


def test_server_thread_raises_when_port_is_taken():
    """端口被占时不能让主线程"假装跑起来了"——必须抛错，CLI 才好提示换端口。"""
    import socket

    from pixelbus.server import ServerThread

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    try:
        with pytest.raises(RuntimeError):
            ServerThread(create_app(make_engine()), port=port, log_level="error").start(timeout=1.0)
    finally:
        squatter.close()
