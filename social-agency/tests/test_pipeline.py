import datetime as dt
import hashlib
from pathlib import Path

from PIL import Image
from sqlalchemy import select, update

from agency.db import session_scope
from agency.jobs import enqueue
from agency.media_probe import probe_video
from agency.models import (Account, Asset, AuditLog, Derivative, Job, MediaRecord, Notification, Post, PostMedia,
                           utcnow)
from agency.publishing import approve_post, scheduler_tick, set_global_pause
from tests.conftest import make_image, make_video
from tests.helpers import create_account, drain, scan_twice


def release_jobs():
    with session_scope() as s:
        s.execute(update(Job).where(Job.status == "queued").values(run_after=utcnow() - dt.timedelta(seconds=1)))


def make_due(post_id):
    with session_scope() as s:
        p = s.get(Post, post_id)
        p.scheduled_for = utcnow() - dt.timedelta(minutes=1)
        scheduler_tick(s)


def run_all(rounds=15):
    for _ in range(rounds):
        release_jobs()
        if drain() == 0:
            break


def only_post():
    with session_scope() as s:
        return s.scalars(select(Post)).one().id


def test_image_end_to_end_limited_autonomy(env, fake_ai):
    acct = create_account(mode="limited", uploads_are_owned=True)
    src = make_image(env.root / "Creator Account" / "Inbox" / "cafe.jpg")
    original_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    first, second = scan_twice()
    assert first["waiting"] == 1 and second["new_assets"] == 1
    run_all()

    with session_scope() as s:
        asset = s.scalars(select(Asset)).one()
        assert asset.account_id == acct and asset.analysis_source.startswith("ai:")
        feed = next(d for d in asset.derivatives if d.kind == "feed")
        with Image.open(feed.path) as im:
            assert im.format == "JPEG" and im.width == 1080
            assert 0.8 <= im.width / im.height <= 1.91
        post = s.scalars(select(Post)).one()
        assert post.status == "scheduled", post.approval_reasons
        assert post.scheduled_for > utcnow()
        assert post.hashtags == ["lisbon", "cafeguide", "travelphotography"]
        assert post.content_source.startswith("ai:")
        pid = post.id

    make_due(pid)
    run_all()
    with session_scope() as s:
        post = s.get(Post, pid)
        assert post.status == "published"
        assert post.permalink.startswith("https://www.instagram.com/p/")
        assert s.scalar(select(MediaRecord).where(MediaRecord.ig_media_id == post.ig_media_id)).post_id == pid
        assert s.get(Asset, post.media[0].asset_id).status == "published"
    assert all(u.startswith("https://agency.example.com/public-media/") for u in env.graph.fetched_urls)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_hash  # original untouched
    assert list((env.root / "Published" / "Creator Account").glob("*.jpg"))
    # Secrets never reach the audit log or job records.
    with session_scope() as s:
        blob = str([a.detail for a in s.scalars(select(AuditLog))]) + str([(j.last_error, j.result) for j in s.scalars(select(Job))])
    assert "LONG_TOKEN_SECRET_123" not in blob and "app-secret-xyz" not in blob


def test_without_ai_everything_needs_review(env):
    create_account(mode="full", uploads_are_owned=True)
    make_image(env.root / "Creator Account" / "Inbox" / "a.jpg", dark=True)
    scan_twice()
    run_all()
    with session_scope() as s:
        post = s.scalars(select(Post)).one()
        assert post.status == "needs_review"
        assert post.content_source == "fallback"
        assert any("AI" in r or "fallback" in r for r in post.approval_reasons)
        ops = [op["op"] for op in post.media[0].derivative.operations]
        assert "exposure_gamma" in ops or "levels" in ops  # dark image was corrected
        # Not approved => the scheduler has nothing to publish.
        assert scheduler_tick(s) == 0


