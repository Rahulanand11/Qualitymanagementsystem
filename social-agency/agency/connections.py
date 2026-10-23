"""Stored Instagram credentials: encryption at rest, refresh, revocation, and client construction."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import instagram
from .instagram import InstagramClient, InstagramError
from .models import Account, Credential, utcnow
from .ops import audit
from .security import decrypt, encrypt

REFRESH_WITHIN = dt.timedelta(days=10)


class NotConnected(Exception):
    pass


def active_credential(s: Session, account: Account) -> Credential | None:
    return s.scalar(select(Credential).where(Credential.account_id == account.id, Credential.provider == "instagram",
                                             Credential.revoked.is_(False)).order_by(Credential.id.desc()))


def store_token(s: Session, account: Account, token: str, expires_in: int, scopes: list[str], ig_user_id: str,
                actor: str = "system") -> Credential:
    for old in s.scalars(select(Credential).where(Credential.account_id == account.id, Credential.revoked.is_(False))):
        old.revoked = True
    cred = Credential(account_id=account.id, encrypted_token=encrypt(token),
                      token_expires_at=utcnow() + dt.timedelta(seconds=expires_in), scopes=scopes)
    s.add(cred)
    account.ig_user_id = ig_user_id or account.ig_user_id
    account.connection_status, account.connection_error = "connected", None
    audit(s, "integration.connected", actor=actor, account_id=account.id, entity_type="account",
          entity_id=account.id, scopes=scopes)
    return cred


def revoke(s: Session, account: Account, actor: str) -> None:
    for cred in s.scalars(select(Credential).where(Credential.account_id == account.id, Credential.revoked.is_(False))):
        cred.revoked = True
    account.connection_status = "not_connected"
    audit(s, "integration.revoked", actor=actor, account_id=account.id, entity_type="account", entity_id=account.id)


def client_for(s: Session, account: Account) -> InstagramClient:
    cred = active_credential(s, account)
    if cred is None:
        raise NotConnected(f"Account '{account.name}' is not connected to Instagram")
    token = decrypt(cred.encrypted_token)
    if cred.token_expires_at and cred.token_expires_at - utcnow() < REFRESH_WITHIN:
        try:
            data = instagram.refresh_token(token)
            token = data["access_token"]
            cred.encrypted_token = encrypt(token)
            cred.token_expires_at = utcnow() + dt.timedelta(seconds=int(data.get("expires_in", 5184000)))
            audit(s, "integration.token_refreshed", account_id=account.id, entity_type="account", entity_id=account.id)
        except InstagramError as exc:
            if cred.token_expires_at < utcnow():
                mark_auth_failure(s, account, str(exc))
                raise NotConnected("Instagram token expired; reconnect the account") from exc
    return InstagramClient(token, account.ig_user_id or "me")


def mark_auth_failure(s: Session, account: Account, message: str) -> None:
    account.connection_status, account.connection_error = "reconnect_required", message
    audit(s, "integration.auth_failed", account_id=account.id, entity_type="account", entity_id=account.id,
          message=message)
