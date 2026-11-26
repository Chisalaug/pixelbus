"""单实例锁：占一个本地端口。

为什么用端口而不是 pid 文件：
- pid 文件在进程被强杀后会留下，判断"这个 pid 还活着吗"在 Windows 上不可靠；
- 端口没了进程就跟着没了，操作系统替我们做清理；
- 顺手还能当"探针"：端口占着就往 HTTP 上问一句 /health，告诉用户**是谁**在跑。

Windows 上要显式 SO_EXCLUSIVEADDRUSE：默认的 SO_REUSEADDR 允许两个进程绑同一个端口，
锁就形同虚设 —— 这是踩过的坑。
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.request

__all__ = ["PortLock", "probe_owner"]


class PortLock:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        port = int(port)
        if not (0 < port < 65536):
            raise ValueError(f"端口号 {port} 不合法（应该是 1~65535）")
        self.port = port
        self.host = host
        self._sock: socket.socket | None = None

    def acquire(self) -> bool:
        if self._sock is not None:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # 不加这个，Windows 下 SO_REUSEADDR 会允许抢占已在监听的端口
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(1)
        except OSError:
            sock.close()
            return False
        self._sock = sock
        return True

    def release(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "PortLock":
        if not self.acquire():
            raise OSError(f"端口 {self.host}:{self.port} 已被占用")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def probe_owner(host: str = "127.0.0.1", port: int = 8787, timeout: float = 0.4) -> dict | None:
    """锁端口被占时，去 HTTP 端口问一句"你是谁"，好把提示写得具体点。"""
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if isinstance(body, dict) and body.get("app") == "pixelbus":
        return body
    return None