def test_incomplete_uploads_and_duplicates(env):
    create_account()
    inbox = env.root / "Creator Account" / "Inbox"
    make_image(inbox / "x.jpg")
    (inbox / "y.jpg.part").write_bytes(b"partial")
    (inbox / "empty.jpg").write_bytes(b"")
    scan_twice()
    (inbox / "copy_of_x.jpg").write_bytes((inbox / "x.jpg").read_bytes())
    _, second = scan_twice()
    assert second["duplicates"] == 1
    with session_scope() as s:
        assert len(s.scalars(select(Asset)).all()) == 1
        assert s.scalar(select(AuditLog).where(AuditLog.action == "asset.duplicate_skipped")) is not None


def test_corrupt_file_is_recorded_not_dropped(env):
    create_account()
    (env.root / "Creator Account" / "Inbox").mkdir(parents=True, exist_ok=True)
    (env.root / "Creator Account" / "Inbox" / "broken.jpg").write_bytes(b"not an image at all")
    scan_twice()
    with session_scope() as s:
        a = s.scalars(select(Asset)).one()
        assert a.status == "failed" and "Could not read media" in a.status_reason


def test_ambiguous_assets_go_to_review_queue(env):
    create_account("Creator Account")
    create_account("Business Account")
    make_image(env.root / "Shared Assets" / "untagged.jpg")
    make_image(env.root / "Creator Account" / "Inbox" / "@business-account__conflict.jpg", size=(1000, 1000))
    make_image(env.root / "Shared Assets" / "@business-account__tagged.jpg", size=(1200, 1500))
    scan_twice()
    with session_scope() as s:
        by_name = {a.filename: a for a in s.scalars(select(Asset))}
        assert by_name["untagged.jpg"].status == "needs_review" and by_name["untagged.jpg"].account_id is None
        assert by_name["@business-account__conflict.jpg"].status == "needs_review"
        assert "Ambiguous" in by_name["@business-account__conflict.jpg"].status_reason
        tagged = by_name["@business-account__tagged.jpg"]
        assert tagged.status == "new" and tagged.assignment_source == "filename"
        assert s.get(Account, tagged.account_id).slug == "business-account"


def _approved_scheduled_post(env, fake_ai_used=True, name="p.jpg"):
    make_image(env.root / "Creator Account" / "Inbox" / name)
    scan_twice()
    run_all()
    with session_scope() as s:
        post = s.scalars(select(Post)).one()
        if post.status != "approved":
            approve_post(s, post, "owner@example.com")
        post.status, post.scheduled_for = "scheduled", utcnow() - dt.timedelta(minutes=1)
        return post.id


def test_emergency_pause_blocks_publishing(env, fake_ai):
    create_account(uploads_are_owned=True)
    pid = _approved_scheduled_post(env)
    with session_scope() as s:
        set_global_pause(s, True, "owner@example.com")
    with session_scope() as s:
        assert scheduler_tick(s) == 0
    # Even a job that was already queued refuses to publish while paused.
    with session_scope() as s:
        enqueue(s, "publish_post", {"post_id": pid}, max_attempts=40)
    run_all()
    with session_scope() as s:
        assert s.get(Post, pid).status == "scheduled"
        assert s.get(Post, pid).ig_media_id is None
    assert not any(c[1].endswith("/media_publish") for c in env.graph.calls)
    with session_scope() as s:
        set_global_pause(s, False, "owner@example.com")
        scheduler_tick(s)
    run_all()
    with session_scope() as s:
        assert s.get(Post, pid).status == "published"


def test_rate_limit_retries_then_publishes(env, fake_ai):
    create_account(uploads_are_owned=True)
    pid = _approved_scheduled_post(env)
    env.graph.fail_next_publish = {"code": 4, "message": "Application request limit reached"}
    with session_scope() as s:
        scheduler_tick(s)
    drain()
    with session_scope() as s:
        post = s.get(Post, pid)
        assert post.status != "published" and post.ig_media_id is None
        job = s.scalars(select(Job).where(Job.kind == "publish_post")).one()
        assert job.status == "queued" and "Rate limited" in job.last_error
    run_all()
    with session_scope() as s:
        assert s.get(Post, pid).status == "published"
    # The container created on the first attempt was reused, so there is exactly one publish.
    assert len([c for c in env.graph.calls if c[0] == "POST" and c[1].endswith("/media") ]) == 1


