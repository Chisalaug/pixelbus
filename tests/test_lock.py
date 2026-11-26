"""单实例锁：占一个本地端口，第二个进程必须拿不到。"""

from __future__ import annotations

import socket

import pytest

from pixelbus.lock import PortLock, probe_owner


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_acquire_and_release():
    port = _free_port()
    lock = PortLock(port)
    assert lock.acquire() is True
    lock.release()
    # 放掉之后必须还能再占一次，否则重启就起不来了
    lock2 = PortLock(port)
    assert lock2.acquire() is True
    lock2.release()


def test_second_lock_fails():
    port = _free_port()
    first = PortLock(port)
    assert first.acquire() is True
    try:
        second = PortLock(port)
        assert second.acquire() is False, "同一个端口不该被两份 pixelbus 同时占住"
    finally:
        first.release()


def test_context_manager_releases():
    port = _free_port()
    with PortLock(port) as lock:
        assert lock.acquire() is True  # 已经占上了，再调一次也是 True
        other = PortLock(port)
        assert other.acquire() is False
    with PortLock(port) as ok_again:  # 退出 with 就释放了
        assert ok_again.acquire() is True


def test_probe_owner_returns_none_when_nothing_there():
    port = _free_port()
    assert probe_owner("127.0.0.1", port, timeout=0.2) is None


def test_probe_owner_ignores_foreign_listener():
    """端口上有人，但不是 pixelbus —— 不能报"已经有 pixelbus 在跑"。"""
    port = _free_port()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        assert probe_owner("127.0.0.1", port, timeout=0.2) is None
    finally:
        srv.close()


@pytest.mark.parametrize("port", [-1, 0, 70000])
def test_bad_port_rejected(port: int):
    with pytest.raises(ValueError):
        PortLock(port)
