"""M3.3 / T-FL-03 — durable object-storage handoff, proven across pod boundaries.

THE DEFECT
==========
The explorer writes ``{work_dir}/{crawl_id}/manifest.jsonl``; qe-central ingests
by reading ``{crawl_storage_root}/{crawl_id}/manifest.jsonl`` from what it
assumes is the same filesystem. In Kubernetes those are two DIFFERENT
``emptyDir`` volumes — pod-local, node-local, destroyed with the pod. So the
moment producer and consumer are scheduled on different pods, ingestion finds no
manifest and records a COMPLETED crawl as ``no manifest produced``.

HOW "DIFFERENT PODS" IS PROVEN HERE
===================================
A pod boundary is, for this purpose, exactly one property: **the producer and
the consumer share no filesystem.** These tests reproduce that property
literally — the producer writes into one temporary directory, the consumer reads
from a DIFFERENT one, and the producer's directory is DELETED before the
consumer runs. Nothing but the object store connects them, so a consumer that
succeeds cannot have succeeded via a shared disk.

Deleting the producer's directory is also the pod-restart test the milestone
asks for: an ``emptyDir`` does not survive its pod, so a restart between the
manifest write and ingestion destroys the directory exactly as this does.

THE STORAGE LAYER IS THE HOUSE ONE
==================================
qe-central goes through ``nexus_sdk.storage.ArtifactStore`` — the same layer
``app/substrate/assets.py`` already uses for this crawl's screenshots, on the
same ``NEXUS_STORAGE_BACKEND`` env contract as platform-api and the engines.
Keys are TENANT-SCOPED via ``ArtifactStore.build_key``, which is what stops a
misconfigured service writing across a tenant boundary.

Gated on a real S3-compatible endpoint (MinIO locally, S3/GCS in CI) via
``QEC_TEST_S3_ENDPOINT``. Not a mock: the point is that a real client, real keys
and real bytes make the round trip.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from app.storage import object_store

from _infra_gate import infra_gate, require_infra

S3_ENDPOINT = os.environ.get("QEC_TEST_S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("QEC_TEST_S3_BUCKET", "qec-evidence")

ENV_S3 = "QEC_TEST_S3_ENDPOINT"
TENANT = "tfl03tenant"

# A26.2 / A27.1 — TWO-STATE, not a plain skipif.
#
# These six tests had never executed in ANY environment: CI provided no object
# storage, so the mark below skipped every one of them and the build stayed
# green. A plain `skipif` cannot tell "no MinIO on this laptop" (fine) from "CI
# was supposed to provide MinIO and did not" (a hole). So the mark stops being a
# skip the moment QEC_REQUIRE_S3 declares the service mandatory — and the
# `require_infra` call in the s3_env fixture then fails them by name.
needs_s3 = infra_gate(
    S3_ENDPOINT, ENV_S3,
    purpose=("the T-FL-03 handoff proof needs a real S3-compatible endpoint "
             "(MinIO locally). A mocked store would prove the test doubles "
             "agree, not that the handoff works."),
    category="s3",
)


@pytest.fixture()
def s3_env(monkeypatch):
    """Configure the HOUSE storage contract (NEXUS_STORAGE_BACKEND + S3_*).

    These are the same variables platform-api, the engines and qe-central's own
    ``substrate/assets.py`` already read — one deployment configures every
    service, which is the design §3.1 co-readability requirement.
    """
    # The CI-side half of the two-state gate. All six S3-gated tests take this
    # fixture, so this single call is what turns "CI forgot to provision MinIO"
    # into six named failures instead of six silent skips.
    require_infra(S3_ENDPOINT, ENV_S3, "s3")
    monkeypatch.setenv("NEXUS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", S3_BUCKET)
    monkeypatch.setenv("S3_ENDPOINT", S3_ENDPOINT)
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ACCESS_KEY",
                       os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"))
    monkeypatch.setenv("S3_SECRET_KEY",
                       os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"))
    # The settings singleton and the cached ArtifactStore both read config at
    # build time, so both must be re-pointed for the fixture to take effect.
    from app.config import settings
    monkeypatch.setattr(settings, "nexus_storage_backend", "s3", raising=False)
    object_store.reset_store_cache_for_tests()
    yield True
    object_store.reset_store_cache_for_tests()


def _write_crawl_evidence(root: Path, crawl_id: str) -> Path:
    """A realistic crawl directory: manifest + staged frames + an artifact."""
    d = root / crawl_id
    (d / "frames").mkdir(parents=True, exist_ok=True)
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    (d / "frames" / "001.png").write_bytes(b"\x89PNG\r\n\x1a\nFRAME-ONE")
    (d / "frames" / "002.png").write_bytes(b"\x89PNG\r\n\x1a\nFRAME-TWO")
    (d / "artifacts" / "policy.pdf").write_bytes(b"%PDF-1.4 policy")
    # write_BYTES, not write_text. `write_text` applies the platform's newline
    # translation, so on Windows this fixture wrote CRLF and on Linux LF - the
    # laptop proof and the CI proof would run against DIFFERENT BYTES, and the
    # one that matters is whichever nobody looked at. The manifest is JSONL:
    # the newline IS the record delimiter, so it is payload here, not layout.
    (d / object_store.MANIFEST_FILENAME).write_bytes(
        b'{"type":"state","state_id":"s1"}\n{"type":"action","idx":1}\n')
    return d


# ══════════════════════════════════════════════════════════════════════════
# THE HEADLINE PROOF
# ══════════════════════════════════════════════════════════════════════════

@needs_s3
@pytest.mark.asyncio
async def test_producer_and_consumer_share_no_filesystem(s3_env):
    """explorer pod → object storage → a DIFFERENT qe-central pod → ingestion."""
    crawl_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as producer_root, \
         tempfile.TemporaryDirectory() as consumer_root:

        # ── PRODUCER POD ───────────────────────────────────────────────
        crawl_dir = _write_crawl_evidence(Path(producer_root), crawl_id)
        uploaded = await object_store.publish_crawl_dir(
            TENANT, crawl_id, crawl_dir)
        assert uploaded == 4, f"expected 4 files published, got {uploaded}"

        # ── CONSUMER POD (a different filesystem, provably empty) ──────
        consumer_dir = Path(consumer_root) / crawl_id
        assert not (consumer_dir / object_store.MANIFEST_FILENAME).exists()
        assert Path(producer_root) != Path(consumer_root)

        resolved = await object_store.ensure_local(
            TENANT, crawl_id, consumer_dir)

        manifest = resolved / object_store.MANIFEST_FILENAME
        assert manifest.is_file(), (
            "the consumer pod could not obtain the manifest — this is the "
            "pod-local emptyDir defect: a completed crawl reads as "
            "'no manifest produced'")
        # BYTE equality, not text equality. Comparing read_text() on both
        # sides runs both through newline translation, which MASKS exactly
        # the corruption this assertion exists to detect: a handoff that
        # rewrote the manifest's line endings would pass a text comparison
        # having changed every record boundary in a newline-delimited file.
        # The transfer is byte-exact today (upload opens "rb", fetch calls
        # write_bytes); this is what stops that silently ceasing to be true.
        original = (crawl_dir / object_store.MANIFEST_FILENAME).read_bytes()
        # RIGHT SUBJECT, not merely a resembling one: b"" == b"" is also a pass,
        # so a handoff that delivered an empty file on both sides would satisfy
        # a bare equality check. Pin what the manifest must actually BE.
        assert (len(original.splitlines()) == 2
                and original.startswith(b'{"type"')), (
            f"the fixture no longer produces the 2-record JSONL this proof "
            f"assumes: {original!r}")
        assert manifest.read_bytes() == original, (
            "the manifest arrived CORRUPTED across the handoff: "
            f"{manifest.read_bytes()!r} != {original!r}")
        assert b'\r' not in manifest.read_bytes(), (
            "the handoff introduced CR bytes into a JSONL manifest - every "
            "record delimiter in the file has changed")

        # The frames the manifest references must travel with it, or ingestion
        # writes a substrate that points at screenshots nobody has.
        assert (resolved / "frames" / "001.png").read_bytes() == \
            b"\x89PNG\r\n\x1a\nFRAME-ONE"
        assert (resolved / "frames" / "002.png").is_file()
        assert (resolved / "artifacts" / "policy.pdf").read_bytes() == \
            b"%PDF-1.4 policy"


@needs_s3
@pytest.mark.asyncio
async def test_pod_restart_between_write_and_ingestion_loses_nothing(s3_env):
    """The producer's whole directory is destroyed before the consumer runs.

    This is what an explorer pod restart does to an ``emptyDir``. Under the old
    design the evidence was simply gone; the object store must make the restart
    irrelevant.
    """
    crawl_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as producer_root, \
         tempfile.TemporaryDirectory() as consumer_root:
        crawl_dir = _write_crawl_evidence(Path(producer_root), crawl_id)
        await object_store.publish_crawl_dir(TENANT, crawl_id, crawl_dir)

        # ── THE POD DIES ───────────────────────────────────────────────
        shutil.rmtree(producer_root, ignore_errors=True)
        assert not Path(producer_root).exists(), "the producer pod still exists"

        # ── A NEW CONSUMER POD INGESTS ─────────────────────────────────
        resolved = await object_store.ensure_local(
            TENANT, crawl_id, Path(consumer_root) / crawl_id)
        assert (resolved / object_store.MANIFEST_FILENAME).is_file(), (
            "a pod restart between the manifest write and ingestion destroyed "
            "the crawl's evidence")
        assert (resolved / "frames" / "001.png").is_file()


@needs_s3
@pytest.mark.asyncio
async def test_ingestion_is_idempotent_and_reads_through(s3_env):
    """A retried ingestion must be cheap: the second call does no network work."""
    crawl_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as producer_root, \
         tempfile.TemporaryDirectory() as consumer_root:
        crawl_dir = _write_crawl_evidence(Path(producer_root), crawl_id)
        await object_store.publish_crawl_dir(TENANT, crawl_id, crawl_dir)
        consumer_dir = Path(consumer_root) / crawl_id

        await object_store.ensure_local(TENANT, crawl_id, consumer_dir)
        # Mark the local copy so a second fetch would be detectable.
        marker = consumer_dir / "LOCAL_MARKER"
        marker.write_text("do not clobber")
        await object_store.ensure_local(TENANT, crawl_id, consumer_dir)
        assert marker.read_text() == "do not clobber"
        assert (consumer_dir / object_store.MANIFEST_FILENAME).is_file()


@needs_s3
@pytest.mark.asyncio
async def test_the_manifest_is_published_last(s3_env):
    """A consumer that races the upload must never see a manifest without frames.

    The manifest's presence is the "evidence is here" signal, so uploading it
    before the frames it references would let a racing consumer ingest a partial
    crawl and present it as a whole one.
    """
    import boto3
    crawl_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as producer_root:
        crawl_dir = _write_crawl_evidence(Path(producer_root), crawl_id)
        await object_store.publish_crawl_dir(TENANT, crawl_id, crawl_dir)

    client = boto3.client(
        "s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY",
                                             "minioadmin"))
    prefix = object_store.evidence_prefix(TENANT, crawl_id) + "/"
    objs = client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)["Contents"]
    by_key = {o["Key"]: o["LastModified"] for o in objs}
    manifest_key = object_store.object_key(
        TENANT, crawl_id, object_store.MANIFEST_FILENAME)
    assert manifest_key in by_key
    others = [t for k, t in by_key.items() if k != manifest_key]
    assert others, "no non-manifest objects were published"
    assert by_key[manifest_key] >= max(others), (
        "the manifest was published BEFORE the frames it references — a "
        "consumer racing the upload could ingest a partial crawl")


@needs_s3
@pytest.mark.asyncio
async def test_evidence_is_written_under_the_tenant_prefix(s3_env):
    """Tenant scoping is the property the hand-rolled layout did not have.

    ``ArtifactStore.build_key`` puts the tenant first, so a misconfigured
    service cannot write a crawl's evidence outside its own tenant's prefix.
    """
    crawl_id = uuid.uuid4().hex
    prefix = object_store.evidence_prefix(TENANT, crawl_id)
    assert prefix.startswith(TENANT + "/"), (
        f"evidence key {prefix!r} is not tenant-scoped")
    assert f"/{object_store.EVIDENCE_NAMESPACE}/" in prefix + "/"
    # A different tenant's prefix for the SAME crawl id must not collide.
    other = object_store.evidence_prefix("someone_else", crawl_id)
    assert not other.startswith(TENANT + "/")
    assert other != prefix


# ══════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY + FAIL-CLOSED
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_local_backend_is_byte_identical_to_today(monkeypatch):
    """An unconfigured deployment must be completely unchanged.

    Only the CLOUD backends are treated as object-backed; ``local`` (the
    default) makes publish a no-op and ``ensure_local`` a pass-through, so the
    pre-M3.3 shared-volume path is preserved exactly.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "nexus_storage_backend", "local",
                        raising=False)
    object_store.reset_store_cache_for_tests()
    try:
        assert object_store.is_object_backed() is False
        with tempfile.TemporaryDirectory() as tmp:
            crawl_id = uuid.uuid4().hex
            d = _write_crawl_evidence(Path(tmp), crawl_id)
            assert await object_store.publish_crawl_dir(TENANT, crawl_id, d) == 0
            assert await object_store.ensure_local(TENANT, crawl_id, d) == d
            assert (d / object_store.MANIFEST_FILENAME).is_file()
    finally:
        object_store.reset_store_cache_for_tests()


