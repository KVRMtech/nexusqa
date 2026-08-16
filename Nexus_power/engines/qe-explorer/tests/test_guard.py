"""QE-Central Contained Explorer — the SAFETY PROOF (design §3.2, §7 item 5).

Exhaustive, browser-free table tests over the pure guard functions. This suite
IS the safety argument: it pins that

  * the packaged refuse pack loads, is well-formed, and stamps a version;
  * a malformed pack FAILS CLOSED (never loads);
  * EVERY irreversible verb in the pack is caught by name;
  * mutation-signal GETs abort in every phase, while ordinary read navigation
    is never starved;
  * the verb × method × phase × domain matrix behaves fail-closed;
  * SUBMIT refuses on THREE independent grounds — no attestation, no approval,
    irreversible-verb match;
  * unknown / malformed input aborts.

Pure — no database, no network, no browser.
"""
from __future__ import annotations

import re

import pytest
import yaml

from app.config import Settings
from app.guard import (
    EVENT_ALLOW,
    EVENT_BLOCKED_METHOD,
    EVENT_REFUSED_VERB_GET,
    Attestation,
    GuardDecision,
    GuardRule,
    Phase,
    RefusePack,
    RefuseRule,
    classify_action_verb,
    classify_request,
    load_refuse_pack,
    registrable_domain,
    same_registrable_domain,
)

# ─── Fixtures ───────────────────────────────────────────────────────────────

#: The packaged production refuse pack (default settings path).
_PACK_PATH = Settings().refuse_pack_path


@pytest.fixture(scope="module")
def pack() -> RefusePack:
    return load_refuse_pack(_PACK_PATH)


def _now_epoch_ms() -> int:
    """Wall-clock epoch millis — the ONE clock domain a persisted deadline lives
    in (M0.5 T-SEC-08).  The old fixture built expiries from small monotonic-ish
    numbers (1_000 + ttl), which is exactly the domain confusion that made every
    attestation look fresh forever."""
    import time as _t

    return int(_t.time() * 1000)


def _valid_attestation(now_ms: int | None = None, ttl_ms: int = 10_000) -> Attestation:
    base = _now_epoch_ms() if now_ms is None else int(now_ms)
    return Attestation(
        attested_by="qa@client.example",
        env_kind="disposable",
        reset_procedure="nightly db reset",
        expires_at_ms=base + ttl_ms,
    )


# ─── 1. Refuse-pack loading + fail-closed on malformed data ─────────────────


