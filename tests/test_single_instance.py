"""Single-instance guard: exactly ONE Web Watcher may run at a time.

The bug this prevents was seen on a real machine: restarts that didn't fully exit stacked up SIX
live copies, each polling the same Telegram bot (so an outdated build answered the user) and each
driving its own browser. See web_watcher/single_instance.py.
"""

from __future__ import annotations

import socket

from web_watcher.single_instance import InstanceLock, port_in_use


def test_second_lock_is_refused_while_the_first_is_held(tmp_path):
    p = tmp_path / "web_watcher.lock"
    first = InstanceLock(p)
    assert first.acquire() is True
    second = InstanceLock(p)
    assert second.acquire() is False        # the second copy must exit
    first.release()


def test_lock_is_reusable_after_release(tmp_path):
    """A restart releases the lock, so the relaunched copy can take it — otherwise an update
    would leave the app unable to start at all."""
    p = tmp_path / "web_watcher.lock"
    a = InstanceLock(p)
    assert a.acquire() is True
    a.release()
    b = InstanceLock(p)
    assert b.acquire() is True              # not wedged shut
    b.release()


def test_lock_never_blocks_startup_on_an_unexpected_error(tmp_path, monkeypatch):
    """Fail OPEN: a guard must never be the reason the app won't start."""
    lock = InstanceLock(tmp_path / "nope" / "web_watcher.lock")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lock.acquire() is True


def test_port_in_use_detects_a_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert port_in_use(port) is True
    # Once closed, the port is free again (a bound-then-released port must not look occupied).
    assert port_in_use(port) is False