@needs_s3
@pytest.mark.asyncio
async def test_a_local_manifest_short_circuits_the_store(s3_env, monkeypatch):
    """Read-through: a manifest already on disk costs no network round trip."""
    calls = []

    async def _boom(*a, **kw):
        calls.append(a)
        raise AssertionError("the object store was consulted unnecessarily")
    monkeypatch.setattr(object_store, "fetch_crawl_dir", _boom)

    with tempfile.TemporaryDirectory() as tmp:
        crawl_id = uuid.uuid4().hex
        d = _write_crawl_evidence(Path(tmp), crawl_id)
        assert await object_store.ensure_local(TENANT, crawl_id, d) == d
    assert not calls


@pytest.mark.asyncio
async def test_a_configured_but_unreachable_store_fails_loudly(monkeypatch):
    """It must NOT degrade to the empty local directory.

    Silently returning nothing would recreate the exact bug this module exists
    to fix, while claiming to have fixed it — a completed crawl recorded as
    'no manifest produced' because of an infrastructure fault.
    """
    from app.config import settings
    monkeypatch.setenv("NEXUS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "unreachable")
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("S3_ACCESS_KEY", "x")
    monkeypatch.setenv("S3_SECRET_KEY", "y")
    monkeypatch.setattr(settings, "nexus_storage_backend", "s3", raising=False)
    object_store.reset_store_cache_for_tests()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(object_store.EvidenceStoreError):
                await object_store.ensure_local(
                    TENANT, uuid.uuid4().hex, Path(tmp) / "missing")
    finally:
        object_store.reset_store_cache_for_tests()


def test_a_crafted_relative_path_cannot_escape_the_prefix(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "nexus_storage_backend", "local",
                        raising=False)
    object_store.reset_store_cache_for_tests()
    try:
        with pytest.raises(object_store.EvidenceStoreError):
            object_store.object_key(TENANT, "abc123", "../../etc/passwd")
    finally:
        object_store.reset_store_cache_for_tests()


# ══════════════════════════════════════════════════════════════════════════
# CROSS-SERVICE CONTRACT — producer and consumer must agree
# ══════════════════════════════════════════════════════════════════════════

def _load_producer():
    import importlib.util
    mod = Path(__file__).resolve().parents[4] / \
        "engines" / "qe-explorer" / "app" / "evidence_publisher.py"
    assert mod.is_file(), f"producer module not found at {mod}"
    spec = importlib.util.spec_from_file_location("_ep", mod)
    ep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ep)
    return ep


