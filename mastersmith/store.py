"""Persistence for the service: the job queue, in the same SQLite file as the spend ledger."""
import json
import os
import sqlite3
import threading
import time

from . import config
from .wallet import _locked


class Store:
    def __init__(self, path=None):
        self.path = str(path or config.DB_PATH)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        # one connection shared by the request threads and the worker, serialised by a lock like the wallet's: without
        # it a build's log lines and the chat's polls interleave on the connection and lose writes
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, user TEXT NOT NULL, kind TEXT NOT NULL,
                           spec TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
                           result TEXT, error TEXT, log TEXT NOT NULL DEFAULT '', source_job TEXT)""")
        self.db.commit()

    # ------------------------------------------------------------ jobs
    @_locked
    def enqueue(self, job_id, user, kind, spec, source_job=None):
        self.db.execute("INSERT INTO jobs(id, user, kind, spec, status, created, source_job) VALUES (?,?,?,?,?,?,?)",
                        (job_id, user, kind, json.dumps(spec), "queued", time.time(), source_job))
        self.db.commit()

    @_locked
    def claim_next(self):
        """Oldest queued job -> running. Returns the row dict or None."""
        cur = self.db.cursor()
        row = cur.execute("SELECT id, user, kind, spec, source_job FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            return None
        cur.execute("UPDATE jobs SET status='running', started=? WHERE id=? AND status='queued'", (time.time(), row[0]))
        self.db.commit()
        if cur.rowcount != 1:
            return None
        return {"id": row[0], "user": row[1], "kind": row[2], "spec": json.loads(row[3]), "source_job": row[4]}

    @_locked
    def mark_running(self, job_id):
        self.db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), job_id))
        self.db.commit()

    @_locked
    def append_log(self, job_id, line):
        self.db.execute("UPDATE jobs SET log = log || ? WHERE id=?", (line + "\n", job_id))
        self.db.commit()

    @_locked
    def finish(self, job_id, status, result=None, error=None):
        self.db.execute("UPDATE jobs SET status=?, finished=?, result=?, error=? WHERE id=?",
                        (status, time.time(), json.dumps(result, default=str) if result is not None else None, error, job_id))
        self.db.commit()

    @_locked
    def job(self, job_id, user=None):
        q = "SELECT id, user, kind, spec, status, created, started, finished, result, error, log FROM jobs WHERE id=?"
        args = [job_id]
        if user:
            q += " AND user=?"
            args.append(user)
        row = self.db.execute(q, args).fetchone()
        if not row:
            return None
        return {"id": row[0], "user": row[1], "kind": row[2], "spec": json.loads(row[3]), "status": row[4],
                "created": row[5], "started": row[6], "finished": row[7],
                "result": json.loads(row[8]) if row[8] else None, "error": row[9], "log": row[10]}

    @_locked
    def jobs_for(self, user, limit=20):
        rows = self.db.execute("SELECT id, kind, status, created, finished FROM jobs WHERE user=? ORDER BY created DESC LIMIT ?",
                               (user, limit)).fetchall()
        return [{"id": r[0], "kind": r[1], "status": r[2], "created": r[3], "finished": r[4]} for r in rows]

    @_locked
    def requeue_stale(self):
        """Jobs left 'running' by a crashed worker go back to the queue on start-up."""
        self.db.execute("UPDATE jobs SET status='queued', started=NULL WHERE status='running'")
        self.db.commit()
