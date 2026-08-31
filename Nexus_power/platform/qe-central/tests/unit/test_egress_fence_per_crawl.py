"""TEAM A / PHASE A — the per-crawl egress fence PRODUCER, unit-proven.

The frozen shape is ``contracts/fleet_egress_fence_v1.json``; this file is the
qe-central (producer) half of that contract — the explorer/squid consumer half
is ``engines/qe-explorer/tests/test_fence_identity.py``. Neither service can
import the other, so each asserts the same data in its own process.

Everything here runs on real files in a tmp dir, no database, no mocks of the
module under test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.controlplane.scheduling import egress_fence as ef


def _contract() -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "fleet_egress_fence_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/fleet_egress_fence_v1.json not found above %s" % here)


CONTRACT = _contract()


def _root(tmp_path) -> str:
    return str(tmp_path / "allowed_domains.txt")


# ── the contract, held by the producer ─────────────────────────────────────

def test_the_layout_and_id_pattern_are_the_contracts():
    assert ef.PER_CRAWL_DIR == CONTRACT["layout"]["per_crawl_dir"]
    assert ef.ACL_INCLUDE == CONTRACT["layout"]["acl_include"]
    assert ef.RELOAD_STAMP == CONTRACT["layout"]["reload_stamp"]
    assert ef.CRAWL_ID_RE.pattern == CONTRACT["crawl_id_pattern"], (
        "the producer's crawl-id pattern drifted from the contract — the "
        "explorer validates the SAME pattern for the proxy login, and a "
        "mismatch strands a dispatchable crawl at the fence")


def test_the_generated_conf_matches_the_contract_sample_verbatim(tmp_path):
    """The three ACL lines squid will read, byte-for-byte as frozen."""
    sample = CONTRACT["sample"]
    ef.write_crawl_fence(sample["domains"], _root(tmp_path),
                         crawl_id=sample["crawl_id"])
    conf = (tmp_path / ef.ACL_INCLUDE).read_text()
    for line in sample["crawls_conf_lines"]:
        assert line in conf, f"generated crawls.conf lost contract line: {line}"
    body = (tmp_path / ef.PER_CRAWL_DIR /
            f"allowlist.{sample['crawl_id']}.txt").read_text()
    assert body == sample["per_crawl_file_body"]


# ── behaviour ──────────────────────────────────────────────────────────────

def test_two_crawls_coexist_and_release_removes_exactly_one(tmp_path):
    root = _root(tmp_path)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    ef.write_crawl_fence(["b.example"], root, crawl_id="cb")
    conf = (tmp_path / ef.ACL_INCLUDE).read_text()
    assert "proxy_auth ca" in conf and "proxy_auth cb" in conf

    stamp_before = (tmp_path / ef.RELOAD_STAMP).read_text()
    assert ef.release_crawl_fence(root, "ca") is True
    conf = (tmp_path / ef.ACL_INCLUDE).read_text()
    assert "proxy_auth ca" not in conf, "a released crawl kept its ACL"
    assert "proxy_auth cb" in conf, "releasing one crawl destroyed another's fence"
    assert not ef.crawl_fence_path(root, "ca").exists()
    assert ef.crawl_fence_path(root, "cb").exists()
    assert (tmp_path / ef.RELOAD_STAMP).read_text() != stamp_before, (
        "the release did not bump the reload stamp — squid would keep the "
        "released crawl's ACL until the next unrelated dispatch")


def test_release_is_idempotent_and_never_raises(tmp_path):
    root = _root(tmp_path)
    assert ef.release_crawl_fence(root, "never-written") is False
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    assert ef.release_crawl_fence(root, "ca") is True
    assert ef.release_crawl_fence(root, "ca") is False


def test_the_conf_names_squids_view_of_the_files(tmp_path, monkeypatch):
    """qe-central writes at ITS mount; squid reads at ITS OWN. The conf must
    name squid's — a conf naming /qec/... would make every dstdomain file
    unopenable inside the proxy container and squid FATALs on reconfigure."""
    monkeypatch.setenv(ef.ENV_SQUID_ROOT, "/etc/squid/allowlist")
    ef.write_crawl_fence(["a.example"], _root(tmp_path), crawl_id="ca")
    conf = (tmp_path / ef.ACL_INCLUDE).read_text()
    assert '"/etc/squid/allowlist/crawls/allowlist.ca.txt"' in conf
    assert str(tmp_path) not in conf, (
        "the generated conf leaked qe-central's own mount path to squid")


def test_an_unsafe_crawl_id_or_domain_is_refused(tmp_path):
    root = _root(tmp_path)
    for bad_id in ("", "has space", 'x"quote', "a/slash", "-leading", "x" * 51):
        with pytest.raises(ef.FenceError):
            ef.write_crawl_fence(["a.example"], root, crawl_id=bad_id)
    for bad_domain in ("two words", "new\nline.example", 'quo"te.example', ""):
        with pytest.raises(ef.FenceError):
            ef.write_crawl_fence([bad_domain], root, crawl_id="ca")
    with pytest.raises(ef.FenceError):
        ef.write_crawl_fence([], root, crawl_id="ca")
    # nothing reached disk for any refusal
    assert not (tmp_path / ef.PER_CRAWL_DIR).exists() or not list(
        (tmp_path / ef.PER_CRAWL_DIR).glob("allowlist.*"))


def test_a_stray_file_in_the_crawls_dir_never_reaches_the_conf(tmp_path):
    """Only files matching the contract layout AND the id pattern become ACLs
    — a stray file cannot smuggle an allow rule into squid."""
    root = _root(tmp_path)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    crawl_dir = tmp_path / ef.PER_CRAWL_DIR
    (crawl_dir / "allowlist.bad id!.txt").write_text("evil.example\n")
    (crawl_dir / "notes.txt").write_text("evil.example\n")
    fenced = ef.regenerate(root)
    assert fenced == ["ca"]
    conf = (tmp_path / ef.ACL_INCLUDE).read_text()
    assert "bad id" not in conf and "notes" not in conf


def test_an_orphaned_fence_ages_out(tmp_path, monkeypatch):
    """The GC backstop: a crash between dispatch and completion cannot keep a
    crawl's egress permission alive forever."""
    root = _root(tmp_path)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    monkeypatch.setenv(ef.ENV_FENCE_MAX_AGE, "0")
    import time
    time.sleep(0.05)                      # let mtime age past 0
    fenced = ef.regenerate(root)
    assert fenced == [], "an aged-out fence was still published to squid"
    assert not ef.crawl_fence_path(root, "ca").exists()
    # CONTROL: with the default age, the same fence survives regeneration.
    monkeypatch.delenv(ef.ENV_FENCE_MAX_AGE)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    assert ef.regenerate(root) == ["ca"]


