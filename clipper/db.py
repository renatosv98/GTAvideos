from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DATA

DB_PATH = DATA / "clipper.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    creator_id TEXT NOT NULL,
    title TEXT,
    duration REAL,
    video_path TEXT,
    chat_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',     -- new | downloaded | analyzed | rendered | failed
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    start REAL NOT NULL,
    end REAL NOT NULL,
    score REAL,
    title TEXT,
    caption TEXT,
    hashtags TEXT,
    reason TEXT,
    risk_notes TEXT,
    words_json TEXT,
    path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | rendered | approved | rejected | published | failed
    review_notes TEXT,
    slot_at TEXT,                            -- ISO UTC publish slot
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,
    remote_id TEXT,
    url TEXT,
    status TEXT NOT NULL,                    -- scheduled | published | failed
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (clip_id, platform)
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
