"""SQLite queue. Mutations and their audit events commit atomically."""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STATUSES = ("queued", "running", "retry_wait", "succeeded", "failed", "cancelled")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','retry_wait','succeeded','failed','cancelled')),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    total_attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    available_at REAL NOT NULL,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    result TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    type TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
            """)

    @contextmanager
    def _connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("任务不存在")
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **json.loads(row["payload"]),
            **{
                name: row[name]
                for name in (
                    "id",
                    "status",
                    "attempt",
                    "total_attempts",
                    "max_attempts",
                    "created_at",
                    "updated_at",
                    "available_at",
                    "lease_expires_at",
                    "error",
                )
            },
            "result": json.loads(row["result"]) if row["result"] is not None else None,
        }

    @staticmethod
    def _event(conn, job_id, event_type, attempt, now, detail):
        conn.execute(
            "INSERT INTO events(job_id,type,attempt,created_at,detail) VALUES(?,?,?,?,?)",
            (job_id, event_type, attempt, now, detail),
        )

    def submit(self, payload: dict, key: str | None = None) -> tuple[dict, bool]:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection(write=True) as conn:
            if key is not None:
                old = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if old is not None:
                    if old["payload_hash"] != digest:
                        raise Conflict("相同幂等键对应了不同请求")
                    return self._public(old), False
            now, job_id = self.clock(), uuid.uuid4().hex
            conn.execute(
                """INSERT INTO jobs(id,idempotency_key,payload,payload_hash,status,
                   max_attempts,created_at,updated_at,available_at)
                   VALUES(?,?,?,?,'queued',?,?,?,?)""",
                (job_id, key, serialized, digest, payload["max_attempts"], now, now, now),
            )
            self._event(conn, job_id, "submitted", 0, now, "任务已持久化，等待执行")
            return self._public(self._row(conn, job_id)), True

    def get(self, job_id: str) -> dict:
        with self._connection() as conn:
            return self._public(self._row(conn, job_id))

    def list_jobs(self, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        where, args = (" WHERE status = ?", (status,)) if status else ("", ())
        with self._connection() as conn:
            # One read snapshot keeps the count and page consistent during writes.
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM jobs" + where, args).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM jobs" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            return {
                "items": [self._public(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        with self._connection() as conn:
            self._row(conn, job_id)
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT id,type,attempt,created_at,detail FROM events
                   WHERE job_id = ? AND id > ? ORDER BY id ASC""",
                    (job_id, after),
                )
            ]

    def claim(self, lease_seconds: float = 60) -> dict | None:
        with self._connection(write=True) as conn:
            now = self.clock()
            row = conn.execute(
                """SELECT * FROM jobs WHERE status IN ('queued','retry_wait')
                   AND available_at <= ? AND attempt < max_attempts
                   ORDER BY available_at,created_at,id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute(
                """UPDATE jobs SET status='running',attempt=attempt+1,
                   total_attempts=total_attempts+1,updated_at=?,lease_token=?,lease_expires_at=?
                   WHERE id=?""",
                (now, token, now + lease_seconds, row["id"]),
            )
            current = self._row(conn, row["id"])
            self._event(conn, row["id"], "started", current["attempt"], now, "Worker 已认领任务")
            return {**self._public(current), "lease_token": token}

    @staticmethod
    def _owns(row, token, now) -> bool:
        return (
            row["status"] == "running"
            and row["lease_token"] == token
            and row["lease_expires_at"] > now
        )

    def complete(self, job_id: str, token: str, result: dict) -> bool:
        with self._connection(write=True) as conn:
            row, now = self._row(conn, job_id), self.clock()
            if not self._owns(row, token, now):
                return False
            conn.execute(
                """UPDATE jobs SET status='succeeded',result=?,error=NULL,updated_at=?,
                   lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
                (json.dumps(result, ensure_ascii=False), now, job_id),
            )
            self._event(conn, job_id, "succeeded", row["attempt"], now, "结果已保存")
            return True

    def _failed(self, conn, row, error, now, retry_base, event_type):
        exhausted = row["attempt"] >= row["max_attempts"]
        status = "failed" if exhausted else "retry_wait"
        delay = min(60, retry_base * (2 ** (row["attempt"] - 1)))
        conn.execute(
            """UPDATE jobs SET status=?,error=?,updated_at=?,available_at=?,
               lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
            (status, error, now, now if exhausted else now + delay, row["id"]),
        )
        detail = f"{error}；已耗尽尝试次数" if exhausted else f"{error}；{delay:g} 秒后重试"
        self._event(conn, row["id"], event_type, row["attempt"], now, detail)

    def fail(self, job_id: str, token: str, error: str, retry_base: float = 2) -> bool:
        with self._connection(write=True) as conn:
            row, now = self._row(conn, job_id), self.clock()
            if not self._owns(row, token, now):
                return False
            self._failed(conn, row, error, now, retry_base, "attempt_failed")
            return True

    def recover_expired(self, retry_base: float = 2) -> int:
        with self._connection(write=True) as conn:
            now = self.clock()
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='running' AND lease_expires_at <= ?", (now,)
            ).fetchall()
            for row in rows:
                self._failed(conn, row, "lease_expired", now, retry_base, "lease_expired")
            return len(rows)

    def cancel(self, job_id: str) -> dict:
        with self._connection(write=True) as conn:
            row, now = self._row(conn, job_id), self.clock()
            if row["status"] not in ("queued", "retry_wait"):
                raise Conflict("只能取消等待中的任务")
            conn.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (now, job_id)
            )
            self._event(conn, job_id, "cancelled", row["attempt"], now, "任务已取消")
            return self._public(self._row(conn, job_id))

    def retry(self, job_id: str) -> dict:
        with self._connection(write=True) as conn:
            row, now = self._row(conn, job_id), self.clock()
            if row["status"] != "failed":
                raise Conflict("只能重新排队已失败的任务")
            conn.execute(
                """UPDATE jobs SET status='queued',attempt=0,result=NULL,error=NULL,
                   available_at=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
                (now, now, job_id),
            )
            self._event(conn, job_id, "retried", 0, now, "任务重新排队，保留此前执行历史")
            return self._public(self._row(conn, job_id))

    def stats(self) -> dict:
        with self._connection() as conn:
            conn.execute("BEGIN")
            counts = dict.fromkeys(STATUSES, 0)
            counts.update(
                {
                    row["status"]: row["n"]
                    for row in conn.execute("SELECT status,COUNT(*) AS n FROM jobs GROUP BY status")
                }
            )
            attempts = conn.execute("SELECT COALESCE(SUM(total_attempts),0) FROM jobs").fetchone()[
                0
            ]
            return {"total": sum(counts.values()), "counts": counts, "total_attempts": attempts}
