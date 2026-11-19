"""Guards against posting mistakes. These run before every commit (git pre-commit hook) and the same checks
run inside the publisher before every real run (scheduler.selftest)."""
import sqlite3
from pathlib import Path

import pytest

from clipper import db, scheduler


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch copy of the live database + an empty ledger: tests never touch real data."""
    copy = tmp_path / "t.db"
    with db.connect() as src:
        dst = sqlite3.connect(copy)
        src.backup(dst)
        dst.close()

    def connect():
        c = sqlite3.connect(copy)
        c.row_factory = sqlite3.Row
        return c

    class Ctx:
        def __enter__(self):
            self.c = connect()
            return self.c

        def __exit__(self, *a):
            self.c.commit()
            self.c.close()

    monkeypatch.setattr(db, "connect", lambda: Ctx())
    monkeypatch.setattr(scheduler, "_ledger_path", lambda: tmp_path / "posted.jsonl")
    monkeypatch.setattr(scheduler, "_alarm", lambda msg: None)
    return connect


def test_selftest_passes_on_live_data():
    assert scheduler.selftest() == []


def test_record_all_statuses_and_counts_failures(scratch):
    with scratch() as c:
        cid = c.execute("SELECT id FROM clips LIMIT 1").fetchone()[0]
        for st in ("failed", "failed", "failed"):
            scheduler._record(c, cid, "t", st, error="x")
        assert c.execute("SELECT attempts FROM uploads WHERE clip_id=? AND platform='t'", (cid,)).fetchone()[0] == 3
        scheduler._record(c, cid, "t", "published", "rid", "url")
        r = c.execute("SELECT status, remote_id FROM uploads WHERE clip_id=? AND platform='t'", (cid,)).fetchone()
        assert tuple(r) == ("published", "rid")


def test_ledger_blocks_second_post_even_if_database_lost_it(scratch):
    scheduler._ledger_add(999, "instagram", "m1")
    assert (999, "instagram") in scheduler._ledger()
    # TikTok drafts that expired are voided so they can be sent again
    scheduler._ledger_add(999, "tiktok", "p1")
    scheduler._ledger_add(999, "tiktok", "p1", void=True, why="expired")
    assert (999, "tiktok") not in scheduler._ledger()


def test_posted_stops_the_run_when_database_breaks(scratch, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("row value misused")
    monkeypatch.setattr(scheduler, "_record", broken)
    with pytest.raises(scheduler.SafetyStop):
        scheduler._posted(999, "instagram", "m1", "published")
    assert (999, "instagram") in scheduler._ledger()  # ledger was written first: never re-posted


def test_publish_due_never_reposts_a_ledger_clip(scratch, monkeypatch):
    """Simulate a run where every platform 'succeeds': clips in the ledger are never uploaded again."""
    from clipper.uploaders import instagram, tiktok, youtube
    calls = []
    monkeypatch.setattr(instagram, "upload", lambda *a, **k: calls.append("ig") or "m")
    monkeypatch.setattr(instagram, "permalink", lambda *a: None)
    monkeypatch.setattr(tiktok, "upload", lambda *a, **k: calls.append("tt") or "p")
    monkeypatch.setattr(youtube, "upload", lambda *a, **k: calls.append("yt") or "v")
    with scratch() as c:
        c.execute("DELETE FROM uploads")
        ids = [r[0] for r in c.execute("SELECT id FROM clips WHERE status IN ('approved','published') "
                                       "AND slot_at IS NOT NULL")]
    for i in ids:
        for p in ("youtube", "tiktok", "instagram"):
            scheduler._ledger_add(i, p, "x")
    scheduler._publish_due(dry_run=False)
    assert calls == []


def test_youtube_metadata():
    from clipper.config import load_creators
    cr = {c.id: c for c in load_creators()}
    with db.connect() as c:
        rows = c.execute("SELECT clips.*, streams.creator_id, COALESCE(streams.gta6,0) AS prio FROM clips "
                         "JOIN streams ON streams.id=clips.stream_id WHERE clips.status IN ('approved','published')"
                         ).fetchall()
    for r in rows:
        tags = scheduler._yt_hashtags(r, cr.get(r["creator_id"]))
        assert 3 <= len(tags) <= 5 and "#Shorts" not in tags
        if not scheduler._is_gta6(r):
            assert not any("GTA6" in t.upper() for t in tags)
            assert not any("GTA 6" in t or "VI" in t.split()[-1:] for t in scheduler._yt_tags(r)[:5])
        assert len(scheduler._yt_title(r)) <= 100


def test_instagram_duplicate_key_ignores_spacing():
    from clipper.uploaders import instagram
    assert instagram._key("Hola  mundo\n#GTA") == instagram._key("hola mundo #gta")


def test_notifications_escape_safely():
    """macOS alerts must use ensure_ascii=False (escaped \\u sequences break AppleScript)."""
    for p in Path(scheduler.__file__).parent.rglob("*.py"):
        for line in p.read_text().splitlines():
            if "display notification" in line and "json.dumps(" in line:
                assert "ensure_ascii=False" in line, f"{p.name}: {line.strip()[:80]}"


def test_gta6_hold_blocks_older_clips(scratch, monkeypatch):
    """While a GTA 6 clip waits for a platform, no non-GTA 6 clip is posted there."""
    from clipper.uploaders import instagram, tiktok, youtube
    posted = []
    monkeypatch.setattr(instagram, "upload", lambda *a, **k: "m")
    monkeypatch.setattr(instagram, "permalink", lambda *a: None)
    monkeypatch.setattr(instagram, "already_posted", lambda *a, **k: None)
    monkeypatch.setattr(tiktok, "upload", lambda *a, **k: "p")
    monkeypatch.setattr(youtube, "upload", lambda *a, **k: "v")
    monkeypatch.setattr(scheduler, "_tiktok_inbox_count", lambda: 0)
    monkeypatch.setattr(scheduler, "_ig_gap_ok", lambda: True)
    monkeypatch.setattr(scheduler, "_ig_paused", lambda: False)
    real = scheduler._posted
    monkeypatch.setattr(scheduler, "_posted", lambda cid, p, *a, **k: (posted.append((cid, p)), real(cid, p, *a, **k)))
    scheduler._publish_due(dry_run=False)
    with scratch() as c:
        g6 = {r[0] for r in c.execute("SELECT c.id FROM clips c JOIN streams s ON s.id=c.stream_id WHERE s.gta6=1")}
        waiting = {(r[0], p) for r in c.execute("SELECT c.id FROM clips c JOIN streams s ON s.id=c.stream_id "
                                                 "WHERE s.gta6=1 AND c.status='approved'") for p in ("youtube", "tiktok", "instagram")}
    for cid, p in posted:
        if cid not in g6:
            assert not any(w[1] == p and w not in posted and (w[0], p) not in scheduler._ledger() for w in waiting), (cid, p)


def test_youtube_backoff_after_upload_limit(tmp_path, monkeypatch):
    """After uploadLimitExceeded, YouTube is not retried for an hour; one alert per episode; success clears it."""
    import json
    from datetime import datetime, timedelta
    alerts = []
    monkeypatch.setattr(scheduler, "_yt_cap_file", lambda: tmp_path / "yt.json")
    monkeypatch.setattr(scheduler, "_alarm_ok", alerts.append)
    assert not scheduler._yt_capped()
    scheduler._yt_cap_hit()
    scheduler._yt_cap_hit()
    assert scheduler._yt_capped() and len(alerts) == 1
    old = (datetime.now(scheduler.UTC) - timedelta(minutes=61)).isoformat()
    (tmp_path / "yt.json").write_text(json.dumps({"since": old, "last": old}))
    assert not scheduler._yt_capped()
    scheduler._yt_cap_clear()
    assert not (tmp_path / "yt.json").exists() and len(alerts) == 2
