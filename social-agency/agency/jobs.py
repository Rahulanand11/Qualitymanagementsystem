"""Database-backed background job queue with idempotency keys, retries and crash recovery.

A database queue keeps the deployment to one service plus one database. Jobs are claimed
one at a time; a job left ``running`` by a crashed worker is re-queued after
``STALE_AFTER``. Handlers raise ``RetryableError`` for transient failures and
``PermanentError`` for failures that must go to the failed queue for a human.
"""
from __future__ import annotations

import datetime as dt
import logging
import traceback
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Job, utcnow
from .security import scrub

log = logging.getLogger("agency.jobs")

STALE_AFTER = dt.timedelta(minutes=15)


class RetryableError(Exception):
    def __init__(self, message: str, delay_seconds: int | None = None):
        super().__init__(message)
        self.delay_seconds = delay_seconds


class PermanentError(Exception):
    pass


Handler = Callable[[Session, Job], dict | None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(
    s: Session,
    kind: str,
    payload: dict | None = None,
    *,
    account_id: int | None = None,
    idempotency_key: str | None = None,
    run_after: dt.datetime | None = None,
    max_attempts: int = 5,
) -> Job:
    """Queue a job. With an idempotency key, an existing job with the same key is returned instead."""
    if idempotency_key:
        existing = s.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    job = Job(kind=kind, payload=payload or {}, account_id=account_id, idempotency_key=idempotency_key,
              run_after=run_after or utcnow(), max_attempts=max_attempts)
    s.add(job)
    try:
        s.flush()
    except IntegrityError:  # raced with another enqueue
        s.rollback()
        return s.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    return job


def backoff_seconds(attempts: int) -> int:
    return min(30 * 2 ** max(attempts - 1, 0), 3600)


def recover_stale(s: Session) -> int:
    cutoff = utcnow() - STALE_AFTER
    res = s.execute(update(Job).where(Job.status == "running", Job.locked_at < cutoff)
                    .values(status="queued", locked_at=None, last_error="Recovered after worker interruption"))
    return res.rowcount or 0


def _claim() -> int | None:
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.status == "queued", Job.run_after <= utcnow())
                       .order_by(Job.run_after, Job.id).limit(1))
        if job is None:
            return None
        claimed = s.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                            .values(status="running", locked_at=utcnow(), attempts=Job.attempts + 1))
        return job.id if claimed.rowcount else None


def run_job(job_id: int) -> str:
    with session_scope() as s:
        job = s.get(Job, job_id)
        fn = HANDLERS.get(job.kind)
        try:
            if fn is None:
                raise PermanentError(f"No handler registered for job kind {job.kind!r}")
            result = fn(s, job) or {}
            job.status, job.result, job.last_error, job.locked_at = "succeeded", result, None, None
            return job.status
        except RetryableError as exc:
            s.rollback()
            job = s.get(Job, job_id)
            _fail(job, scrub(str(exc)), retry=True, delay=exc.delay_seconds)
        except PermanentError as exc:
            s.rollback()
            job = s.get(Job, job_id)
            _fail(job, scrub(str(exc)), retry=False)
        except Exception as exc:  # unexpected: retry, the handler must be idempotent
            s.rollback()
            job = s.get(Job, job_id)
            log.error("job %s (%s) crashed: %s", job_id, job.kind, scrub(traceback.format_exc()))
            _fail(job, scrub(f"{type(exc).__name__}: {exc}"), retry=True)
        on_failure = FAILURE_HOOKS.get(job.kind)
        if on_failure and job.status == "dead":
            on_failure(s, job)
        return job.status


def _fail(job: Job, message: str, *, retry: bool, delay: int | None = None) -> None:
    job.last_error, job.locked_at = message, None
    if retry and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = utcnow() + dt.timedelta(seconds=delay or backoff_seconds(job.attempts))
    else:
        job.status = "dead"


FAILURE_HOOKS: dict[str, Callable[[Session, Job], None]] = {}


def on_dead(kind: str):
    def deco(fn):
        FAILURE_HOOKS[kind] = fn
        return fn
    return deco


def run_pending(limit: int = 50) -> int:
    with session_scope() as s:
        recover_stale(s)
    done = 0
    while done < limit:
        job_id = _claim()
        if job_id is None:
            break
        run_job(job_id)
        done += 1
    return done


def retry_job(s: Session, job: Job) -> None:
    """Manually re-queue a dead job from the dashboard."""
    job.status, job.attempts, job.run_after, job.locked_at = "queued", 0, utcnow(), None
