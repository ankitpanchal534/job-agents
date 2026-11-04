from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from jobagent.text import normalize_question

STATUSES = ("new", "rejected", "watch_later", "queued", "blocked", "applied", "failed")
DECISIONS = ("apply", "watch_later", "reject")
BLOCK_REASONS = ("captcha", "login_expired", "unknown_question", "unsupported_portal", "submit_unconfirmed", "other")
UPDATABLE_FIELDS = ("source", "company", "title", "jd_summary", "score")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL UNIQUE,
    source     TEXT,
    company    TEXT,
    title      TEXT,
    jd_summary TEXT,
    status     TEXT NOT NULL DEFAULT 'new'
               CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    score      REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER REFERENCES jobs(id),
    question     TEXT NOT NULL,
    question_key TEXT NOT NULL,
    answer       TEXT,
    source       TEXT NOT NULL CHECK (source IN ('generated', 'human')),
    status       TEXT NOT NULL
                 CHECK (status IN ('pending_review', 'needs_input', 'approved', 'rejected')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    extracted          TEXT NOT NULL,
    filter_reasons     TEXT NOT NULL,
    score              REAL,
    missing_must_haves TEXT NOT NULL,
    decision           TEXT NOT NULL CHECK (decision IN ({", ".join(repr(d) for d in DECISIONS)})),
    reason             TEXT NOT NULL,
    model              TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    reason          TEXT NOT NULL CHECK (reason IN ({", ".join(repr(r) for r in BLOCK_REASONS)})),
    detail          TEXT NOT NULL DEFAULT '',
    page_url        TEXT,
    screenshot_path TEXT,
    host            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    notified_at     TEXT
);
CREATE TABLE IF NOT EXISTS journey_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    step       TEXT NOT NULL,
    agent      TEXT NOT NULL,
    status     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    details    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journey_job ON journey_steps(job_id, id);
