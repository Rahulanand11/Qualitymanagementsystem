"""Audit trail, global settings (emergency pause) and structured logging."""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog, Setting
from .security import scrub

log = logging.getLogger("agency")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def audit(
    s: Session,
    action: str,
    *,
    actor: str = "system",
    account_id: int | None = None,
    entity_type: str = "",
    entity_id: int | None = None,
    **detail: Any,
) -> None:
    clean = json.loads(scrub(json.dumps(detail, default=str)))
    s.add(AuditLog(actor=actor, action=action, account_id=account_id, entity_type=entity_type,
                   entity_id=entity_id, detail=clean))
    log.info("%s %s#%s %s", action, entity_type, entity_id, clean)


def get_setting(s: Session, key: str, default: Any = None) -> Any:
    row = s.get(Setting, key)
    return default if row is None else row.value


def set_setting(s: Session, key: str, value: Any) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value


def publishing_globally_paused(s: Session) -> bool:
    return bool(get_setting(s, "publishing_paused", False))


def notify(s: Session, title: str, message: str = "", *, level: str = "info", account_id: int | None = None,
           link: str = "") -> None:
    """Record an alert for the dashboard and forward it to ALERT_WEBHOOK_URL (e.g. Slack) if configured."""
    import os

    import httpx

    from .models import Notification

    message = scrub(message)
    s.add(Notification(title=title, message=message, level=level, account_id=account_id, link=link))
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if url and level in ("warning", "error"):
        try:
            httpx.post(url, json={"text": f"[{level.upper()}] {title}: {message}"}, timeout=10)
        except httpx.HTTPError:
            log.warning("alert webhook delivery failed")
