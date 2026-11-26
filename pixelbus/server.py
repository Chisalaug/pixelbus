"""HTTP + SSE：把引擎的事件变成"能订阅的东西"。

四个口子：

- ``GET /health``  —— 存活探测。``probe_owner`` 靠这里的 ``app == "pixelbus"``
  判断"锁端口是不是被自己人占了"，所以这几个字段不能随便改。
- ``GET /state``   —— 当前完整快照（每个 signal 的分数、每个 state 的成立与否）。
- ``GET /events``  —— SSE 事件流，``event:`` 就是事件类型，``data:`` 是 JSON。
- ``POST /reload`` —— 重载 YAML。

**线程模型**：引擎跑在自己的线程里，uvicorn 跑在另一个线程（各自有各自的 event loop）。
所以不能直接把事件塞进 ``asyncio.Queue``，必须用 ``loop.call_soon_threadsafe`` 跨线程投递。
``TestClient`` 里跑的话没有独立 loop，这时退化成"直接 put"，测试才好写。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .engine import Engine, Event
from .rules import RulesError

__all__ = ["create_app", "Broker", "ServerThread", "sse_payload"]

HEARTBEAT = 15.0  # 秒。SSE 超过这个时间没数据就发一行注释，免得被代理掐断


# --------------------------------------------------------------------------
# 扇出
# --------------------------------------------------------------------------


class Broker:
    """一条引擎 → N 个订阅者的广播通道。

    每个订阅者一个 ``asyncio.Queue``。引擎线程 push，SSE 协程 await get。
    """

    def __init__(self, replay: int = 0) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0
        self.replay = replay
        self._history: list[dict] = []

    # -- 生命周期 ---------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._queues)

    # -- 发布 -------------------------------------------------------------
    def payload(self, event: Event) -> dict:
        self._seq += 1
        body = event.to_dict()
        body["id"] = self._seq
        return body

    def publish(self, event: Event) -> None:
        body = self.payload(event)
        self._history.append(body)
        if len(self._history) > max(1, self.replay or 1):
            del self._history[: -max(1, self.replay or 1)]
        with self._lock:
            queues = list(self._queues)
        loop = self._loop
        for q in queues:
            try:
                if loop is not None:
                    loop.call_soon_threadsafe(_safe_put, q, body)
                else:
                    _safe_put(q, body)
            except RuntimeError:
                # loop 已经关了（服务在退出），这条丢掉就行
                pass

    def history(self) -> list[dict]:
        return list(self._history)


def _safe_put(q: asyncio.Queue, body: dict) -> None:
    try:
        q.put_nowait(body)
    except asyncio.QueueFull:
        # 客户端消费不过来：丢最老的一条，保证它拿到的是最新状态
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(body)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def sse_payload(body: Mapping[str, Any]) -> str:
    """一条 SSE 记录。``event:`` 用事件类型，方便浏览器 ``addEventListener("appear")``。"""
    kind = str(body.get("kind") or "message")
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if body.get("id") is not None:
        lines.append(f"id: {body['id']}")
    lines.append(f"event: {kind}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


def _parse_kinds(text: str | None) -> set[str] | None:
    if not text:
        return None
    kinds = {k.strip() for k in text.split(",") if k.strip()}
    return kinds or None


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


def create_app(engine: Engine, *, broker: Broker | None = None, started_at: float | None = None) -> FastAPI:
    """把引擎包成 FastAPI app。``engine`` 不用先 start()，绑定订阅即可。"""
    app = FastAPI(title="pixelbus", version=__version__, docs_url="/docs", redoc_url=None)
    br = broker or Broker(replay=20)
    app.state.engine = engine
    app.state.broker = br
    app.state.started_at = time.time() if started_at is None else started_at
    app.state.unsubscribe = engine.add_subscriber(br.publish)

    @app.on_event("startup")
    async def _on_startup() -> None:
        br.bind_loop(asyncio.get_running_loop())

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        br.bind_loop(None)
        try:
            app.state.unsubscribe()
        except Exception:
            pass

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "app": "pixelbus",
            "version": __version__,
            "endpoints": {
                "state": "/state",
                "events": "/events（SSE）",
                "reload": "POST /reload",
                "health": "/health",
            },
            "rules": os.path.basename(engine.rules.path) if engine.rules.path else "",
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        snap = engine.snapshot()
        return {
            "app": "pixelbus",
            "version": __version__,
            "rules": os.path.basename(engine.rules.path) if engine.rules.path else "",
            "uptime": round(time.time() - app.state.started_at, 1),
            "fps": snap.fps,
            "frames": snap.frames,
            "dropped": snap.dropped,
            "grab_errors": engine.grab_errors,
            "subscribers": br.count,
            "pid": os.getpid(),
        }

    @app.get("/state")
    def state() -> dict[str, Any]:
        return engine.snapshot().to_dict()

    @app.get("/events")
    async def events(
        request: Request,
        kinds: str | None = Query(None, description="只看这些事件类型，逗号分隔，例如 appear,change"),
        name: str | None = Query(None, description="只看这个名字（signal 或 state 名）"),
        replay: int = Query(3, ge=0, le=50, description="连上时先补发最近几条历史事件"),
    ) -> StreamingResponse:
        want = _parse_kinds(kinds)
        q = br.subscribe()

        def keep(body: Mapping[str, Any]) -> bool:
            if want is not None and str(body.get("kind")) not in want:
                return False
            if name and str(body.get("name")) != name:
                return False
            return True

        async def stream() -> Any:
            try:
                yield "retry: 3000\n\n"
                for old in br.history()[-replay:] if replay else []:
                    if keep(old):
                        yield sse_payload(old)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        body = await asyncio.wait_for(q.get(), timeout=HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if keep(body):
                        yield sse_payload(body)
            finally:
                br.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/reload")
    def reload() -> Any:
        from .rules import load_rules

        path = engine.rules.path
        if not path:
            return JSONResponse({"ok": False, "message": "这份规则不是从文件加载的，没法重载"}, status_code=400)
        try:
            rules = load_rules(path)
        except FileNotFoundError:
            return JSONResponse({"ok": False, "message": f"找不到规则文件：{path}"}, status_code=400)
        except RulesError as exc:
            return JSONResponse(
                {"ok": False, "message": str(exc), "issues": [_issue_dict(i) for i in exc.issues]},
                status_code=400,
            )
        engine.set_rules(rules)
        return {
            "ok": True,
            "rules": os.path.basename(path),
            "fps": rules.fps,
            "signals": len(rules.signals),
            "states": len(rules.states),
            "actions": len(rules.actions),
            "warnings": [_issue_dict(w) for w in rules.warnings],
        }

    return app


def _issue_dict(issue: Any) -> dict[str, Any]:
    """``Issue`` 转成 JSON。没有 filename 也行，前端只关心 where / msg / hint。"""
    where = getattr(issue, "where", "") or ""
    if not isinstance(where, str):
        where = "/".join(str(p) for p in where if p != "")
    return {
        "where": where,
        "message": getattr(issue, "msg", str(issue)),
        "hint": getattr(issue, "hint", "") or "",
        "line": getattr(issue, "line", None),
        "severity": getattr(issue, "severity", "error"),
    }


# --------------------------------------------------------------------------
# 在后台线程里跑 uvicorn
# --------------------------------------------------------------------------


class ServerThread:
    """把 uvicorn 塞进一个线程：主线程还要跑引擎和终端输出。

    用法::

        srv = ServerThread(app, host="127.0.0.1", port=8787).start()
        ...
        srv.stop()
    """

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 8787, log_level: str = "warning") -> None:
        self.app = app
        self.host = host
        self.port = int(port)
        self.log_level = log_level
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 3.0) -> "ServerThread":
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, name="pixelbus-http", daemon=True)
        self._thread.start()
        self._wait_ready(timeout)
        return self

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:  # 端口被占时 uvicorn 会 sys.exit(1)，那是 SystemExit，不是 Exception
            self.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    def _wait_ready(self, timeout: float = 3.0) -> None:
        """等 uvicorn 真的把端口 serve 起来（``Server.started`` 为 True）。

        超时必须抛错 —— 早先这里是"连一下端口、连上就算好"，两个问题：
        端口被占时 uvicorn 只在 stderr 打一行 ERROR 然后 sys.exit(1)，主线程照样
        打印"events: http://..."假装跑起来了；而别人开的监听 socket 也能被连上。
        现在只认 uvicorn 自己的 ``started`` 标志。
        """
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.error:
                raise RuntimeError(self._explain())
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError(self._explain() if self.error
                           else f"{self.host}:{self.port} 在 {timeout:g}s 内没起来（端口可能被别的程序占着）")

    def _explain(self) -> str:
        """把 uvicorn 的 ``SystemExit: 3`` 翻译成人话。"""
        reason = self.error or ""
        if reason.startswith(("SystemExit", "ExitCode")):
            return f"{self.host}:{self.port} 起不来（端口大概被别的程序占了）"
        return reason

    def stop(self, timeout: float = 3.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