"""

_ANSWER_COLUMNS = "id, job_id, question, answer, source, status, created_at, updated_at"
OPEN_ANSWER_STATUSES = ("pending_review", "needs_input")


class DuplicateJobError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    url: str
    source: Optional[str]
    company: Optional[str]
    title: Optional[str]
    jd_summary: Optional[str]
    status: str
    score: Optional[float]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Answer:
    id: int
    job_id: Optional[int]
    question: str
    answer: Optional[str]
    source: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Evaluation:
    id: int
    job_id: int
    extracted: dict
    filter_reasons: list
    score: Optional[float]
    missing_must_haves: list
    decision: str
    reason: str
    model: str
    created_at: str


@dataclass(frozen=True)
class Block:
    id: int
    job_id: int
    reason: str
    detail: str
    page_url: Optional[str]
    screenshot_path: Optional[str]
    host: str
    created_at: str
    resolved_at: Optional[str]
    notified_at: Optional[str]


@dataclass(frozen=True)
class JourneyStep:
    id: int
    job_id: int
    step: str
    agent: str
    status: str
    summary: str
    details: Optional[dict]
    created_at: str


def _evaluation(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"],
        job_id=row["job_id"],
        extracted=json.loads(row["extracted"]),
        filter_reasons=json.loads(row["filter_reasons"]),
        score=row["score"],
        missing_must_haves=json.loads(row["missing_must_haves"]),
        decision=row["decision"],
        reason=row["reason"],
        model=row["model"],
        created_at=row["created_at"],
    )


def _journey_step(row: sqlite3.Row) -> JourneyStep:
    details_str = row["details"]
    details = json.loads(details_str) if details_str else None
    return JourneyStep(
        id=row["id"],
        job_id=row["job_id"],
        step=row["step"],
        agent=row["agent"],
        status=row["status"],
        summary=row["summary"],
        details=details,
        created_at=row["created_at"],
    )


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Storage:
    def __init__(self, db_path, clock: Callable[[], datetime] = _utc_now):
        self._path = Path(db_path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _now(self) -> str:
        return self._clock().isoformat()

    def add_job(self, url: str, source: Optional[str] = None) -> Job:
        now = self._now()
        try:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO jobs (url, source, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (normalize_url(url), source, now, now),
                )
                return self._fetch(conn, cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise DuplicateJobError(f"job already saved: {url}") from error

    def get_job(self, job_id: int) -> Optional[Job]:
        with closing(self._connect()) as conn:
            return self._fetch(conn, job_id)

    def get_job_by_url(self, url: str) -> Optional[Job]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE url = ?", (normalize_url(url),)).fetchone()
        return Job(**row) if row else None

    def update_job(self, job_id: int, **fields) -> Job:
        unknown = sorted(set(fields) - set(UPDATABLE_FIELDS))
        if unknown:
            raise ValueError(f"cannot update field(s): {unknown}; allowed: {list(UPDATABLE_FIELDS)}")
        return self._update(job_id, fields)

    def set_status(self, job_id: int, status: str) -> Job:
        if status not in STATUSES:
            raise ValueError(f"invalid status '{status}'; allowed: {list(STATUSES)}")
        return self._update(job_id, {"status": status})

    def list_jobs(self, status: Optional[str] = None) -> list[Job]:
        query, params = "SELECT * FROM jobs", ()
        if status is not None:
            query, params = query + " WHERE status = ?", (status,)
        with closing(self._connect()) as conn:
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        return [Job(**row) for row in rows]

    def block_job(
        self,
        job_id: int,
        reason: str,
        detail: str = "",
        page_url: Optional[str] = None,
        screenshot_path: Optional[str] = None,
    ) -> Block:
        if reason not in BLOCK_REASONS:
            raise ValueError(f"invalid block reason '{reason}'; allowed: {list(BLOCK_REASONS)}")
        with closing(self._connect()) as conn, conn:
            job = self._fetch(conn, job_id)
            if job is None:
                raise ValueError(f"no job with id {job_id}")
            existing = conn.execute(
                "SELECT * FROM blocks WHERE job_id = ? AND reason = ? AND resolved_at IS NULL", (job_id, reason)
            ).fetchone()
            if existing:
                return Block(**existing)
            now = self._now()
            cursor = conn.execute(
                "INSERT INTO blocks (job_id, reason, detail, page_url, screenshot_path, host, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, reason, detail, page_url, screenshot_path, urlsplit(page_url or job.url).netloc.lower(), now),
            )
            conn.execute("UPDATE jobs SET status = 'blocked', updated_at = ? WHERE id = ?", (now, job_id))
            return Block(**conn.execute("SELECT * FROM blocks WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def list_blocks(self, open_only: bool = True) -> list[Block]:
        query = "SELECT * FROM blocks" + (" WHERE resolved_at IS NULL" if open_only else "") + " ORDER BY id"
        with closing(self._connect()) as conn:
            return [Block(**row) for row in conn.execute(query).fetchall()]

    def resume_job(self, job_id: int) -> Job:
        with closing(self._connect()) as conn, conn:
            job = self._fetch(conn, job_id)
            if job is None:
                raise ValueError(f"no job with id {job_id}")
            if job.status != "blocked":
                raise ValueError(f"job #{job_id} is '{job.status}', not blocked")
            now = self._now()
            conn.execute("UPDATE blocks SET resolved_at = ? WHERE job_id = ? AND resolved_at IS NULL", (now, job_id))
            conn.execute("UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?", (now, job_id))
            return self._fetch(conn, job_id)

    def is_block_resolved(self, block_id: int) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT resolved_at FROM blocks WHERE id = ?", (block_id,)).fetchone()
            if not row:
                return True
            return row["resolved_at"] is not None

    def add_journey_step(
        self,
        job_id: int,
        step: str,
        agent: str,
        status: str,
        summary: str,
        details: Optional[dict] = None,
    ) -> JourneyStep:
        now = self._now()
        details_json = json.dumps(details) if details is not None else None
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO journey_steps (job_id, step, agent, status, summary, details, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, step, agent, status, summary, details_json, now),
            )
            row = conn.execute("SELECT * FROM journey_steps WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return _journey_step(row)

    def list_journey_steps(self, job_id: int) -> list[JourneyStep]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM journey_steps WHERE job_id = ? ORDER BY id ASC", (job_id,)
            ).fetchall()
            return [_journey_step(row) for row in rows]

    def mark_block_notified(self, block_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE blocks SET notified_at = ? WHERE id = ?", (self._now(), block_id))

    def last_alert(self, reason: str, host: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(notified_at) AS last FROM blocks WHERE reason = ? AND host = ?", (reason, host)
            ).fetchone()
        return row["last"]

    def unalerted_count(self, reason: str, host: str, since: Optional[str], exclude_id: int) -> int:
        query = "SELECT COUNT(*) AS n FROM blocks WHERE reason = ? AND host = ? AND notified_at IS NULL AND id != ?"
        params = [reason, host, exclude_id]
        if since is not None:
            query, params = query + " AND created_at > ?", params + [since]
        with closing(self._connect()) as conn:
            return conn.execute(query, params).fetchone()["n"]

    def add_evaluation(
        self,
        job_id: int,
        extracted: dict,
        filter_reasons: list,
        score: Optional[float],
        missing_must_haves: list,
        decision: str,
        reason: str,
        model: str,
    ) -> Evaluation:
        if decision not in DECISIONS:
            raise ValueError(f"invalid decision '{decision}'; allowed: {list(DECISIONS)}")
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO evaluations (job_id, extracted, filter_reasons, score, missing_must_haves,"
                " decision, reason, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    json.dumps(extracted),
                    json.dumps(filter_reasons),
                    score,
                    json.dumps(missing_must_haves),
                    decision,
                    reason,
                    model,
                    self._now(),
                ),
            )
            return _evaluation(conn.execute("SELECT * FROM evaluations WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def list_evaluations(self, job_id: int) -> list[Evaluation]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM evaluations WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        return [_evaluation(row) for row in rows]

    def queue_answer(self, question: str, answer: Optional[str] = None, job_id: Optional[int] = None) -> Answer:
        key = normalize_question(question)
        open_marks = ", ".join("?" for _ in OPEN_ANSWER_STATUSES)
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                f"SELECT {_ANSWER_COLUMNS} FROM answers WHERE question_key = ? AND status IN ({open_marks})",
                (key, *OPEN_ANSWER_STATUSES),
            ).fetchone()
            if existing:
                return Answer(**existing)
            status, source = ("pending_review", "generated") if answer else ("needs_input", "human")
            now = self._now()
            cursor = conn.execute(
                "INSERT INTO answers (job_id, question, question_key, answer, source, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, question, key, answer, source, status, now, now),
            )
            return self._fetch_answer(conn, cursor.lastrowid)

    def list_answers(self, status: Optional[str] = None) -> list[Answer]:
        query, params = f"SELECT {_ANSWER_COLUMNS} FROM answers", ()
        if status is not None:
            query, params = query + " WHERE status = ?", (status,)
        with closing(self._connect()) as conn:
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        return [Answer(**row) for row in rows]

    def approve_answer(self, answer_id: int, final_answer: Optional[str] = None) -> Answer:
        with closing(self._connect()) as conn, conn:
            current = self._fetch_answer(conn, answer_id)
            text = final_answer if final_answer is not None else current.answer
            if not text:
                raise ValueError("an answer is required to approve this item")
            edited = current.answer is None or text != current.answer
            conn.execute(
                "UPDATE answers SET answer = ?, source = ?, status = 'approved', updated_at = ? WHERE id = ?",
                (text, "human" if edited else current.source, self._now(), answer_id),
            )
            return self._fetch_answer(conn, answer_id)

    def reject_answer(self, answer_id: int) -> Answer:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE answers SET status = 'rejected', updated_at = ? WHERE id = ?", (self._now(), answer_id))
            return self._fetch_answer(conn, answer_id)

    def approved_answers(self) -> list[tuple[str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT question, answer FROM answers WHERE status = 'approved' ORDER BY id").fetchall()
        return [(row["question"], row["answer"]) for row in rows]

    @staticmethod
    def _fetch_answer(conn: sqlite3.Connection, answer_id: int) -> Optional[Answer]:
        row = conn.execute(f"SELECT {_ANSWER_COLUMNS} FROM answers WHERE id = ?", (answer_id,)).fetchone()
        return Answer(**row) if row else None

    def _update(self, job_id: int, fields: dict) -> Job:
        columns = list(fields) + ["updated_at"]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        values = [*fields.values(), self._now(), job_id]
        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
            return self._fetch(conn, job_id)

    @staticmethod
    def _fetch(conn: sqlite3.Connection, job_id: int) -> Optional[Job]:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job(**row) if row else None
