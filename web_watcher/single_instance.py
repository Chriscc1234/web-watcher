r"""
Single-instance guard — exactly ONE Web Watcher may run at a time.

Why this exists: a restart used to be able to leave the previous copy alive (a hung browser or
the web server kept the process up), so the launcher's new copy stacked on top of it. Several
copies then ran at once — each polling the SAME Telegram bot, so an OUTDATED build could answer
the user's messages, and each driving its own browser (thrashing one GPU). That was diagnosed on
a real machine with SIX live instances, one a day old.

Two independent checks, because either alone has a hole:
  1. AN OS-LEVEL LOCK FILE — an exclusively-locked file in the data dir. Held for the life of the
     process and released by the OS even on a hard kill or power loss, so it can never go stale.
     This is what makes the guarantee real, and it closes the start-at-the-same-moment race that
     a port probe cannot.
  2. A PORT PROBE — is something already serving the dashboard port? Catches an older build that
     predates the lock file, and answers "is a real app there?" rather than "did someone leave a
     file behind?".

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  port_in_use          ~L45   Is something already serving the dashboard port?
  InstanceLock.acquire ~L70   Take the exclusive lock (False = another copy holds it)
  InstanceLock.release ~L110  Give it up (also happens automatically on exit)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

LOCK_NAME = "web_watcher.lock"


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True when something is already accepting connections on this port — i.e. another copy of
    the app is already serving the dashboard."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


class InstanceLock:
    """An exclusive, OS-held lock on a file in the data dir.

    The lock lives with the PROCESS, not the file's contents: if this process dies for any reason
    (crash, kill, power cut) the OS drops it, so a stale file can never wedge the app shut — the
    failure mode that would be much worse than the bug this prevents.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            from web_watcher import paths
            path = paths.data_dir() / LOCK_NAME
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        """Take the lock. False means ANOTHER instance already holds it — the caller should exit.
        Any unexpected error returns True (fail OPEN): a guard must never be the reason the app
        won't start."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False                      # someone else holds it — we are the second copy
            # Record who holds it; purely informational (the OS lock is the real guarantee).
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()
            except Exception:
                pass
            self._fh = fh
            return True
        except Exception as exc:
            log.debug("instance lock unavailable (%s) — continuing", exc)
            return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def __enter__(self) -> "InstanceLock":
        self.acquired = self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
