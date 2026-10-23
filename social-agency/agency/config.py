"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so non-technical operators can just edit one file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


# Anthropic first-party prices, USD per million tokens (input, output).
MODEL_PRICING = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class Settings:
    app_env: str = "development"
    data_dir: Path = Path("./data")
    watch_root: Path = Path("./data/Social Media")
    database_url: str = ""
    secret_key: str = ""
    token_encryption_key: str = ""
    public_base_url: str = "http://localhost:8000"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    ig_api_version: str = "v23.0"
    ig_send_alt_text: bool = True
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    ai_model_light: str = "claude-opus-5-5"
    ai_monthly_budget_usd: float = 50.0
    worker_interval_seconds: int = 15
    upload_stable_seconds: int = 10
    warnings: list[str] = field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_app_id and self.meta_app_secret)

    @property
    def public_media_ready(self) -> bool:
        """Instagram fetches media from a public HTTPS URL at publish time."""
        url = self.public_base_url
        return url.startswith("https://") and "localhost" not in url and "127.0.0.1" not in url


def _dev_secret(data_dir: Path, name: str, factory) -> str:
    """In development, persist generated secrets so restarts keep sessions/tokens valid."""
    secrets_dir = data_dir / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / name
    if path.exists():
        return path.read_text().strip()
    value = factory()
    path.write_text(value)
    path.chmod(0o600)
    return value


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("AGENCY_ENV_FILE", ".env")))
    env = os.environ
    data_dir = Path(env.get("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(
        app_env=env.get("APP_ENV", "development"),
        data_dir=data_dir,
        watch_root=Path(env.get("WATCH_ROOT", str(data_dir / "Social Media"))).resolve(),
        database_url=env.get("DATABASE_URL", f"sqlite:///{data_dir / 'agency.db'}"),
        secret_key=env.get("SECRET_KEY", ""),
        token_encryption_key=env.get("TOKEN_ENCRYPTION_KEY", ""),
        public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        meta_app_id=env.get("META_APP_ID", ""),
        meta_app_secret=env.get("META_APP_SECRET", ""),
        ig_api_version=env.get("IG_API_VERSION", "v23.0"),
        ig_send_alt_text=env.get("IG_SEND_ALT_TEXT", "true").lower() == "true",
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        ai_model=env.get("AI_MODEL", "claude-opus-5-5"),
        ai_model_light=env.get("AI_MODEL_LIGHT", env.get("AI_MODEL", "claude-opus-5-5")),
        ai_monthly_budget_usd=float(env.get("AI_MONTHLY_BUDGET_USD", "50")),
        worker_interval_seconds=int(env.get("WORKER_INTERVAL_SECONDS", "15")),
        upload_stable_seconds=int(env.get("UPLOAD_STABLE_SECONDS", "10")),
    )
    if s.is_production:
        missing = [k for k, v in (("SECRET_KEY", s.secret_key), ("TOKEN_ENCRYPTION_KEY", s.token_encryption_key)) if not v]
        if missing:
            raise RuntimeError(f"Production requires these environment variables: {', '.join(missing)}")
    else:
        if not s.secret_key:
            s.secret_key = _dev_secret(data_dir, "secret_key", lambda: secrets.token_urlsafe(48))
        if not s.token_encryption_key:
            s.token_encryption_key = _dev_secret(data_dir, "fernet_key", lambda: Fernet.generate_key().decode())
            s.warnings.append("Using a generated development encryption key stored in DATA_DIR/.secrets.")
    if not s.meta_configured:
        s.warnings.append("META_APP_ID / META_APP_SECRET not set: Instagram accounts cannot be connected yet.")
    if not s.public_media_ready:
        s.warnings.append("PUBLIC_BASE_URL is not a public HTTPS URL: Instagram cannot fetch media, so publishing is blocked.")
    if not s.anthropic_api_key:
        s.warnings.append("ANTHROPIC_API_KEY not set: AI analysis/captions fall back to heuristics and every post requires review.")
    s.storage_dir.mkdir(parents=True, exist_ok=True)
    return s


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Used by tests after changing environment variables."""
    global _settings
    _settings = None