def test_no_tmp_residue_survives(tmp_path):
    root = _root(tmp_path)
    for i in range(5):
        ef.write_crawl_fence([f"h{i}.example"], root, crawl_id=f"c{i}")
    ef.release_crawl_fence(root, "c0")
    residue = [p.name for p in tmp_path.rglob("*.tmp-*")]
    residue += [p.name for p in tmp_path.rglob("*.releasing")]
    assert not residue, f"atomic-write residue left behind: {residue}"


def test_the_stamp_content_changes_every_regeneration(tmp_path):
    """The proxy watcher compares CONTENT: two regenerations in one second
    must still read as two changes (an mtime compare could collapse them)."""
    root = _root(tmp_path)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    s1 = (tmp_path / ef.RELOAD_STAMP).read_text()
    ef.write_crawl_fence(["b.example"], root, crawl_id="cb")
    s2 = (tmp_path / ef.RELOAD_STAMP).read_text()
    assert s1 != s2


def test_regenerate_reports_what_it_fenced(tmp_path):
    root = _root(tmp_path)
    ef.write_crawl_fence(["a.example"], root, crawl_id="ca")
    ef.write_crawl_fence(["b.example"], root, crawl_id="cb")
    assert sorted(ef.regenerate(root)) == ["ca", "cb"]


def test_tag_rule_matches_the_contract():
    assert ef._tag("ab-1_c") == re.sub(r"[^A-Za-z0-9]", "_", "ab-1_c")
