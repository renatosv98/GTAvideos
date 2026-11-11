from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from clipper import config, render, scheduler, signals


def test_candidates_find_spikes_and_respect_gap():
    loud = np.full(3600, -40.0, dtype=np.float32)
    for t in (600, 650, 2000):
        loud[t - 3:t + 3] = -5.0
    cands = signals.find_candidates(loud, None)
    peaks = [c.peak for c in cands]
    assert any(abs(p - 2000) < 5 for p in peaks)
    # 600 and 650 are closer than min_gap: only one of them survives
    assert sum(1 for p in peaks if 590 < p < 660) == 1
    assert all(c.start < c.peak < c.end for c in cands)


def test_slots_are_on_interval_boundaries():
    t = datetime(2026, 11, 19, 13, 7, tzinfo=timezone.utc)
    s = scheduler.next_slot_after(t)
    local = s.astimezone(scheduler._tz())
    assert s > t
    assert local.minute == 0 and local.hour % scheduler.SETTINGS["schedule"]["interval_hours"] == 0
    assert s - t <= timedelta(hours=4)


def _creator(granted, source="email"):
    return config.Creator("x", "X", "@x", ["twitch.tv/x"], {"granted": granted, "source": source}, {})


def test_permission_gate(monkeypatch):
    monkeypatch.setattr(config, "load_creators", lambda: [_creator(False)])
    with pytest.raises(config.PermissionError_):
        config.resolve_creator({"channel_url": "https://twitch.tv/x"})
    monkeypatch.setattr(config, "load_creators", lambda: [_creator(True)])
    assert config.resolve_creator({"channel_url": "https://twitch.tv/x"}).id == "x"
    with pytest.raises(config.PermissionError_):
        config.resolve_creator({"channel_url": "https://twitch.tv/someone_else"})
    monkeypatch.setattr(config, "load_creators", lambda: [_creator(True, source="")])
    with pytest.raises(config.PermissionError_):
        config.resolve_creator({"channel_url": "https://twitch.tv/x"})


def test_ass_captions(tmp_path):
    words = [{"w": w, "s": 100 + i * 0.4, "e": 100.3 + i * 0.4} for i, w in enumerate("no way he just did that bro".split())]
    out = tmp_path / "s.ass"
    render.write_ass(words, 100.0, out, 1080)
    text = out.read_text()
    assert "NO WAY HE" in text and "\\pos(540,1080)" in text
    assert text.count("Dialogue:") == 3


def test_jump_cuts_remap_words():
    from clipper.render import plan_segments, remap_words
    segs = plan_segments(10, 40, [[30, 40], [10, 20], [25, 25.2]])  # unsorted, one too short
    assert segs == [(10, 20), (30, 40)]
    words = [{"w": "a", "s": 12, "e": 13}, {"w": "cut", "s": 22, "e": 23}, {"w": "b", "s": 31, "e": 32}]
    out = remap_words(words, segs)
    assert [w["w"] for w in out] == ["a", "b"]
    assert out[1]["s"] == 11  # 31 - 30 + 10s of first segment


def test_reprioritize_puts_most_viral_first(tmp_path, monkeypatch):
    import sqlite3
    from clipper import db, scheduler
    conn = sqlite3.connect(tmp_path / "t.db"); conn.row_factory = sqlite3.Row; conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO streams (id, url, creator_id) VALUES (1, 'u', 'x')")
    for cid, slot, score in [(1, "2026-11-20T00:00:00+00:00", 30), (2, "2026-11-20T04:00:00+00:00", 80),
                             (3, "2026-11-20T08:00:00+00:00", 55)]:
        conn.execute("INSERT INTO clips (id, stream_id, start, end, score, status, slot_at) VALUES (?,1,0,1,?,'approved',?)",
                     (cid, score, slot))
    scheduler.reprioritize(conn)
    order = [r["id"] for r in conn.execute("SELECT id FROM clips ORDER BY slot_at")]
    assert order == [2, 3, 1]
