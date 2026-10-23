import agency.worker  # noqa: F401  (registers job handlers)
from agency.connections import store_token
from agency.db import session_scope
from agency.ingest import FolderWatcher, ensure_folders
from agency.jobs import run_pending
from agency.models import Account, BrandProfile


def create_account(name="Creator Account", mode="approval", connect=True, **rules) -> int:
    with session_scope() as s:
        a = Account(name=name, slug=name.lower().replace(" ", "-"), folder_name=name, autonomy_mode=mode,
                    account_type="creator", posts_per_week=4, timezone="UTC")
        a.brand = BrandProfile(niche="travel photography", voice="warm, specific, unfussy",
                               content_pillars=[{"name": "City Guides", "weight": 2}, {"name": "Behind the Lens", "weight": 1}],
                               ctas=["Save this for your next trip."], hashtag_bank=["travelphotography", "cityguide"],
                               automation_rules={"min_qc_score": 70, "auto_formats": ["image", "carousel"], **rules})
        s.add(a)
        s.flush()
        ensure_folders(s)
        if connect:
            store_token(s, a, "LONG_TOKEN_SECRET_123", 5184000, ["instagram_business_basic"], "17841400000000001")
        return a.id


def scan_twice():
    """The watcher needs two scans with an unchanged file before it ingests it."""
    w = FolderWatcher()
    with session_scope() as s:
        first = w.scan(s)
    with session_scope() as s:
        second = w.scan(s)
    return first, second


def drain(rounds=10):
    total = 0
    for _ in range(rounds):
        n = run_pending()
        total += n
        if n == 0:
            break
    return total