def test_producer_key_layout_matches_the_sdk_build_key(monkeypatch):
    """THE contract that prevents total, silent evidence loss.

    The explorer cannot import ``nexus_sdk`` — the contained Playwright image
    deliberately does not carry it — so it mirrors the key layout BY HAND. If
    that hand-copy ever drifts from ``ArtifactStore.build_key``, the producer
    publishes where the consumer will never look: every crawl's evidence lost,
    with no error on either side.

    So this compares the producer's output against the REAL SDK key builder,
    not against a second copy of the same assumption.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "nexus_storage_backend", "local",
                        raising=False)
    object_store.reset_store_cache_for_tests()
    try:
        ep = _load_producer()
        for tenant, crawl_id, rel in (
            ("acme", "0123456789abcdef0123456789abcdef", "manifest.jsonl"),
            ("acme", "0123456789abcdef0123456789abcdef", "frames/001.png"),
            ("t-with-dash", "ff" * 16, "artifacts/policy.pdf"),
        ):
            assert ep.object_key(tenant, crawl_id, rel) == \
                object_store.object_key(tenant, crawl_id, rel), (
                f"producer and consumer disagree on the key for {rel!r}: "
                f"{ep.object_key(tenant, crawl_id, rel)!r} vs "
                f"{object_store.object_key(tenant, crawl_id, rel)!r}")
            assert ep.evidence_prefix(tenant, crawl_id) == \
                object_store.evidence_prefix(tenant, crawl_id)
    finally:
        object_store.reset_store_cache_for_tests()


def test_producer_and_consumer_agree_on_namespace_and_filename():
    ep = _load_producer()
    assert ep.MANIFEST_FILENAME == object_store.MANIFEST_FILENAME
    assert ep.EVIDENCE_NAMESPACE == object_store.EVIDENCE_NAMESPACE
    assert ep.EVIDENCE_SEGMENT == object_store.EVIDENCE_SEGMENT


def test_producer_reads_the_house_env_contract():
    """Both services must be configurable by ONE deployment's variables."""
    ep = _load_producer()
    assert ep.ENV_BACKEND == "NEXUS_STORAGE_BACKEND", (
        "the producer reads a private env var instead of the house contract — "
        "one deployment could then configure the consumer and not the producer")
    for name in ("S3_ENDPOINT", "S3_REGION", "S3_ACCESS_KEY", "S3_SECRET_KEY",
                 "S3_BUCKET"):
        assert name in (ep.ENV_S3_ENDPOINT, ep.ENV_S3_REGION,
                        ep.ENV_S3_ACCESS_KEY, ep.ENV_S3_SECRET_KEY,
                        ep.ENV_S3_BUCKET), f"{name} missing from the producer"


def test_producer_refuses_to_publish_without_a_tenant(tmp_path, monkeypatch):
    """Un-scoped evidence would land outside any tenant's prefix."""
    ep = _load_producer()
    monkeypatch.setenv("NEXUS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "b")
    crawl_id = uuid.uuid4().hex
    (tmp_path / crawl_id).mkdir()
    (tmp_path / crawl_id / "manifest.jsonl").write_text("{}\n")
    out = ep.publish_crawl_evidence(str(tmp_path), crawl_id, "")
    assert out["published"] is False and "tenant" in out["error"].lower()


def test_producer_is_a_noop_on_a_local_backend(tmp_path, monkeypatch):
    ep = _load_producer()
    monkeypatch.delenv("NEXUS_STORAGE_BACKEND", raising=False)
    assert ep.is_object_backed() is False
    out = ep.publish_crawl_evidence(str(tmp_path), uuid.uuid4().hex, "acme")
    assert out == {"published": False, "files": 0, "error": ""}
