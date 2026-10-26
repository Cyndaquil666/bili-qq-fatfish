"""SQLite-backed memory shared by both lives of the fish.

One database = one entity.  Videos watched on Bilibili and lines said in QQ
land in the same place, so the fish can remember "I saw something about that
yesterday" without any cross-process glue.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    bvid TEXT PRIMARY KEY,
    title TEXT,
    up_name TEXT,
    up_mid INTEGER,
    duration INTEGER,
    pubdate INTEGER,
    first_seen INTEGER,
    last_seen INTEGER,
    watch_count INTEGER DEFAULT 0,
    last_decision TEXT,
    last_score REAL,
    topics TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT,
    title TEXT,
    summary TEXT,
    reaction TEXT,
    score REAL,
    topics TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_day ON episodes(day, channel);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,
    channel TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    result TEXT,
    dry_run INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_actions_day ON actions(day, action);
CREATE TABLE IF NOT EXISTS interests (
    topic TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    hits INTEGER DEFAULT 0,
    last_ts INTEGER
);
CREATE TABLE IF NOT EXISTS peers (
    channel TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    name TEXT,
    familiarity REAL DEFAULT 0.2,
    last_ts INTEGER,
    notes TEXT,
    PRIMARY KEY (channel, peer_id)
);
CREATE TABLE IF NOT EXISTS group_msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT,
    name TEXT,
    text TEXT,
    direct_at INTEGER DEFAULT 0,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_group_msgs ON group_msgs(group_id, ts);
CREATE TABLE IF NOT EXISTS journal (
    day TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bili_comments (
    rpid INTEGER PRIMARY KEY,
    bvid TEXT,
    aid INTEGER,
    text TEXT,
    created_ts INTEGER,
    checked_ts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS self_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    why TEXT,
    tags TEXT,
    importance REAL DEFAULT 0.5,
    source TEXT DEFAULT 'reflect'
);
CREATE INDEX IF NOT EXISTS idx_self_notes_day ON self_notes(day, kind);
"""


