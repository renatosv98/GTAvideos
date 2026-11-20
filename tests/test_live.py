from clipper.download import part_url, split_part
from clipper.live import gta_spans, next_chunk

CFG = {"first_chunk_min": 45, "chunk_min": 60, "max_chunk_min": 90}


def test_part_url_roundtrip():
    u = part_url("https://www.twitch.tv/videos/1", 167, 2867.5)
    assert split_part(u) == ("https://www.twitch.tv/videos/1", (167.0, 2867.0))
    assert split_part("https://www.twitch.tv/videos/1") == ("https://www.twitch.tv/videos/1", None)


def test_in_progress_chapter_runs_to_now():
    info = {"chapters": [{"start_time": 0, "end_time": 167, "title": "Just Chatting"},
                         {"start_time": 167, "end_time": 167, "title": "Grand Theft Auto VI"}]}
    assert gta_spans(info, 3000) == [(167.0, 3000.0)]


def test_chunks_while_live_and_after():
    spans = [(167.0, 99999.0)]
    assert next_chunk(spans, 0, 167 + 40 * 60, 0, True, CFG) is None          # 40 min: wait
    assert next_chunk(spans, 0, 167 + 46 * 60, 0, True, CFG) == (167.0, 167 + 46 * 60)
    a = 167 + 46 * 60
    assert next_chunk(spans, a, a + 50 * 60, 1, True, CFG) is None            # later chunks need 60 min
    assert next_chunk(spans, a, a + 61 * 60, 1, True, CFG) == (a, a + 61 * 60)
    assert next_chunk(spans, a, a + 300 * 60, 1, True, CFG) == (a, a + 90 * 60)  # capped when behind
    assert next_chunk([(167.0, a + 10 * 60)], a, a + 10 * 60, 1, False, CFG) == (a, a + 600)  # ended: remainder
    assert next_chunk([(167.0, a + 60)], a, a + 60, 1, False, CFG) is None     # under 5 min left: done


def test_live_gta6_chapter_counts_as_gta6():
    from clipper.download import is_gta6
    info = {"duration": 3000, "chapters": [{"start_time": 0, "end_time": 167, "title": "Just Chatting"},
                                           {"start_time": 167, "end_time": 167, "title": "Grand Theft Auto VI"}]}
    assert is_gta6(info)
    info["duration"] = 600
    assert not is_gta6(info)


def test_compilation_plan_and_time():
    from datetime import datetime, timezone
    from clipper.compilation import MAX_S, _publish_time, plan
    ms = [{"id": i, "a": i * 300.0, "b": i * 300.0 + 60, "score": 100 - i, "title": f"t{i}", "caption": "",
           "creator_id": "rubius", "stream_title": ""} for i in range(30)]
    segs = plan(ms)
    assert sum(s["b"] - s["a"] for s in segs) <= MAX_S
    assert [s["a"] for s in segs] == sorted(s["a"] for s in segs)  # stream order
    assert segs[0]["ids"] == [0]  # best moments chosen
    over = [{**ms[0], "id": 1, "a": 10.0, "b": 70.0}, {**ms[0], "id": 2, "a": 72.0, "b": 120.0}]
    assert len(plan(over)) == 1  # overlapping moments merged
    t = _publish_time(datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc))
    assert t.hour == 18 and t.day == 19


def test_compilation_thumbnail_on_real_video(tmp_path):
    """The thumbnail step runs real ffmpeg on a real (tiny) video, incl. a seek past the end."""
    from clipper import compilation
    v = tmp_path / "v.mp4"
    compilation._ff("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=4", "-pix_fmt", "yuv420p", str(v))
    txt = tmp_path / "t.txt"
    txt.write_text("DÍA 1 EN GTA 6")
    out = tmp_path / "thumb.jpg"
    compilation.thumbnail(v, 8.5, txt, out)
    assert out.stat().st_size > 5000


