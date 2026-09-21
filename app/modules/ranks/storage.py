# -*- coding: utf-8 -*-
"""SQLite 台账:每轮每个 (关键词, 域名) 一行,历史全部留底,不覆盖。"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    keyword TEXT NOT NULL,
    domain TEXT NOT NULL,
    gl TEXT, hl TEXT, device TEXT,
    position INTEGER,
    url TEXT,
    depth INTEGER,
    status TEXT NOT NULL DEFAULT 'ok',
    cost REAL,
    engine TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_kdd ON checks(keyword, domain, run_date);
CREATE INDEX IF NOT EXISTS idx_checks_domain ON checks(domain, run_date);
"""


def db_path():
    from app import config
    return config.data_dir() / "ranks.db"


def connect():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def save(conn, rows):
    conn.executemany(
        "INSERT INTO checks (run_date, checked_at, keyword, domain, gl, hl, device,"
        " position, url, depth, status, cost, engine)"
        " VALUES (:run_date,:checked_at,:keyword,:domain,:gl,:hl,:device,"
        ":position,:url,:depth,:status,:cost,:engine)", rows)
    conn.commit()


def run_dates(conn, domain, limit=2):
    q = ("SELECT DISTINCT run_date FROM checks WHERE domain=? "
         "ORDER BY run_date DESC LIMIT ?")
    return [r[0] for r in conn.execute(q, (domain, limit))]


def prev_position(conn, keyword, domain, before_run_date):
    """上一轮的名次 —— 只认 status='ok' 的记录,报错那轮不能当基准。"""
    row = conn.execute(
        "SELECT position FROM checks WHERE keyword=? AND domain=? AND run_date<?"
        " AND status='ok' ORDER BY run_date DESC, id DESC LIMIT 1",
        (keyword, domain, before_run_date)).fetchone()
    return row[0] if row else None


def latest(conn, domain):
    """每个词取最近一次成功记录,外加上一轮的名次用于算涨跌。"""
    q = """
    SELECT c.* FROM checks c
    JOIN (SELECT keyword, MAX(id) AS mid FROM checks
          WHERE domain=? AND status='ok' GROUP BY keyword) m ON c.id=m.mid
    ORDER BY (c.position IS NULL), c.position
    """
    return [dict(r) for r in conn.execute(q, (domain,))]


def history(conn, keyword, domain, limit=60):
    q = ("SELECT run_date, position, url, status FROM checks"
         " WHERE keyword=? AND domain=? ORDER BY run_date DESC, id DESC LIMIT ?")
    return [dict(r) for r in conn.execute(q, (keyword, domain, limit))]


def failed_keywords(conn, domain):
    """从来没有过 ok 记录的词 —— 这些才是真正待补查的。"""
    q = """
    SELECT DISTINCT keyword FROM checks WHERE domain=? AND keyword NOT IN
      (SELECT keyword FROM checks WHERE domain=? AND status='ok')
    """
    return [r[0] for r in conn.execute(q, (domain, domain))]


def run_cost(conn, domain, run_date=None):
    if run_date is None:
        d = run_dates(conn, domain, 1)
        if not d:
            return None
        run_date = d[0]
    row = conn.execute(
        "SELECT COALESCE(SUM(cost),0), COUNT(DISTINCT keyword),"
        " MAX(engine) FROM checks WHERE domain=? AND run_date=?",
        (domain, run_date)).fetchone()
    return {"run_date": run_date, "cost": round(row[0], 4),
            "keywords": row[1], "engine": row[2]}


def domains(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT domain FROM checks ORDER BY domain")]