class Memory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Small, additive migrations for databases created by older builds."""
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(group_msgs)")}
        if "direct_at" not in cols:
            self.db.execute("ALTER TABLE group_msgs ADD COLUMN direct_at INTEGER DEFAULT 0")
        if "meta" not in cols:
            self.db.execute("ALTER TABLE group_msgs ADD COLUMN meta TEXT")

    def close(self) -> None:
        with self._lock:
            self.db.commit()
            self.db.close()

    # -------------------------------------------------------- self notes
    def add_self_note(
        self,
        kind: str,
        text: str,
        why: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        source: str = "reflect",
    ) -> int:
        ts = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO self_notes(ts,day,kind,text,why,tags,importance,source) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    ts,
                    time.strftime("%Y-%m-%d", time.localtime(ts)),
                    str(kind or "note"),
                    str(text or "").strip(),
                    str(why or "").strip(),
                    json.dumps(tags or [], ensure_ascii=False),
                    float(importance),
                    str(source or "reflect"),
                ),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def self_notes(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM self_notes"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = [dict(r) for r in self.db.execute(sql, args)]
        for row in rows:
            try:
                row["tags"] = json.loads(row.get("tags") or "[]")
            except Exception:
                row["tags"] = []
        return rows

    def recent_self_notes(self, limit: int = 5) -> list[dict]:
        return self.self_notes(limit=limit)

    # ---------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return row["v"]

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False, default=str), int(time.time())),
            )
            self.db.commit()

    # ------------------------------------------------------------ videos
    def upsert_video(self, bvid: str, **fields) -> None:
        now = int(time.time())
        topics = fields.pop("topics", None)
        meta = fields.pop("meta", None)
        with self._lock:
            row = self.db.execute("SELECT bvid FROM videos WHERE bvid=?", (bvid,)).fetchone()
            if row:
                sets, vals = ["last_seen=?"], [now]
                for k, v in fields.items():
                    sets.append(f"{k}=?")
                    vals.append(v)
                if topics is not None:
                    sets.append("topics=?")
                    vals.append(json.dumps(topics, ensure_ascii=False))
                if meta is not None:
                    sets.append("meta=?")
                    vals.append(json.dumps(meta, ensure_ascii=False, default=str))
                vals.append(bvid)
                self.db.execute(f"UPDATE videos SET {', '.join(sets)} WHERE bvid=?", vals)
            else:
                self.db.execute(
                    "INSERT INTO videos(bvid,title,up_name,up_mid,duration,pubdate,first_seen,last_seen,topics,meta) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        bvid,
                        fields.get("title", ""),
                        fields.get("up_name", ""),
                        fields.get("up_mid", 0),
                        fields.get("duration", 0),
                        fields.get("pubdate", 0),
                        now,
                        now,
                        json.dumps(topics or [], ensure_ascii=False),
                        json.dumps(meta or {}, ensure_ascii=False, default=str),
                    ),
                )
            self.db.commit()

    def add_my_comment(self, bvid: str, aid: int, rpid: int, text: str) -> None:
        if not rpid:
            return
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO bili_comments(rpid,bvid,aid,text,created_ts,checked_ts) "
                "VALUES(?,?,?,?,?,COALESCE((SELECT checked_ts FROM bili_comments WHERE rpid=?),0))",
                (int(rpid), bvid, int(aid), text, int(time.time()), int(rpid)),
            )
            self.db.commit()

    def my_comments(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bili_comments ORDER BY created_ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_comment_checked(self, rpid: int, ts: int | None = None) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE bili_comments SET checked_ts=? WHERE rpid=?",
                (int(ts if ts is not None else time.time()), int(rpid)),
            )
            self.db.commit()

    def mark_watched(self, bvid: str, decision: str, score: float, topics: list[str]) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE videos SET watch_count=watch_count+1, last_decision=?, last_score=?, "
                "last_seen=?, topics=? WHERE bvid=?",
                (decision, score, int(time.time()), json.dumps(topics, ensure_ascii=False), bvid),
            )
            self.db.commit()

    def seen_within(self, bvid: str, days: int = 30) -> bool:
        cutoff = int(time.time()) - days * 86400
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM videos WHERE bvid=? AND last_seen>=?", (bvid, cutoff)
            ).fetchone()
        return row is not None

    def watched_count_today(self, day: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(DISTINCT ref) c FROM episodes WHERE day=? AND channel='bilibili' AND kind='watch'",
                (day,),
            ).fetchone()
        return int(row["c"] if row else 0)

    # ---------------------------------------------------------- episodes
    def add_episode(
        self,
        channel: str,
        kind: str,
        summary: str,
        title: str = "",
        ref: str = "",
        reaction: str = "",
        score: float = 0.0,
        topics: list[str] | None = None,
        meta: dict | None = None,
        day: str | None = None,
    ) -> int:
        now = int(time.time())
        day = day or time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO episodes(ts,day,channel,kind,ref,title,summary,reaction,score,topics,meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    day,
                    channel,
                    kind,
                    ref,
                    title,
                    summary,
                    reaction,
                    score,
                    json.dumps(topics or [], ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                ),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def recent_episodes(self, limit: int = 20, channel: str | None = None, days: int = 14) -> list[dict]:
        cutoff = int(time.time()) - days * 86400
        sql = "SELECT * FROM episodes WHERE ts>=?"
        args: list[Any] = [cutoff]
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [self._episode_row(r) for r in rows]

    def episodes_for_day(self, day: str, channel: str | None = None) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE day=?"
        args: list[Any] = [day]
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [self._episode_row(r) for r in rows]

    @staticmethod
    def _episode_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("topics", "meta"):
            try:
                d[key] = json.loads(d.get(key) or "null") or ([] if key == "topics" else {})
            except Exception:
                d[key] = [] if key == "topics" else {}
        return d

    # ----------------------------------------------------------- actions
    def record_action(
        self,
        channel: str,
        action: str,
        target: str = "",
        detail: str = "",
        result: str = "ok",
        dry_run: bool = False,
        day: str | None = None,
    ) -> None:
        now = int(time.time())
        day = day or time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            self.db.execute(
                "INSERT INTO actions(ts,day,channel,action,target,detail,result,dry_run) VALUES(?,?,?,?,?,?,?,?)",
                (now, day, channel, action, target, detail[:1000], result, 1 if dry_run else 0),
            )
            self.db.commit()

    def count_actions(
        self,
        action: str,
        since_ts: int | None = None,
        channel: str | None = None,
        target: str | None = None,
        day: str | None = None,
        include_dry: bool = False,
    ) -> int:
        sql = "SELECT COUNT(*) c FROM actions WHERE action=?"
        args: list[Any] = [action]
        if day:
            sql += " AND day=?"
            args.append(day)
        if since_ts:
            sql += " AND ts>=?"
            args.append(since_ts)
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        if target:
            sql += " AND target=?"
            args.append(target)
        if not include_dry:
            sql += " AND dry_run=0 AND result='ok'"
        with self._lock:
            row = self.db.execute(sql, args).fetchone()
        return int(row["c"] if row else 0)

    def last_action_ts(self, action: str, target: str | None = None) -> int:
        sql = "SELECT MAX(ts) t FROM actions WHERE action=? AND dry_run=0 AND result='ok'"
        args: list[Any] = [action]
        if target:
            sql += " AND target=?"
            args.append(target)
        with self._lock:
            row = self.db.execute(sql, args).fetchone()
        return int(row["t"] or 0) if row else 0

    def actions_for_day(self, day: str, include_dry: bool = True) -> list[dict]:
        sql = "SELECT * FROM actions WHERE day=?"
        args: list[Any] = [day]
        if not include_dry:
            sql += " AND dry_run=0"
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------- interests
    def ensure_interest(self, topic: str, weight: float) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO interests(topic,weight,hits,last_ts) VALUES(?,?,0,?) "
                "ON CONFLICT(topic) DO NOTHING",
                (topic, weight, int(time.time())),
            )
            self.db.commit()

    def bump_interest(self, topic: str, delta: float, hit: bool = True) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE interests SET weight=MAX(0.05, MIN(1.0, weight+?)), hits=hits+?, last_ts=? WHERE topic=?",
                (delta, 1 if hit else 0, int(time.time()), topic),
            )
            self.db.commit()

    def interests(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM interests ORDER BY weight DESC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- peers
    def touch_peer(self, channel: str, peer_id: str, name: str = "", delta: float = 0.0) -> None:
        now = int(time.time())
        with self._lock:
            self.db.execute(
                "INSERT INTO peers(channel,peer_id,name,familiarity,last_ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(channel,peer_id) DO UPDATE SET name=excluded.name, "
                "familiarity=MAX(0.0, MIN(1.0, peers.familiarity+?)), last_ts=excluded.last_ts",
                (channel, str(peer_id), name, min(1.0, max(0.0, 0.2 + delta)), now, delta),
            )
            self.db.commit()

    def peers(self, channel: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM peers WHERE channel=? ORDER BY last_ts DESC LIMIT ?", (channel, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------ group buffer
    def add_group_msg(
        self,
        group_id: str,
        user_id: str,
        name: str,
        text: str,
        ts: int | None = None,
        direct_at: bool = False,
        meta: dict | None = None,
    ) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO group_msgs(ts,group_id,user_id,name,text,direct_at,meta) VALUES(?,?,?,?,?,?,?)",
                (
                    ts or int(time.time()),
                    str(group_id),
                    str(user_id),
                    name,
                    text,
                    1 if direct_at else 0,
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                ),
            )
            self.db.commit()

    def recent_group_msgs(self, group_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM group_msgs WHERE group_id=? ORDER BY id DESC LIMIT ?", (str(group_id), limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_group_msgs(self, days: int = 7) -> None:
        cutoff = int(time.time()) - days * 86400
        with self._lock:
            self.db.execute("DELETE FROM group_msgs WHERE ts<?", (cutoff,))
            self.db.commit()

    # ----------------------------------------------------------- journal
    def set_journal(self, day: str, text: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO journal(day,text,ts) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET text=excluded.text, ts=excluded.ts",
                (day, text, int(time.time())),
            )
            self.db.commit()

    def get_journal(self, day: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT text FROM journal WHERE day=?", (day,)).fetchone()
        return row["text"] if row else ""

    def days_with_activity(self, limit: int = 14) -> list[str]:
        with self._lock:
            rows = self.db.execute(
                "SELECT day, COUNT(*) c FROM episodes GROUP BY day ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["day"] for r in rows]