def test_compilation_upload_is_locked(monkeypatch, tmp_path):
    """Two callers (build + publisher) can't upload the same compilation at once."""
    import fcntl
    from clipper import compilation
    monkeypatch.setattr(compilation, "DATA", tmp_path)
    monkeypatch.setattr(compilation, "_ensure_table", lambda: None)
    monkeypatch.setattr(compilation, "_upload_ready_locked", lambda: ["uploaded"])
    with open(tmp_path / "compilation_upload.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert compilation.upload_ready() == ["compilation upload already running elsewhere"]
    assert compilation.upload_ready() == ["uploaded"]


H = 3600.0
MARATHON = {"chapters": [  # Peereira-style 24h stream
    {"start_time": 0, "end_time": 3 * H, "title": "Grand Theft Auto VI"},
    {"start_time": 3 * H, "end_time": 5 * H, "title": "Just Chatting"},
    {"start_time": 5 * H, "end_time": 8 * H, "title": "Grand Theft Auto V"},
    {"start_time": 8 * H, "end_time": 8 * H, "title": "Grand Theft Auto VI"}]}  # in progress


def test_spans_are_gta6_only():
    assert gta_spans(MARATHON, 10 * H) == [(0.0, 3 * H), (8 * H, 10 * H)]


def test_chunks_never_cross_into_other_games():
    edge = 10 * H - 120
    got, done, n = [], 0.0, 0
    while (c := next_chunk(gta_spans(MARATHON, 10 * H), done, edge, n, True, CFG)):
        got.append(c)
        done, n = c[1], n + 1
    assert got == [(0.0, 1.5 * H), (1.5 * H, 3 * H), (8 * H, 9.5 * H)]
    for a, b in got:  # every chunk sits inside a GTA VI chapter
        assert (0 <= a and b <= 3 * H) or (8 * H <= a and b <= 10 * H)


def test_live_watcher_takes_turns(monkeypatch, tmp_path):
    """Creators are served one chunk each per round, not one creator's whole backlog first."""
    from types import SimpleNamespace
    from clipper import live
    order = []
    left = {"a": 3, "b": 1, "c": 2}

    def one(creator, raw, st):
        if left[creator.id]:
            left[creator.id] -= 1
            order.append(creator.id)
            return True
        return False
    monkeypatch.setattr(live, "_one_chunk", one)
    monkeypatch.setattr(live, "load_creators", lambda: [SimpleNamespace(id=x) for x in "abc"])
    monkeypatch.setattr(live, "_watchdog", lambda: None)
    monkeypatch.setattr(live, "_load", lambda f: {"creators": []})
    monkeypatch.setattr(live, "DATA", tmp_path)
    monkeypatch.setattr(live, "ACTIVE", tmp_path / "a")
    live.watch()
    assert order == ["a", "b", "c", "a", "c", "a"]


def test_live_playlist_marked_finished():
    """A live playlist is rewritten as finished (absolute URLs, ENDLIST) so ffmpeg downloads it at full speed."""
    from clipper.download import finished_playlist
    live = ('#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-MAP:URI="init-0.mp4"\n'
            '#EXTINF:10.000,\n0.mp4\n#EXTINF:10.000,\n1.mp4\n')
    out = finished_playlist(live, "https://cdn.example/abc/1080p60/index-dvr.m3u8")
    lines = out.splitlines()
    assert lines[0] == "#EXTM3U" and "#EXT-X-PLAYLIST-TYPE:VOD" in lines and lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" not in lines
    assert 'URI="https://cdn.example/abc/1080p60/init-0.mp4"' in out
    assert "https://cdn.example/abc/1080p60/1.mp4" in lines
    assert out.count("#EXT-X-ENDLIST") == 1


def test_interrupted_download_never_falls_back_to_whole_stream(tmp_path, monkeypatch):
    """A half-written file from a crash must not count as 'downloaded' (the retry would then skip the range
    and fetch the entire stream)."""
    from clipper import download
    d = tmp_path / "streams" / "v1_p0"
    d.mkdir(parents=True)
    (d / "source.mp4.part").write_text("x")
    (d / "source.f399.mp4.ytdl").write_text("x")
    assert download._have_source(d) is None
    download._clear_partial(d)
    assert not any(d.iterdir())


def test_cut_off_download_is_not_treated_as_done(tmp_path):
    """A source.mp4 cut off by a network drop (Peereira part 20, 20 Nov) is deleted and re-downloaded,
    never handed to the transcriber."""
    from clipper import compilation, download
    (tmp_path / "source.mp4").write_bytes(b"\x00" * 48)
    assert download._have_source(tmp_path) is None and not (tmp_path / "source.mp4").exists()
    good = tmp_path / "source.mp4"
    compilation._ff("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=15", "-pix_fmt", "yuv420p",
                    "-b:v", "2M", str(good))
    assert download._have_source(tmp_path) == good


def test_playlist_slice_takes_only_needed_segments():
    from clipper.download import slice_playlist
    pl = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:0\n" + "".join(
        f"#EXTINF:10.000,\nhttps://cdn/x/{i}.ts\n" for i in range(20)) + "#EXT-X-ENDLIST\n"
    out, off = slice_playlist(pl, 45, 72)
    segs = [ln for ln in out.splitlines() if ln.startswith("https://")]
    assert segs == ["https://cdn/x/4.ts", "https://cdn/x/5.ts", "https://cdn/x/6.ts", "https://cdn/x/7.ts"]
    assert off == 40.0 and out.splitlines()[-1] == "#EXT-X-ENDLIST" and "MEDIA-SEQUENCE" not in out
    import pytest
    with pytest.raises(ValueError):
        slice_playlist(pl, 500, 600)
