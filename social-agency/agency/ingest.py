"""Folder watcher: detects new uploads, waits for them to finish, deduplicates, and registers assets.

Folder convention (inside WATCH_ROOT, e.g. a Google Drive/Dropbox/OneDrive synced folder)::

    Social Media/
      Creator Account/Inbox/            -> assigned to the account whose folder_name is "Creator Account"
      Creator Account/Inbox/Paris Trip/ -> sub-folder becomes the campaign and the grouping key
      Creator Account/Approved/         -> same account, and the owner has pre-confirmed rights
      Business Account/Inbox/
      Shared Assets/                    -> no account: goes to the review queue unless tagged
      Brand Assets/                     -> logos/fonts/references, never ingested as post media
      Published/  Failed/               -> written by the agent (copies of final exports)

An asset can also be assigned with a filename tag ``@<account-slug>__name.jpg`` or a sidecar
``name.jpg.json`` containing ``{"account": "<slug>", "campaign": "...", "rights_confirmed": true}``.
Originals are never moved, modified or deleted.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .jobs import enqueue
from .media_probe import PARTIAL_EXT, media_kind, probe, sha256_file
from .models import Account, Asset, IngestedFile
from .ops import audit

log = logging.getLogger("agency.ingest")

SYSTEM_FOLDERS = {"brand assets", "published", "failed"}
TAG_RE = re.compile(r"^@([a-z0-9][a-z0-9\-]*)__", re.I)


def ensure_folders(s: Session) -> Path:
    root = get_settings().watch_root
    for name in ("Shared Assets", "Brand Assets", "Published", "Failed"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for acct in s.scalars(select(Account)):
        for sub in ("Inbox", "Approved"):
            (root / acct.folder_name / sub).mkdir(parents=True, exist_ok=True)
    return root


def _is_candidate(path: Path) -> bool:
    if path.name.startswith(".") or path.name.startswith("~$"):
        return False
    if path.suffix.lower() in PARTIAL_EXT or path.suffix.lower() == ".json":
        return False
    return media_kind(path) is not None


def _resolve_assignment(s: Session, rel: Path, path: Path) -> dict:
    """Work out account, campaign and pre-approval from folder, filename tag, or sidecar."""
    parts = rel.parts
    accounts = {a.folder_name.lower(): a for a in s.scalars(select(Account))}
    by_slug = {a.slug.lower(): a for a in accounts.values()}
    result = {"account": None, "source": "", "campaign": "", "preapproved": False, "rights_confirmed": False,
              "conflict": ""}

    folder_account = accounts.get(parts[0].lower()) if len(parts) > 1 else None
    if folder_account is not None:
        result.update(account=folder_account, source="folder")
        if len(parts) > 2 and parts[1].lower() == "approved":
            result.update(preapproved=True, rights_confirmed=True)
        inner = parts[2:-1] if len(parts) > 3 else ()
        if inner:
            result["campaign"] = "/".join(inner)
    elif len(parts) > 2 and parts[0].lower() == "shared assets":
        result["campaign"] = "/".join(parts[1:-1])

    m = TAG_RE.match(path.name)
    if m:
        tagged = by_slug.get(m.group(1).lower())
        if tagged and folder_account and tagged.id != folder_account.id:
            result["conflict"] = f"Folder says {folder_account.slug} but filename tag says {tagged.slug}"
        elif tagged:
            result.update(account=tagged, source=result["source"] or "filename")

    sidecar = path.with_name(path.name + ".json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text())
            tagged = by_slug.get(str(meta.get("account", "")).lower())
            if tagged and result["account"] and tagged.id != result["account"].id:
                result["conflict"] = f"Sidecar account {tagged.slug} conflicts with {result['account'].slug}"
            elif tagged:
                result.update(account=tagged, source=result["source"] or "sidecar")
            result["campaign"] = meta.get("campaign", result["campaign"])
            if meta.get("rights_confirmed") is True:
                result["rights_confirmed"] = True
        except (ValueError, OSError) as exc:
            result["conflict"] = f"Unreadable sidecar file: {exc}"
    return result


class FolderWatcher:
    """Polling watcher. Polling works for every sync client (Drive/Dropbox/OneDrive) and network drives."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[int, float]] = {}

    def scan(self, s: Session) -> dict:
        settings = get_settings()
        root = ensure_folders(s)
        stats = {"seen": 0, "waiting": 0, "new_assets": 0, "duplicates": 0, "rejected": 0}
        known = {row.path: row for row in s.scalars(select(IngestedFile))}
        now = time.time()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not _is_candidate(path):
                continue
            rel = path.relative_to(root)
            if rel.parts[0].lower() in SYSTEM_FOLDERS:
                continue
            stats["seen"] += 1
            st = path.stat()
            key = str(path)
            prev = known.get(key)
            if prev and prev.size_bytes == st.st_size and abs(prev.mtime - st.st_mtime) < 1e-6:
                continue  # already handled and unchanged
            # Upload-completeness check: size must be non-zero and unchanged across two scans,
            # and the file must not have been modified in the last UPLOAD_STABLE_SECONDS.
            signature = (st.st_size, st.st_mtime)
            if st.st_size == 0 or self._pending.get(key) != signature or now - st.st_mtime < settings.upload_stable_seconds:
                self._pending[key] = signature
                stats["waiting"] += 1
                continue
            self._pending.pop(key, None)
            outcome = self._register(s, path, rel, st.st_size, st.st_mtime, prev)
            stats[outcome] += 1
        return stats

    def _register(self, s: Session, path: Path, rel: Path, size: int, mtime: float, prev) -> str:
        def remember(outcome: str, asset_id=None, message=""):
            if prev is None:
                s.add(IngestedFile(path=str(path), size_bytes=size, mtime=mtime, asset_id=asset_id,
                                   outcome=outcome, message=message))
            else:
                prev.size_bytes, prev.mtime, prev.asset_id, prev.outcome, prev.message = size, mtime, asset_id, outcome, message
            s.flush()

        try:
            digest = sha256_file(path)
        except OSError as exc:
            remember("rejected", message=f"Unreadable: {exc}")
            return "rejected"
        existing = s.scalar(select(Asset).where(Asset.sha256 == digest))
        if existing is not None:
            remember("duplicate", existing.id, f"Same content as asset #{existing.id}")
            audit(s, "asset.duplicate_skipped", account_id=existing.account_id, entity_type="asset",
                  entity_id=existing.id, path=str(rel))
            return "duplicates"

        kind = media_kind(path)
        assignment = _resolve_assignment(s, rel, path)
        asset = Asset(sha256=digest, original_path=str(path), filename=path.name, media_kind=kind,
                      size_bytes=size, campaign=assignment["campaign"],
                      group_key=assignment["campaign"] or "", status="new")
        try:
            info = probe(path)
        except Exception as exc:  # corrupt or unsupported file
            asset.status, asset.status_reason = "failed", f"Could not read media: {exc}"
            asset.meta = {"error": str(exc)}
            s.add(asset)
            s.flush()
            remember("rejected", asset.id, asset.status_reason)
            audit(s, "asset.rejected", entity_type="asset", entity_id=asset.id, reason=asset.status_reason)
            return "rejected"

        asset.mime = info.get("mime", "")
        asset.width, asset.height = info.get("width"), info.get("height")
        asset.duration, asset.aspect_ratio = info.get("duration"), info.get("aspect_ratio")
        asset.meta = {k: v for k, v in info.items() if k not in ("width", "height", "duration", "aspect_ratio")}
        asset.meta["relative_path"] = str(rel)
        asset.meta["preapproved"] = assignment["preapproved"]

        if assignment["conflict"]:
            asset.status, asset.status_reason = "needs_review", f"Ambiguous account: {assignment['conflict']}"
        elif assignment["account"] is None:
            asset.status, asset.status_reason = "needs_review", "No account assigned (use an account Inbox folder, a filename tag, or assign it in the dashboard)"
        else:
            asset.account_id = assignment["account"].id
            asset.assignment_source = assignment["source"]
            rules = (assignment["account"].brand.automation_rules if assignment["account"].brand else {}) or {}
            asset.rights_confirmed = assignment["rights_confirmed"] or bool(rules.get("uploads_are_owned"))
            if asset.rights_confirmed:
                asset.rights_note = "Pre-approved folder" if assignment["preapproved"] else (
                    "Sidecar confirmation" if assignment["rights_confirmed"] else "Account rule: uploads are owner's content")
        s.add(asset)
        s.flush()
        remember("new_asset", asset.id)
        audit(s, "asset.ingested", account_id=asset.account_id, entity_type="asset", entity_id=asset.id,
              path=str(rel), status=asset.status, assignment=assignment["source"] or None)
        if asset.status == "new":
            enqueue(s, "analyze_asset", {"asset_id": asset.id}, account_id=asset.account_id,
                    idempotency_key=f"analyze:{asset.id}")
        return "new_assets"


def assign_asset(s: Session, asset: Asset, account: Account, actor: str, campaign: str | None = None) -> None:
    """Manual assignment from the review queue."""
    asset.account_id = account.id
    asset.assignment_source = "dashboard"
    if campaign is not None:
        asset.campaign = campaign
        asset.group_key = campaign
    rules = (account.brand.automation_rules if account.brand else {}) or {}
    if rules.get("uploads_are_owned"):
        asset.rights_confirmed = True
        asset.rights_note = "Account rule: uploads are owner's content"
    if asset.status in ("needs_review", "new"):
        asset.status, asset.status_reason = "new", ""
        enqueue(s, "analyze_asset", {"asset_id": asset.id}, account_id=account.id,
                idempotency_key=f"analyze:{asset.id}:{account.id}")
    audit(s, "asset.assigned", actor=actor, account_id=account.id, entity_type="asset", entity_id=asset.id)