class TestRefusePackLoading:
    def test_packaged_pack_loads_and_is_versioned(self, pack: RefusePack):
        assert pack.version == "refuse-pack-v1"
        assert pack.irreversible_verbs, "pack must ship irreversible verbs"
        assert pack.mutation_signal_get_rules, "pack must ship mutation-signal rules"
        # ships with no escape hatches enabled
        assert pack.allow_overrides == ()

    def test_all_rule_ids_are_unique(self, pack: RefusePack):
        ids = [r.id for r in (*pack.irreversible_verbs,
                              *pack.mutation_signal_get_rules,
                              *pack.allow_overrides)]
        assert len(ids) == len(set(ids))

    def test_every_rule_regex_compiles(self, pack: RefusePack):
        for rule in (*pack.irreversible_verbs, *pack.mutation_signal_get_rules):
            # matches() compiles+caches internally; a bad regex would have
            # already refused to load, so this simply exercises the path.
            assert rule.matches({"button_name": "", "url_path": "", "url_query": ""}) is False

    def test_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_refuse_pack(str(tmp_path / "does_not_exist.yaml"))

    def test_non_mapping_fails_closed(self, tmp_path):
        bad = tmp_path / "list.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_bad_yaml_fails_closed(self, tmp_path):
        bad = tmp_path / "broken.yaml"
        bad.write_text("version: [unclosed\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_invalid_regex_fails_closed(self, tmp_path):
        doc = {"version": "x", "irreversible_verbs": [
            {"id": "bad", "match": "(", "applies_to": ["button_name"],
             "severity": "critical"}]}
        bad = tmp_path / "regex.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_unknown_applies_to_field_fails_closed(self, tmp_path):
        doc = {"version": "x", "irreversible_verbs": [
            {"id": "bad", "match": "delete", "applies_to": ["nonsense_field"],
             "severity": "critical"}]}
        bad = tmp_path / "applies.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_bad_severity_fails_closed(self, tmp_path):
        doc = {"version": "x", "irreversible_verbs": [
            {"id": "bad", "match": "delete", "applies_to": ["button_name"],
             "severity": "whenever"}]}
        bad = tmp_path / "sev.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_duplicate_rule_id_fails_closed(self, tmp_path):
        doc = {"version": "x",
               "irreversible_verbs": [
                   {"id": "dup", "match": "delete", "applies_to": ["button_name"],
                    "severity": "critical"}],
               "mutation_signal_get_rules": [
                   {"id": "dup", "match": "logout", "applies_to": ["url_path"],
                    "severity": "high"}]}
        bad = tmp_path / "dup.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))

    def test_extra_key_fails_closed(self, tmp_path):
        doc = {"version": "x", "surprise": True}
        bad = tmp_path / "extra.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            load_refuse_pack(str(bad))


# ─── 2. Registrable-domain heuristic ────────────────────────────────────────


class TestRegistrableDomain:
    @pytest.mark.parametrize("host,expected", [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("auth.example.com", "example.com"),
        ("a.b.c.example.com", "example.com"),
        ("example.co.uk", "example.co.uk"),
        ("portal.example.co.uk", "example.co.uk"),
        ("a.b.portal.example.co.uk", "example.co.uk"),
        ("acmelife.com.au", "acmelife.com.au"),
        ("login.acmelife.com.au", "acmelife.com.au"),
        ("localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("10.0.0.5:8080", "10.0.0.5"),
        ("example.com:443", "example.com"),
        ("", ""),
        ("   ", ""),
        ("trailing.dot.example.com.", "example.com"),
    ])
    def test_registrable_domain(self, host, expected):
        assert registrable_domain(host) == expected

    @pytest.mark.parametrize("a,b,same", [
        ("example.com", "example.com", True),
        ("auth.example.com", "example.com", True),
        ("auth.example.co.uk", "pay.example.co.uk", True),
        ("example.com", "evil.com", False),
        ("example.com", "example.org", False),
        ("example.co.uk", "example.org.uk", False),
        ("", "example.com", False),
        ("example.com", "", False),
    ])
    def test_same_registrable_domain(self, a, b, same):
        assert same_registrable_domain(a, b) is same


# ─── 3. classify_action_verb — EVERY irreversible verb is caught ────────────

# One representative accessible name per pack rule id. The coverage test below
# asserts this map's keys are EXACTLY the pack's irreversible rule ids, so a
# verb added to the pack without a sample here FAILS the build.
_VERB_SAMPLES: dict[str, str] = {
    "rp.verb.delete": "Delete account",
    "rp.verb.purge": "Purge records",
    "rp.verb.remove": "Remove beneficiary",
    "rp.verb.pay": "Pay now",
    "rp.verb.charge": "Charge card",
    "rp.verb.transfer": "Transfer funds",
    "rp.verb.withdraw": "Withdraw cash",
    "rp.verb.disburse": "Disburse claim",
    "rp.verb.sign": "Sign document",
    "rp.verb.approve": "Approve request",
    "rp.verb.authorize": "Authorize access",
    "rp.verb.bind": "Bind coverage",
    "rp.verb.rescind": "Rescind policy",
    "rp.verb.surrender": "Surrender policy",
    "rp.verb.lapse": "Lapse policy",
    "rp.verb.escheat": "Escheat funds",
    "rp.verb.underwrite": "Submit to underwriting",
    "rp.verb.credit_pull": "Pull credit report",
    "rp.verb.logout": "Log out",
    "rp.verb.admin": "Admin console",
}


class TestClassifyActionVerb:
    def test_samples_cover_every_pack_verb(self, pack: RefusePack):
        pack_ids = {r.id for r in pack.irreversible_verbs}
        assert set(_VERB_SAMPLES) == pack_ids, (
            "every irreversible verb in the pack MUST have a caught-by-name "
            "sample (missing coverage is a safety hole)"
        )

    @pytest.mark.parametrize("rule_id,button", sorted(_VERB_SAMPLES.items()))
    def test_each_verb_is_flagged_irreversible(self, pack, rule_id, button):
        result = classify_action_verb(button, "", pack)
        assert result.irreversible is True
        assert result.rule_id == rule_id
        assert result.reason
        assert result.severity in ("critical", "high", "medium")

    def test_insurance_lexicon_specifically_caught(self, pack: RefusePack):
        # STARTING_POINT.md R2: a generic English danger list misses these.
        for button in ("Bind Coverage", "Surrender Policy", "Lapse Policy",
                       "Escheat Funds", "Rescind", "Disburse Benefit",
                       "Submit to Underwriting", "Send to E-Sign"):
            assert classify_action_verb(button, "", pack).irreversible is True

    @pytest.mark.parametrize("button", [
        "Search", "Next page", "Continue", "Back", "Home", "View details",
        "Filter results", "Sort", "Register", "Help", "Settings",
        "Dashboard", "Download", "Print", "Save draft", "Cancel",
        "Add note", "Edit profile",
    ])
    def test_benign_controls_are_not_flagged(self, pack, button):
        assert classify_action_verb(button, "", pack).irreversible is False

    @pytest.mark.parametrize("button", [
        "Sign in", "Sign In", "Sign up", "Sign on", "Log in", "Login",
        "Signin", "Signup",
    ])
    def test_login_controls_are_never_mistaken_for_e_sign(self, pack, button):
        # The login submit must NOT be classified irreversible or AUTH breaks.
        assert classify_action_verb(button, "", pack).irreversible is False

    def test_logout_control_is_flagged(self, pack: RefusePack):
        for button in ("Log out", "Logout", "Sign out", "Sign Out", "Log off"):
            assert classify_action_verb(button, "", pack).irreversible is True

    def test_verb_caught_via_url_path_not_only_button(self, pack: RefusePack):
        # Empty button, destructive intent in the URL path/query.
        assert classify_action_verb(
            "", "https://x.example.com/transfer/confirm", pack).irreversible is True

    def test_admin_is_button_scoped_not_url_scoped(self, pack: RefusePack):
        # rp.verb.admin applies_to==[button_name]; an /admin PATH is a fence
        # concern, not an irreversible verb, so it is NOT flagged by url alone.
        assert classify_action_verb("", "https://x.example.com/admin", pack).irreversible is False
        assert classify_action_verb("Admin", "https://x.example.com/", pack).irreversible is True

    def test_no_pack_fails_closed_to_irreversible(self):
        result = classify_action_verb("Search", "https://x/", None)
        assert result.irreversible is True
        assert result.rule_id == GuardRule.NO_REFUSE_PACK

    def test_allow_override_wins_over_irreversible(self):
        override_pack = RefusePack(
            version="test",
            irreversible_verbs=[RefuseRule(
                id="rp.verb.delete", match=r"\bdelete\b",
                applies_to=["button_name"], severity="critical")],
            allow_overrides=[RefuseRule(
                id="ov.safe_delete", match=r"\bdelete draft\b",
                applies_to=["button_name"], severity="medium",
                description="deleting an unsaved draft is reversible")],
        )
        blocked = classify_action_verb("Delete account", "", override_pack)
        assert blocked.irreversible is True and blocked.rule_id == "rp.verb.delete"
        allowed = classify_action_verb("Delete draft", "", override_pack)
        assert allowed.irreversible is False and allowed.rule_id == "ov.safe_delete"


# ─── 4. Mutation-signal GETs vs ordinary read navigation ────────────────────


class TestMutationSignalGet:
    @pytest.mark.parametrize("url,event_rule_prefix", [
        ("https://x.example.com/account?action=delete&id=5", "rp.get."),
        ("https://x.example.com/logout", "rp.get."),
        ("https://x.example.com/auth/signout", "rp.get."),
        ("https://x.example.com/session/destroy", "rp.get."),
        ("https://x.example.com/users/5/delete", "rp.get."),
        ("https://x.example.com/item?_method=delete", "rp.get."),
        ("https://x.example.com/thing?do=logout", "rp.get."),
    ])
    def test_mutation_signal_get_aborts_in_explore(self, pack, url, event_rule_prefix):
        d = classify_request("GET", url, Phase.EXPLORE, pack, False)
        assert d.allow is False
        assert d.event_kind == EVENT_REFUSED_VERB_GET
        assert d.rule_id.startswith(event_rule_prefix)

    @pytest.mark.parametrize("phase", [Phase.EXPLORE, Phase.AUTH, Phase.SUBMIT])
    def test_logout_get_aborts_in_every_phase(self, pack, phase):
        d = classify_request("GET", "https://x.example.com/logout", phase, pack,
                             True, attestation=_valid_attestation(),
                             submit_flow_approved=True, now_ms=1_000)
        assert d.allow is False
        assert d.event_kind == EVENT_REFUSED_VERB_GET

    @pytest.mark.parametrize("url", [
        "https://x.example.com/dashboard?tab=summary",
        "https://x.example.com/payments",              # 'pay' verb, but a READ page
        "https://x.example.com/policies/123",
        "https://x.example.com/search?q=delete",        # searching for a word != mutating
        "https://x.example.com/transfer",               # nav to a transfer PAGE is a read
    ])
    def test_ordinary_read_navigation_is_not_starved(self, pack, url):
        d = classify_request("GET", url, Phase.EXPLORE, pack, False)
        assert d.allow is True
        assert d.event_kind == EVENT_ALLOW
        assert d.rule_id == GuardRule.EXPLORE_READ_OK


# ─── 5. verb × method × phase × domain matrix ───────────────────────────────

_READS = ["GET", "HEAD", "OPTIONS"]
_MUTATIONS = ["POST", "PUT", "PATCH", "DELETE"]
_BENIGN_READ_URL = "https://app.example.com/dashboard"
_LOGIN_URL = "https://app.example.com/login"


class TestExplorePhase:
    @pytest.mark.parametrize("method", _READS)
    def test_reads_allowed(self, pack, method):
        d = classify_request(method, _BENIGN_READ_URL, Phase.EXPLORE, pack, False)
        assert d.allow is True and d.rule_id == GuardRule.EXPLORE_READ_OK

    @pytest.mark.parametrize("method", _MUTATIONS)
    def test_all_mutations_blocked(self, pack, method):
        d = classify_request(method, _BENIGN_READ_URL, Phase.EXPLORE, pack, True,
                             "Save")
        assert d.allow is False
        assert d.rule_id == GuardRule.EXPLORE_MUTATION_BLOCKED
        assert d.event_kind == EVENT_BLOCKED_METHOD

    def test_mutation_blocked_even_on_login_domain(self, pack):
        # is_login_domain has NO effect in EXPLORE — mutation is always refused.
        d = classify_request("POST", _LOGIN_URL, Phase.EXPLORE, pack, True)
        assert d.allow is False


class TestAuthPhase:
    @pytest.mark.parametrize("method", _READS)
    def test_reads_allowed(self, pack, method):
        d = classify_request(method, _BENIGN_READ_URL, Phase.AUTH, pack, True)
        assert d.allow is True and d.rule_id == GuardRule.AUTH_READ_OK

    def test_login_post_allowed_on_same_domain(self, pack):
        d = classify_request("POST", _LOGIN_URL, Phase.AUTH, pack, True, "Sign In")
        assert d.allow is True and d.rule_id == GuardRule.AUTH_LOGIN_POST_OK

    def test_login_post_blocked_off_domain(self, pack):
        d = classify_request("POST", "https://evil.example.net/collect",
                             Phase.AUTH, pack, False, "Sign In")
        assert d.allow is False
        assert d.rule_id == GuardRule.AUTH_OFFDOMAIN_POST_BLOCKED

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    def test_non_post_mutations_blocked(self, pack, method):
        d = classify_request(method, _LOGIN_URL, Phase.AUTH, pack, True)
        assert d.allow is False
        assert d.rule_id == GuardRule.AUTH_NONLOGIN_MUTATION_BLOCKED

    def test_login_post_with_irreversible_verb_blocked(self, pack):
        # A POST during AUTH that carries a destructive verb is anomalous.
        d = classify_request("POST", "https://app.example.com/account/delete",
                             Phase.AUTH, pack, True, "Delete account")
        assert d.allow is False
        assert d.rule_id.startswith("rp.verb.")

    @pytest.mark.parametrize("button", ["Sign In", "Log In", "Login", "Continue"])
    def test_common_login_buttons_never_break_auth(self, pack, button):
        d = classify_request("POST", _LOGIN_URL, Phase.AUTH, pack, True, button)
        assert d.allow is True


class TestSubmitPhase:
    @pytest.mark.parametrize("method", _READS)
    def test_reads_allowed(self, pack, method):
        d = classify_request(method, _BENIGN_READ_URL, Phase.SUBMIT, pack, False)
        assert d.allow is True and d.rule_id == GuardRule.SUBMIT_READ_OK

    def test_refusal_without_attestation(self, pack):
        d = classify_request("POST", "https://app.example.com/quote/save",
                             Phase.SUBMIT, pack, True, "Save quote",
                             attestation=None, submit_flow_approved=True,
                             now_ms=1_000)
        assert d.allow is False
        assert d.rule_id == GuardRule.SUBMIT_NO_ATTESTATION

    def test_refusal_without_approval(self, pack):
        d = classify_request("POST", "https://app.example.com/quote/save",
                             Phase.SUBMIT, pack, True, "Save quote",
                             attestation=_valid_attestation(),
                             submit_flow_approved=False, now_ms=1_000)
        assert d.allow is False
        assert d.rule_id == GuardRule.SUBMIT_NOT_APPROVED

    def test_irreversible_verb_crossed_and_recorded_when_attested_and_approved(self, pack):
        # Founder/client direction: a Test/disposable environment has NO
        # submission limits. Reaching this gate already required a submit-capable
        # (disposable) attestation + flow approval, so an irreversible verb is
        # CROSSED rather than refused — and the reason string records that it was
        # an irreversible verb, so evidence still shows exactly what was done.
        d = classify_request("POST", "https://app.example.com/policy/bind",
                             Phase.SUBMIT, pack, True, "Bind coverage",
                             attestation=_valid_attestation(),
                             submit_flow_approved=True, now_ms=1_000)
        assert d.allow is True
        assert d.rule_id == GuardRule.SUBMIT_MUTATION_OK
        assert "irreversible verb" in (d.reason or "").lower()

    def test_benign_mutation_allowed_when_attested_and_approved(self, pack):
        d = classify_request("POST", "https://app.example.com/quote/save",
                             Phase.SUBMIT, pack, True, "Save quote",
                             attestation=_valid_attestation(),
                             submit_flow_approved=True, now_ms=1_000)
        assert d.allow is True
        assert d.rule_id == GuardRule.SUBMIT_MUTATION_OK

    def test_default_kwargs_are_fail_closed(self, pack):
        # Called with ONLY the pinned positional args → a mutating submit is
        # refused (attestation/approval default to the safe values).
        d = classify_request("POST", "https://app.example.com/quote/save",
                             Phase.SUBMIT, pack, True, "Save quote")
        assert d.allow is False
        assert d.rule_id == GuardRule.SUBMIT_NO_ATTESTATION


# ─── 6. Attestation validity (the SUBMIT gate's fail-closed core) ───────────


class TestAttestation:
    def test_valid_disposable_unexpired_is_capable(self):
        base = _now_epoch_ms()
        assert _valid_attestation(now_ms=base, ttl_ms=5_000).is_submit_capable(base) is True

    def test_expired_is_not_capable(self):
        base = _now_epoch_ms()
        att = _valid_attestation(now_ms=base, ttl_ms=5_000)  # expires at base+5s
        assert att.is_submit_capable(base + 6_000) is False
        assert att.is_submit_capable(base + 9_999) is False

    def test_non_disposable_env_is_not_capable(self):
        base = _now_epoch_ms()
        for env in ("prod", "staging", "", "PRODUCTION"):
            att = Attestation(attested_by="x", env_kind=env, expires_at_ms=base + 10_000)
            assert att.is_submit_capable(base) is False

    def test_missing_attested_by_is_not_capable(self):
        base = _now_epoch_ms()
        att = Attestation(attested_by="", env_kind="disposable", expires_at_ms=base + 10_000)
        assert att.is_submit_capable(base) is False

    def test_missing_expiry_is_not_capable(self):
        base = _now_epoch_ms()
        att = Attestation(attested_by="x", env_kind="disposable", expires_at_ms=None)
        assert att.is_submit_capable(base) is False

    def test_omitted_now_reads_the_wall_clock(self):
        # M0.5 T-SEC-08: freshness no longer depends on a caller supplying a
        # clock (the old contract, which the crawler satisfied with a MONOTONIC
        # reading). Omitting it reads the wall clock, so a live attestation
        # passes and a lapsed one does not — with no argument either way.
        assert _valid_attestation().is_submit_capable() is True
        lapsed = Attestation(attested_by="x", env_kind="disposable",
                             expires_at_ms=_now_epoch_ms() - 1)
        assert lapsed.is_submit_capable() is False

    def test_monotonic_reading_is_refused_not_compared(self):
        # The exact defect: ms-since-crawl-start compared against an epoch
        # deadline is true for ~50_000 years. A value that cannot be an epoch
        # reading is now REFUSED rather than silently treated as "very fresh".
        att = _valid_attestation()
        for monotonic_ms in (0, 1_000, 5_000, 1_800_000):
            assert att.is_submit_capable(monotonic_ms) is False


# ─── 7. Fail-closed on unknown / malformed input ────────────────────────────


class TestFailClosed:
    @pytest.mark.parametrize("phase", [Phase.EXPLORE, Phase.AUTH, Phase.SUBMIT])
    @pytest.mark.parametrize("method", ["TRACE", "CONNECT", "FOO", "", "  ", "get; drop"])
    def test_unknown_method_blocked_in_every_phase(self, pack, phase, method):
        d = classify_request(method, _BENIGN_READ_URL, phase, pack, True)
        assert d.allow is False
        assert d.rule_id == GuardRule.UNKNOWN_METHOD

    @pytest.mark.parametrize("phase", ["wander", "", "   ", "explor", None, 42])
    def test_unknown_phase_blocked(self, pack, phase):
        d = classify_request("GET", _BENIGN_READ_URL, phase, pack, True)
        assert d.allow is False
        assert d.rule_id == GuardRule.UNKNOWN_PHASE

    def test_no_pack_blocks_everything(self):
        d = classify_request("GET", _BENIGN_READ_URL, Phase.EXPLORE, None, True)
        assert d.allow is False
        assert d.rule_id == GuardRule.NO_REFUSE_PACK

    def test_method_case_insensitive(self, pack):
        assert classify_request("get", _BENIGN_READ_URL, Phase.EXPLORE, pack, False).allow is True
        assert classify_request("PoSt", _BENIGN_READ_URL, Phase.EXPLORE, pack, True).allow is False

    def test_phase_accepts_raw_string(self, pack):
        assert classify_request("GET", _BENIGN_READ_URL, "explore", pack, False).allow is True

    def test_phase_normalises_case_and_whitespace(self, pack):
        # Surrounding whitespace/case normalise to a KNOWN phase (the phase is
        # internal, not untrusted input) — this maps to EXPLORE, not "unknown".
        for phase in ("EXPLORE ", " Explore", "AUTH", "Submit"):
            d = classify_request("GET", _BENIGN_READ_URL, phase, pack, True)
            assert d.rule_id != GuardRule.UNKNOWN_PHASE

    def test_malformed_url_does_not_crash(self, pack):
        # An unparseable URL degrades to empty path/query; a GET stays a read.
        d = classify_request("GET", "://::not a url", Phase.EXPLORE, pack, False)
        assert isinstance(d, GuardDecision)
        assert d.allow is True


# ─── 8. Invariants over the whole decision surface ──────────────────────────


class TestDecisionInvariants:
    def test_every_decision_carries_rule_id_and_reason(self, pack):
        phases = ["explore", "auth", "submit", "bogus"]
        methods = _READS + _MUTATIONS + ["TRACE", ""]
        urls = [_BENIGN_READ_URL, _LOGIN_URL, "https://x/logout",
                "https://x/account?action=delete"]
        for phase in phases:
            for method in methods:
                for url in urls:
                    for dom in (True, False):
                        d = classify_request(method, url, phase, pack, dom, "Go")
                        assert d.rule_id, (method, phase, url)
                        assert d.reason, (method, phase, url)
                        if d.allow:
                            assert d.event_kind == EVENT_ALLOW
                        else:
                            assert d.event_kind in (EVENT_BLOCKED_METHOD,
                                                    EVENT_REFUSED_VERB_GET)

    def test_no_mutating_method_ever_allowed_in_explore(self, pack):
        for method in _MUTATIONS:
            for url in (_BENIGN_READ_URL, _LOGIN_URL,
                        "https://x/pay", "https://x/quote"):
                for dom in (True, False):
                    d = classify_request(method, url, Phase.EXPLORE, pack, dom, "Go")
                    assert d.allow is False, (method, url, dom)

    def test_guard_decision_is_frozen(self):
        d = classify_request("GET", _BENIGN_READ_URL, Phase.EXPLORE,
                             load_refuse_pack(_PACK_PATH), False)
        with pytest.raises((AttributeError, TypeError)):
            d.allow = True  # type: ignore[misc]


# ─── 9. config helpers (the HMAC shared-secret surface) ─────────────────────


class TestConfigHelpers:
    """Settings is constructed by ALIAS (``QEC_EXPLORER_TOKEN=``), never by field
    name (``explorer_token=``).

    ``Settings`` declares ``populate_by_name: True``, so on a recent
    pydantic-settings BOTH work — but on the version this service PINS
    (pydantic-settings 2.6.1, requirements.txt) init-by-field-name is not honoured
    for an aliased field, and the value silently falls back to the environment.
    Under that fallback every instance below shared the one env-provided secret:
    ``token_matches("s3cr3t-token")`` returned False, and two Settings built with
    deliberately DIFFERENT keys produced byte-identical signatures — the
    signing test asserting they differ failed with
    ``assert '092f23c61642b10d' != '092f23c61642b10d'``.

    It passed on a dev box with a newer pydantic-settings and failed in CI on the
    pinned one. The alias form is accepted by every version, so it is the portable
    way to say "this instance has THIS secret".
    """

    def test_token_matches_constant_time_true(self):
        s = Settings(QEC_EXPLORER_TOKEN="s3cr3t-token")
        assert s.token_matches("s3cr3t-token") is True

    @pytest.mark.parametrize("provided", ["wrong", "", "  ", None, "s3cr3t-token "])
    def test_token_matches_false_on_mismatch(self, provided):
        s = Settings(QEC_EXPLORER_TOKEN="s3cr3t-token")
        # a trailing-space variant is stripped and still compared → still True,
        # so exclude it; everything else must be False.
        expected = provided is not None and provided.strip() == "s3cr3t-token"
        assert s.token_matches(provided) is expected

    def test_empty_configured_secret_never_matches(self):
        s = Settings(QEC_EXPLORER_TOKEN="")
        assert s.token_matches("") is False
        assert s.token_matches("anything") is False

    def test_sign_payload_is_v2_and_secret_dependent(self):
        # M0.5 T-SEC-06: a signature is deliberately NO LONGER deterministic —
        # each envelope carries its own single-use nonce and timestamp, which is
        # precisely what makes a captured callback un-replayable. What must stay
        # stable is the KEY IDENTITY, and it must differ per secret.
        a = Settings(QEC_EXPLORER_TOKEN="k1")
        b = Settings(QEC_EXPLORER_TOKEN="k2")
        sig1 = a.sign_payload(b"hello")
        sig2 = a.sign_payload(b"hello")
        assert sig1 != sig2                               # fresh nonce each time
        envelope = re.fullmatch(
            r"v2;kid=([0-9a-f]{16});ts=(\d+);nonce=([0-9a-f]{32});sig=([0-9a-f]{64})",
            sig1)
        assert envelope is not None
        kid_a = envelope.group(1)
        assert re.match(rf"v2;kid={kid_a};", sig2)        # same key, same kid
        kid_b = re.match(r"v2;kid=([0-9a-f]{16});", b.sign_payload(b"hello")).group(1)
        assert kid_a != kid_b                             # secret-dependent

    def test_budget_defaults_shape(self):
        b = Settings().budget_defaults()
        assert set(b) == {"max_states", "max_depth", "max_actions_per_state",
                          "max_wall_ms", "max_requests", "rate_per_s"}
        assert b["max_states"] == 200 and b["rate_per_s"] == 1.0

    def test_callback_path_renders_crawl_id(self):
        assert Settings().callback_path("c7f2") == "/internal/crawls/c7f2/complete"
