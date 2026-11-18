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
