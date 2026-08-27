"""config.yaml durability — the app's ENTIRE state lives in this one file.

save() was a plain open("w"): truncate, then write. Roughly fifteen API endpoints do
load -> mutate -> save, and FastAPI runs sync endpoints on a threadpool, so the desktop
dashboard, the owner's bot and a buddy's bot can all be mid-sequence at once. Two hazards:
corruption (a crash between truncate and flush loses every watch) and lost updates.
"""
from __future__ import annotations
import threading
from pathlib import Path
import pytest
from web_watcher import config as C


def _cfg():
    return C.AppConfig.model_validate({})


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "config.yaml"
    C.save(_cfg(), p)
    assert p.exists()
    assert [f.name for f in tmp_path.iterdir()] == ["config.yaml"]   # temp cleaned up


def test_a_reader_never_sees_a_half_written_file(tmp_path):
    # os.replace is atomic, so the file is either the old one or the new one — never partial.
    p = tmp_path / "config.yaml"
    cfg = _cfg()
    cfg.watches = [C.Watch(name=f"W{i}", urls=["https://x"], instruction="x",
                           interval_minutes=30) for i in range(50)]
    C.save(cfg, p)
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                got = C.load(p)
                if len(got.watches) != 50:
                    bad.append(len(got.watches))
            except Exception as exc:
                bad.append(repr(exc))

    t = threading.Thread(target=reader, daemon=True); t.start()
    for _ in range(25):
        C.save(cfg, p)
    stop.set(); t.join(timeout=5)
    assert bad == []


def test_concurrent_writers_do_not_lose_each_others_watches(tmp_path):
    # The lost-update case: two threads each add a watch. Under config.mutate() both survive.
    p = tmp_path / "config.yaml"
    C.save(_cfg(), p)

    def add(name):
        for _ in range(10):
            with C.mutate(p) as cfg:
                cfg.watches.append(C.Watch(name=f"{name}{len(cfg.watches)}", urls=["https://x"],
                                           instruction="x", interval_minutes=30))

    ts = [threading.Thread(target=add, args=(n,)) for n in ("A", "B", "C")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(C.load(p).watches) == 30      # every single append survived


def test_mutate_does_not_save_when_the_body_raises(tmp_path):
    p = tmp_path / "config.yaml"
    C.save(_cfg(), p)
    with pytest.raises(ValueError):
        with C.mutate(p) as cfg:
            cfg.watches.append(C.Watch(name="ghost", urls=["https://x"],
                                       instruction="x", interval_minutes=30))
            raise ValueError("boom")
    assert C.load(p).watches == []           # the failed edit left nothing behind


def test_lock_is_reentrant_so_save_inside_mutate_works(tmp_path):
    p = tmp_path / "config.yaml"
    with C.mutate(p) as cfg:
        cfg.browser.headless = True
        C.save(cfg, p)                        # explicit save INSIDE mutate must not deadlock
    assert C.load(p).browser.headless is True
