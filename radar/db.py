"""SQLite 저장소. 기사 원문은 저장하지 않는다(저작권) - 제목/요약문/링크/메타데이터만."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    source TEXT,
    published TEXT,          -- YYYY-MM-DD
    snippet TEXT,
    lang TEXT,               -- ko | en
    origin TEXT,             -- google_ko | google_foreign | naver | bigkinds
    norm_hash TEXT,
    is_dup INTEGER DEFAULT 0,
    UNIQUE(query, norm_hash)
);
CREATE INDEX IF NOT EXISTS ix_art_q ON articles(query, published);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    query TEXT, kind TEXT, created_at TEXT, body TEXT
);

CREATE TABLE IF NOT EXISTS threads (       -- Slack 스레드 ↔ 분석 키워드 (후속 질문용)
    ts TEXT PRIMARY KEY, channel TEXT, query TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS watch_stats (     -- 관심 키워드 일별 기사 수
    day TEXT, query TEXT, n INTEGER, n_en INTEGER, PRIMARY KEY(day, query)
);

CREATE TABLE IF NOT EXISTS processed (      -- 아침 배치가 처리한 Slack 메시지
    ts TEXT PRIMARY KEY, channel TEXT, status TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    ts TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, usd REAL
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    query TEXT,
    created_at TEXT,
    horizon TEXT,            -- 1m | 3m | 6m | 12m
    statement TEXT,
    probability REAL,
    basis TEXT,
    signposts TEXT,
    due_date TEXT,
    outcome INTEGER,         -- NULL=미채점, 1=발생, 0=미발생
    resolved_at TEXT,
    note TEXT
);
"""


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        for col in ("slack_channel", "slack_ts"):  # 구버전 DB 마이그레이션
            try:
                c.execute(f"ALTER TABLE predictions ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        yield c
        c.commit()
    finally:
        c.close()


def insert_articles(rows: list[dict]) -> int:
    before = 0
    with conn() as c:
        before = c.total_changes
        c.executemany(
            """INSERT OR IGNORE INTO articles
               (query,title,url,source,published,snippet,lang,origin,norm_hash)
               VALUES (:query,:title,:url,:source,:published,:snippet,:lang,:origin,:norm_hash)""",
            rows,
        )
        return c.total_changes - before


def load_articles(query: str, include_dups: bool = False) -> pd.DataFrame:
    with conn() as c:
        sql = "SELECT * FROM articles WHERE query=?" + ("" if include_dups else " AND is_dup=0")
        df = pd.read_sql_query(sql + " ORDER BY published", c, params=(query,))
    if not df.empty:
        df["published"] = pd.to_datetime(df["published"], errors="coerce")
    return df


def mark_dups(ids: list[int]):
    with conn() as c:
        c.executemany("UPDATE articles SET is_dup=1 WHERE id=?", [(i,) for i in ids])


def list_queries() -> list[str]:
    with conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT query FROM articles ORDER BY query")]


def save_report(query: str, kind: str, body: dict):
    with conn() as c:
        c.execute("INSERT INTO reports(query,kind,created_at,body) VALUES (?,?,?,?)",
                  (query, kind, datetime.now().isoformat(timespec="seconds"),
                   json.dumps(body, ensure_ascii=False)))


def latest_report(query: str, kind: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT body, created_at FROM reports WHERE query=? AND kind=? ORDER BY id DESC LIMIT 1",
                      (query, kind)).fetchone()
    if not r:
        return None
    body = json.loads(r["body"])
    body["_created_at"] = r["created_at"]
    return body


def save_predictions(query: str, preds: list[dict]):
    now = datetime.now().isoformat(timespec="seconds")
    with conn() as c:
        c.executemany(
            """INSERT INTO predictions(query,created_at,horizon,statement,probability,basis,signposts,due_date)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(query, now, p["horizon"], p["statement"], p["probability"],
              p.get("basis", ""), p.get("signposts", ""), p["due_date"]) for p in preds],
        )


def load_predictions(query: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if query:
            return pd.read_sql_query("SELECT * FROM predictions WHERE query=? ORDER BY due_date", c, params=(query,))
        return pd.read_sql_query("SELECT * FROM predictions ORDER BY due_date", c)


def resolve_prediction(pid: int, outcome: int, note: str = ""):
    with conn() as c:
        c.execute("UPDATE predictions SET outcome=?, resolved_at=?, note=? WHERE id=?",
                  (outcome, datetime.now().isoformat(timespec="seconds"), note, pid))


def save_thread(ts: str, channel: str, query: str):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?)",
                  (ts, channel, query, datetime.now().isoformat(timespec="seconds")))


def thread_query(ts: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT query FROM threads WHERE ts=?", (ts,)).fetchone()
    return r[0] if r else None


def last_collected(query: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT MAX(published) FROM articles WHERE query=?", (query,)).fetchone()
    return r[0] if r else None


def log_usage(model: str, tin: int, tout: int, usd: float):
    with conn() as c:
        c.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), model, tin, tout, usd))


def usage_summary() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(
            """SELECT substr(ts,1,7) AS month, model, COUNT(*) AS calls,
                      SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, ROUND(SUM(usd),3) AS usd
               FROM llm_usage GROUP BY 1,2 ORDER BY 1 DESC, 2""", c)


def kv_get(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(key: str, value: str):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))


def is_processed(ts: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM processed WHERE ts=?", (ts,)).fetchone() is not None


def mark_processed(ts: str, channel: str, status: str = "done"):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO processed VALUES (?,?,?,?)",
                  (ts, channel, status, datetime.now().isoformat(timespec="seconds")))


def set_prediction_msg(pid: int, channel: str, ts: str):
    with conn() as c:
        c.execute("UPDATE predictions SET slack_channel=?, slack_ts=? WHERE id=?", (channel, ts, pid))


def clear_failed() -> int:
    """실패로 기록된 Slack 요청을 지워 다음 배치에서 다시 처리되게 한다."""
    with conn() as c:
        return c.execute("DELETE FROM processed WHERE status LIKE 'error%'").rowcount