def test_permanent_failure_goes_to_failed_queue(env, fake_ai):
    create_account(uploads_are_owned=True)
    pid = _approved_scheduled_post(env)
    env.graph.fail_next_publish = {"code": 9004, "message": "The media could not be fetched from this uri"}
    with session_scope() as s:
        scheduler_tick(s)
    run_all()
    with session_scope() as s:
        post = s.get(Post, pid)
        assert post.status == "failed" and "could not be fetched" in post.last_error
        assert post.ig_media_id is None
        assert s.scalar(select(Notification).where(Notification.level == "error")) is not None


def test_account_isolation_blocks_cross_account_media(env, fake_ai):
    a = create_account("Creator Account", uploads_are_owned=True)
    b = create_account("Business Account", uploads_are_owned=True)
    pid = _approved_scheduled_post(env)
    with session_scope() as s:
        post = s.get(Post, pid)
        post.account_id = b  # simulate a bug that points the post at the wrong account
        scheduler_tick(s)
    run_all()
    with session_scope() as s:
        post = s.get(Post, pid)
        assert post.status == "failed" and "isolation" in post.last_error
    assert not any(c[1].endswith("/media_publish") for c in env.graph.calls)
    assert a != b


def test_reel_pipeline_trims_formats_and_publishes(env, fake_ai):
    create_account(mode="full", uploads_are_owned=True)
    make_video(env.root / "Creator Account" / "Inbox" / "clip.mp4", seconds=4)
    scan_twice()
    run_all()
    with session_scope() as s:
        asset = s.scalars(select(Asset)).one()
        reel = next(d for d in asset.derivatives if d.kind == "reel")
        info = probe_video(Path(reel.path))
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
        ops = [o["op"] for o in reel.operations]
        assert "trim" in ops and "blurred_fill" in ops and "loudnorm" in ops
        assert reel.duration < 4.7  # leading black segment removed
        assert any(d.kind == "cover" for d in asset.derivatives)
        post = s.scalars(select(Post)).one()
        assert post.format == "reel"
        assert post.status == "scheduled", post.approval_reasons
        pid = post.id
    make_due(pid)
    run_all(25)
    with session_scope() as s:
        post = s.get(Post, pid)
        assert post.status == "published", (post.status, post.last_error)
    assert env.graph.container_progress_polls >= 2  # waited for Instagram to finish processing


def test_carousel_from_campaign_folder(env, fake_ai):
    create_account(mode="approval", uploads_are_owned=True)
    for i, size in enumerate([(1600, 1200), (1080, 1350), (2000, 1000)]):
        make_image(env.root / "Creator Account" / "Inbox" / "Lisbon Weekend" / f"{i}.jpg", size=size)
    scan_twice()
    run_all()
    with session_scope() as s:
        post = s.scalars(select(Post)).one()
        assert post.format == "carousel" and len(post.media) == 3
        assert post.status == "needs_review"  # approval mode
        assert post.approval_reasons == ["Account is in approval-required mode"]
        for pm in post.media:
            with Image.open(pm.derivative.path) as im:
                assert abs(im.width / im.height - 0.8) < 0.01  # consistent 4:5 across the carousel
        approve_post(s, post, "owner@example.com")
        post.status, post.scheduled_for = "scheduled", utcnow() - dt.timedelta(minutes=1)
        scheduler_tick(s)
    run_all()
    with session_scope() as s:
        assert s.scalars(select(Post)).one().status == "published"
    carousel = [c for c in env.graph.calls if c[2].get("media_type") == "CAROUSEL"]
    assert len(carousel) == 1 and len(carousel[0][2]["children"].split(",")) == 3
