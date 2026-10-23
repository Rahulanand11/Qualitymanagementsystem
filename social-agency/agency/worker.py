"""Background worker: folder watching, scheduling, periodic syncs/reports, and job execution.

Run with ``python -m agency.worker``. One tick is idempotent, so the worker can be restarted any time.
"""
from __future__ import annotations

import logging
import signal
import time

from sqlalchemy import select

from . import analytics, community, pipeline, publishing, reports  # noqa: F401  (register job handlers)
from .config import get_settings
from .db import init_db, session_scope
from .ingest import FolderWatcher
from .jobs import enqueue, run_pending
from .models import Account, utcnow
from .ops import configure_logging, set_setting
from .publishing import scheduler_tick

log = logging.getLogger("agency.worker")


def periodic(s) -> int:
    now = utcnow()
    n = 0
    for acct in s.scalars(select(Account).where(Account.connection_status == "connected")):
        enqueue(s, "sync_account", {"account_id": acct.id}, account_id=acct.id,
                idempotency_key=f"sync:{acct.id}:{now:%Y%m%d}:{now.hour // 6}")
        enqueue(s, "sync_comments", {"account_id": acct.id}, account_id=acct.id,
                idempotency_key=f"comments:{acct.id}:{now:%Y%m%d%H}")
        if now.weekday() == 0 and now.hour >= 6:
            enqueue(s, "build_report", {"account_id": acct.id, "kind": "weekly"}, account_id=acct.id,
                    idempotency_key=f"report:weekly:{acct.id}:{now:%G-%V}")
        if now.day == 1 and now.hour >= 6:
            enqueue(s, "build_report", {"account_id": acct.id, "kind": "monthly"}, account_id=acct.id,
                    idempotency_key=f"report:monthly:{acct.id}:{now:%Y-%m}")
        n += 1
    return n


def tick(watcher: FolderWatcher) -> dict:
    out = {}
    with session_scope() as s:
        out["ingest"] = watcher.scan(s)
    with session_scope() as s:
        out["scheduled"] = scheduler_tick(s)
        out["periodic_accounts"] = periodic(s)
    out["jobs_run"] = run_pending()
    with session_scope() as s:
        set_setting(s, "worker_heartbeat", utcnow().isoformat())
    return out


def main() -> None:
    configure_logging()
    settings = get_settings()
    for w in settings.warnings:
        log.warning(w)
    init_db()
    watcher = FolderWatcher()
    stop = {"flag": False}
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    except ValueError:  # not the main thread (run.py starts the worker in a thread)
        pass
    log.info("worker started; watching %s", settings.watch_root)
    while not stop["flag"]:
        try:
            result = tick(watcher)
            if result["jobs_run"] or result["ingest"]["new_assets"]:
                log.info("tick %s", result)
        except Exception:
            log.exception("worker tick failed; will retry")
        for _ in range(settings.worker_interval_seconds):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
