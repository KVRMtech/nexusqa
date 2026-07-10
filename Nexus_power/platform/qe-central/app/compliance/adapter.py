"""QE-Central Phase-8 — compliance-framework adapters (design SEAM E).

WHAT THIS IS
============
A set of **READ-ONLY PROJECTIONS** that re-shape the evidence VKPower-Verdict
already captures — the hash-chained ``verdict_events``, the reproducible
``decision_dossiers``, the governed ``verification_waivers`` and the immutable
``audit_log`` — into the control-mapping shape a specific compliance framework
expects (SOC 2, the NAIC Model Audit Rule, the EU AI-Act technical-documentation
profile).

The load-bearing property is that this module **captures nothing new and mutates
nothing**.  It is a pure function of an :class:`EvidenceBundle` snapshot: the
same bundle always projects to the same report (byte-for-byte, verifiable via
:func:`report_digest`), and projecting never touches the database, the network,
an LLM, or the caller's input structures.  A regulator can therefore replay a
report from the raw evidence and get the identical digest — the projection adds
no trust of its own, it only *re-frames* trust that the hash chains already
carry.

HONESTY DISCIPLINE (never green-wash a control)
===============================================
Every control carries an explicit ``enforcement`` label — ``code_enforced`` (a
structural code invariant, proven by the existing unit/contract suite),
``operational`` (depends on the running deployment or an external auditor and is
therefore NOT provable from evidence rows here), or ``hybrid`` (a code invariant
with an operational dependency, e.g. envelope encryption whose KMS key must be
provisioned out-of-band).  An operational control is projected as ``operational``
status, never as ``satisfied`` — the projection refuses to claim an auditor's
sign-off or a restore-drill it cannot see.  Evidence-backed controls degrade
honestly too: a control expecting verdict rows in a window that has none is
``not_evidenced`` (the mechanism exists, no activity was captured), and a verdict
chain that fails re-derivation is ``partial`` (tamper detected), never hidden.

ZERO LLM.  No network.  No mutation.  Pure functions over a snapshot.
"""
from __future__ import annotations

import abc
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

# ─── Versioning (bump when the projection shape or control set changes) ─────
ADAPTER_VERSION = "compliance-adapter-v1"

#: The verdict_events hash-chain registry the projection re-derives against.
#: Mirrors ``platform/api/app/services/test_factory/verdict_events.REGISTRY_VERSION``
#: — the chain re-derivation below is byte-compatible with how the chain was
#: WRITTEN, so a report's chain verdict matches what the writer computed.
CHAIN_REGISTRY_VERSION = "auditor-v1+lint-v1"

# ─── Control status vocabulary (honest, never green-washed) ─────────────────
STATUS_SATISFIED = "satisfied"          # implemented AND evidenced in-window
STATUS_PARTIAL = "partial"              # implemented but evidence incomplete/tampered
STATUS_NOT_EVIDENCED = "not_evidenced"  # code-enforced, no runtime rows in-window
STATUS_OPERATIONAL = "operational"      # needs the running deployment / an auditor

# ─── Enforcement classification (code invariant vs operational vs both) ─────
ENFORCE_CODE = "code_enforced"
ENFORCE_OPERATIONAL = "operational"
ENFORCE_HYBRID = "hybrid"

# ─── Evidence kinds a control projects over (the shared substrate) ──────────
KIND_VERDICT_CHAIN = "verdict_chain"    # verdict_events (+ chain re-derivation)
KIND_DOSSIERS = "dossiers"              # decision_dossiers
KIND_AUDIT_LOG = "audit_log"            # audit_log
KIND_WAIVERS = "waivers"                # verification_waivers (governed exceptions)
KIND_CODE = "code_invariant"            # a structural code mechanism (test-proven)
KIND_OPERATIONAL = "operational"        # a deployment/external-auditor dependency

#: How many evidence references a single control lists (report stays bounded;
#: the ``count`` field always reports the FULL, un-capped total).
_REF_CAP = 50


# ══════════════════════════════════════════════════════════════════════════
#  Verdict hash-chain re-derivation (tamper-evidence projection)
# ══════════════════════════════════════════════════════════════════════════
#
# Byte-compatible mirror of verdict_events._canonical_payload / the chain hash:
# each event's chain_hash = sha256(prev_chain_hash + canonical_payload).  Re-
# deriving the chain from the stored rows and comparing to the stored hashes is
# how a regulator (and this projection) proves the evidence was never rewritten.

_CHAIN_CORE_KEYS = (
    "tenant_id", "artifact_id", "test_id", "version", "registry_version",
    "source", "overall", "decision", "axes", "gaps",
)


def canonical_verdict_payload(verdict: Mapping[str, Any]) -> str:
    """Canonical JSON of a verdict's chain-core fields (writer-compatible).

    Mirrors ``verdict_events._canonical_payload`` exactly (same key set, same
    ``sort_keys`` + compact separators + ``default=str``) so the string hashed
    here is byte-identical to the one hashed at write time.
    """
    core = {
        "tenant_id": verdict.get("tenant_id"),
        "artifact_id": verdict.get("artifact_id"),
        "test_id": verdict.get("test_id"),
        "version": verdict.get("version"),
        "registry_version": verdict.get("registry_version") or CHAIN_REGISTRY_VERSION,
        "source": verdict.get("source"),
        "overall": int(verdict.get("overall") or 0),
        "decision": str(verdict.get("decision") or ""),
        "axes": dict(verdict.get("axes") or {}),
        "gaps": int(verdict.get("gaps") or 0),
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)


def compute_chain_hash(prev_chain_hash: str, verdict: Mapping[str, Any]) -> str:
    """The chain hash for ``verdict`` following ``prev_chain_hash`` (sha256)."""
    payload = canonical_verdict_payload(verdict)
    return hashlib.sha256((str(prev_chain_hash or "") + payload).encode("utf-8")).hexdigest()


def _created_sort_key(verdict: Mapping[str, Any]) -> tuple[str, str]:
    """A stable ascending order key for a verdict (created_at, then id).

    ``created_at`` may be a datetime or an ISO string; both sort correctly as
    ISO text.  The ``verdict_id`` tie-break makes the order total (deterministic)
    even when two rows share a timestamp.
    """
    created = verdict.get("created_at")
    if isinstance(created, datetime):
        created_key = created.isoformat()
    else:
        created_key = str(created or "")
    return (created_key, str(verdict.get("verdict_id") or ""))


def verify_verdict_chains(verdicts: Iterable[Mapping[str, Any]]) -> dict:
    """Re-derive every per-(tenant, artifact, test) verdict chain and report it.

    The chain is grouped by ``(tenant_id, artifact_id, test_id)`` and, within a
    group, ordered ascending by ``(created_at, verdict_id)`` — the same order the
    writer appended in.  Each event's stored ``chain_hash`` is compared to the
    hash re-derived from the previous link; any mismatch (or a missing hash) is a
    tamper signal recorded in ``breaks``.

    Returns a deterministic dict::

        {"verified": bool, "chains_checked": int, "events_checked": int,
         "breaks": [{"tenant_id","artifact_id","test_id","verdict_id",
                     "expected","stored","reason"}, ...]}

    ``verified`` is True iff every checked event re-derives to its stored hash
    (vacuously True for an empty evidence set — nothing to contradict).
    """
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for v in verdicts:
        key = (
            str(v.get("tenant_id") or ""),
            str(v.get("artifact_id") or ""),
            str(v.get("test_id") or ""),
        )
        groups.setdefault(key, []).append(v)

    breaks: list[dict] = []
    events_checked = 0
    for key in sorted(groups.keys()):
        prev = ""
        for v in sorted(groups[key], key=_created_sort_key):
            events_checked += 1
            stored = str(v.get("chain_hash") or "")
            expected = compute_chain_hash(prev, v)
            if not stored:
                breaks.append({
                    "tenant_id": key[0], "artifact_id": key[1], "test_id": key[2],
                    "verdict_id": str(v.get("verdict_id") or ""),
                    "expected": expected, "stored": "", "reason": "missing_chain_hash",
                })
            elif stored != expected:
                breaks.append({
                    "tenant_id": key[0], "artifact_id": key[1], "test_id": key[2],
                    "verdict_id": str(v.get("verdict_id") or ""),
                    "expected": expected, "stored": stored, "reason": "hash_mismatch",
                })
            # The chain always advances on the STORED hash: a downstream link is
            # judged against what was actually persisted, so one tampered row
            # surfaces exactly one break rather than cascading through the tail.
            prev = stored or expected
    return {
        "verified": not breaks,
        "chains_checked": len(groups),
        "events_checked": events_checked,
        "breaks": breaks,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Evidence snapshot (the read-only input the projection consumes)
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class EvidenceWindow:
    """The time window an evidence bundle covers (open on any None bound)."""

    start: datetime | None = None
    end: datetime | None = None
    hours: float | None = None

    def as_dict(self) -> dict:
        """JSON-serialisable projection (datetimes → ISO strings)."""
        return {
            "start": self.start.isoformat() if isinstance(self.start, datetime) else None,
            "end": self.end.isoformat() if isinstance(self.end, datetime) else None,
            "hours": self.hours,
        }


def _copy_rows(rows: Iterable[Mapping[str, Any]] | None) -> tuple[dict, ...]:
    """Defensive deep-ish copy of evidence rows into immutable tuples of dicts.

    Each row is shallow-copied so the projection can never mutate the caller's
    structures and the bundle is a stable snapshot.
    """
    return tuple(dict(r) for r in (rows or []) if isinstance(r, Mapping))


@dataclass(frozen=True)
class EvidenceBundle:
    """An immutable, tenant-scoped snapshot of the evidence to project.

    Built via :meth:`build`, which applies a DEFENSIVE tenant filter (defence in
    depth over Postgres RLS + the loader's ``WHERE tenant_id`` clause): a row
    whose ``tenant_id`` does not match is dropped, so a tenant-A report can never
    surface tenant-B evidence even if an upstream leak occurred.
    """

    tenant_id: str
    window: EvidenceWindow
    verdicts: tuple[dict, ...] = field(default_factory=tuple)
    dossiers: tuple[dict, ...] = field(default_factory=tuple)
    waivers: tuple[dict, ...] = field(default_factory=tuple)
    audit: tuple[dict, ...] = field(default_factory=tuple)

    @classmethod
    def build(
        cls,
        *,
        tenant_id: str,
        window: EvidenceWindow | None = None,
        verdicts: Iterable[Mapping[str, Any]] | None = None,
        dossiers: Iterable[Mapping[str, Any]] | None = None,
        waivers: Iterable[Mapping[str, Any]] | None = None,
        audit: Iterable[Mapping[str, Any]] | None = None,
    ) -> "EvidenceBundle":
        """Build a tenant-scoped snapshot, dropping any foreign-tenant row."""
        tid = str(tenant_id or "").strip()
        if not tid:
            raise ValueError("tenant_id is required to build an evidence bundle")

        def _scoped(rows: Iterable[Mapping[str, Any]] | None) -> tuple[dict, ...]:
            return tuple(
                r for r in _copy_rows(rows)
                if str(r.get("tenant_id") or "") == tid
            )

        return cls(
            tenant_id=tid,
            window=window or EvidenceWindow(),
            verdicts=_scoped(verdicts),
            dossiers=_scoped(dossiers),
            waivers=_scoped(waivers),
            audit=_scoped(audit),
        )


# ══════════════════════════════════════════════════════════════════════════
#  Control specification (static) + resolution (against the bundle)
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ControlSpec:
    """One framework control and how VKPower-Verdict implements it.

    ``evidence_kind`` names WHICH shared evidence source drives the control's
    in-window status; ``enforcement`` is the honesty label (code vs operational
    vs hybrid); ``artifact`` is the module / DDL / doc an auditor inspects.
    """

    control_id: str
    criterion: str
    title: str
    implementation: str
    evidence_kind: str
    enforcement: str
    artifact: str
    operational_note: str = ""


def _refs_and_count(
    kind: str, bundle: EvidenceBundle, chain: Mapping[str, Any],
) -> tuple[list[str], int | None, str]:
    """Return ``(refs, count, status)`` for one evidence kind over the bundle.

    ``refs`` is sorted + capped (deterministic); ``count`` is the FULL total
    (None for non-row kinds).  ``status`` is the row-driven status BEFORE the
    enforcement label is applied by :func:`_resolve`.
    """
    if kind == KIND_VERDICT_CHAIN:
        refs = sorted({
            str(v.get("chain_hash") or v.get("verdict_id") or "")
            for v in bundle.verdicts
        } - {""})
        count = len(bundle.verdicts)
        if count == 0:
            status = STATUS_NOT_EVIDENCED
        elif chain.get("verified"):
            status = STATUS_SATISFIED
        else:
            status = STATUS_PARTIAL
        return refs[:_REF_CAP], count, status

    if kind == KIND_DOSSIERS:
        refs = sorted({
            str(d.get("verdict_id") or d.get("chain_hash") or "")
            for d in bundle.dossiers
        } - {""})
        count = len(bundle.dossiers)
        return refs[:_REF_CAP], count, (STATUS_SATISFIED if count else STATUS_NOT_EVIDENCED)

    if kind == KIND_AUDIT_LOG:
        refs = sorted({str(a.get("log_id") or "") for a in bundle.audit} - {""})
        count = len(bundle.audit)
        return refs[:_REF_CAP], count, (STATUS_SATISFIED if count else STATUS_NOT_EVIDENCED)

    if kind == KIND_WAIVERS:
        # The GUARANTEE is that every exception is governed (owner + reason +
        # expiry, never silent) — a code invariant that holds at ANY count, so
        # zero recorded waivers is still ``satisfied`` (no exceptions were taken).
        refs = sorted({str(w.get("waiver_id") or "") for w in bundle.waivers} - {""})
        return refs[:_REF_CAP], len(bundle.waivers), STATUS_SATISFIED

    if kind == KIND_OPERATIONAL:
        return [], None, STATUS_OPERATIONAL

    # KIND_CODE (and any unknown kind, treated conservatively as code-invariant).
    return [], None, STATUS_SATISFIED


def _resolve(spec: ControlSpec, bundle: EvidenceBundle, chain: Mapping[str, Any]) -> dict:
    """Project one control against the bundle into its report shape.

    Combines the row-driven status with the enforcement label: an
    ``operational`` control is always ``operational`` status (never claimed
    satisfied from evidence it cannot see); a ``hybrid`` control keeps its
    code-side status and surfaces the operational dependency in
    ``operational_note``.
    """
    refs, count, row_status = _refs_and_count(spec.evidence_kind, bundle, chain)

    if spec.enforcement == ENFORCE_OPERATIONAL:
        status = STATUS_OPERATIONAL
    else:
        status = row_status

    artifact_refs = refs if refs else [spec.artifact]
    projection = {
        "control_id": spec.control_id,
        "criterion": spec.criterion,
        "title": spec.title,
        "implementation": spec.implementation,
        "enforcement": spec.enforcement,
        "status": status,
        "evidence": {
            "kind": spec.evidence_kind,
            "count": count,
            "refs": artifact_refs[:_REF_CAP],
            "artifact": spec.artifact,
        },
    }
    if spec.operational_note:
        projection["operational_note"] = spec.operational_note
    return projection


# ══════════════════════════════════════════════════════════════════════════
#  Digest (deterministic, hash-verifiable)
# ══════════════════════════════════════════════════════════════════════════


def report_digest(report: Mapping[str, Any]) -> str:
    """A deterministic ``sha256:`` digest of a projected report.

    Computed over the canonical JSON of the report with the ``report_digest``
    field itself excluded, so a verifier can recompute it from the report body
    and confirm the projection was not altered after the fact.  Excludes ``meta``
    (wall-clock / requester context the HTTP layer attaches) so the digest stays
    a pure function of the evidence, not of WHEN the report was rendered.
    """
    body = {k: v for k, v in report.items() if k not in ("report_digest", "meta")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
#  Adapter base + concrete framework projections
# ══════════════════════════════════════════════════════════════════════════


class ComplianceAdapter(abc.ABC):
    """Base class for a framework projection over the shared evidence.

    Subclasses declare ``framework`` / ``title`` and implement
    :meth:`control_specs`; the base :meth:`project` does all shared work
    (chain re-derivation, per-control resolution, totals, digest) so every
    framework projects deterministically and identically-structured.
    """

    framework: ClassVar[str]
    title: ClassVar[str]
    version: ClassVar[str] = ADAPTER_VERSION

    @abc.abstractmethod
    def control_specs(self) -> list[ControlSpec]:
        """The framework's control set (order is stable → deterministic)."""
        raise NotImplementedError

    def control_count(self) -> int:
        """Number of controls this adapter projects."""
        return len(self.control_specs())

    def _attestations(self, specs: list[ControlSpec]) -> dict:
        """Which controls are code-enforced vs operational vs hybrid (honest)."""
        by_enforcement: dict[str, list[str]] = {
            ENFORCE_CODE: [], ENFORCE_OPERATIONAL: [], ENFORCE_HYBRID: [],
        }
        for s in specs:
            by_enforcement.setdefault(s.enforcement, []).append(s.control_id)
        return {
            "code_enforced": sorted(by_enforcement.get(ENFORCE_CODE, [])),
            "hybrid": sorted(by_enforcement.get(ENFORCE_HYBRID, [])),
            "operational": sorted(by_enforcement.get(ENFORCE_OPERATIONAL, [])),
            "note": (
                "code_enforced controls are structural invariants proven by the "
                "unit/contract suite; hybrid controls add an operational dependency "
                "(e.g. KMS key provisioning); operational controls require the "
                "running deployment or an external auditor and are NOT claimed "
                "satisfied from evidence rows."
            ),
        }

    def project(self, bundle: EvidenceBundle) -> dict:
        """Project the bundle into this framework's report shape (pure).

        Deterministic: the same bundle always yields the same dict (verifiable
        via :func:`report_digest`).  Never mutates the bundle, never does I/O.
        """
        chain = verify_verdict_chains(bundle.verdicts)
        specs = self.control_specs()
        controls = [_resolve(s, bundle, chain) for s in specs]

        totals: dict[str, int] = {
            STATUS_SATISFIED: 0, STATUS_PARTIAL: 0,
            STATUS_NOT_EVIDENCED: 0, STATUS_OPERATIONAL: 0,
        }
        for c in controls:
            totals[c["status"]] = totals.get(c["status"], 0) + 1

        report: dict = {
            "framework": self.framework,
            "framework_title": self.title,
            "adapter_version": self.version,
            "tenant_id": bundle.tenant_id,
            "window": bundle.window.as_dict(),
            "evidence_summary": {
                "verdicts": len(bundle.verdicts),
                "dossiers": len(bundle.dossiers),
                "waivers": len(bundle.waivers),
                "audit_events": len(bundle.audit),
                "chain": chain,
            },
            "control_totals": totals,
            "controls": controls,
            "attestations": self._attestations(specs),
        }
        report["report_digest"] = report_digest(report)
        return report


# ── SOC 2 Trust Services Criteria ───────────────────────────────────────────


class SOC2Adapter(ComplianceAdapter):
    """SOC 2 Trust Services Criteria projection (Security / Availability /
    Confidentiality / Processing Integrity)."""

    framework = "soc2"
    title = "SOC 2 Trust Services Criteria"

    def control_specs(self) -> list[ControlSpec]:
        return [
            # ── Security (Common Criteria) ──────────────────────────────────
            ControlSpec(
                "CC6.1", "Security",
                "Logical access — tenant isolation (Row-Level Security)",
                "Every session sets the nexus.current_tenant_id GUC inside its "
                "transaction; all tenant tables carry FORCE ROW LEVEL SECURITY + "
                "a tenant_isolation policy, proven at runtime through the least-"
                "privilege roles.",
                KIND_CODE, ENFORCE_CODE,
                "app/db/__init__.py + alembic_qec qec_001 (FORCE RLS) + "
                "tests/contract/test_rls_isolation.py",
            ),
            ControlSpec(
                "CC6.1b", "Security",
                "Service-token audience isolation (no VKPower↔Verdict bleed)",
                "Inbound JWTs are verified against QEC_JWT_AUDIENCE; a foreign "
                "audience is always rejected (401), an untenanted token is refused "
                "— no cross-service token replay.",
                KIND_CODE, ENFORCE_CODE,
                "app/auth.py + app/service_token.py + tests/unit/test_boot_and_aud.py",
            ),
            ControlSpec(
                "CC6.3", "Security",
                "Least-privilege database roles (separation of duties)",
                "The owning role qec has ZERO grants on the nexus DB; the writer "
                "role qec_substrate holds only INSERT/SELECT on the substrate "
                "tables (+ SELECT on the evidence/audit tables). No role can "
                "escalate.",
                KIND_CODE, ENFORCE_HYBRID,
                "scripts/qec_db_bootstrap.sql",
                operational_note="Role creation + password rotation are performed "
                "on the deployment (operational).",
            ),
            ControlSpec(
                "CC6.7", "Security",
                "Encryption of credentials at rest (KMS envelope, refuse-plaintext)",
                "Client credentials are envelope-encrypted (KMS KEK, AAD = app_id); "
                "when the envelope service is unavailable the write REFUSES (503) "
                "rather than store plaintext. A deployed process refuses to boot on "
                "a development KEK.",
                KIND_CODE, ENFORCE_HYBRID,
                "app/routers/apps.py (_encrypt_credentials) + app/security/"
                "boot_validator.py",
                operational_note="KMS key provisioning, rotation, and IAM binding "
                "are operational (cloud KMS).",
            ),
            ControlSpec(
                "CC6.8", "Security",
                "Prevention of unauthorized activity (onboarding attestation gate)",
                "A crawl/cycle against a real client app physically REFUSES until "
                "the app is onboarding-live: signed rules-of-engagement + a non-"
                "prod/disposable env attestation + a passed safety preflight. "
                "Fail-closed by default.",
                KIND_CODE, ENFORCE_CODE,
                "app/security/prod_guard.py + tests/unit/test_prod_guard.py",
            ),
            ControlSpec(
                "CC7.1", "Security",
                "System operations monitoring",
                "Prometheus /metrics exposition + a correlation-id on every "
                "request/response and log line; /health runs a live RLS-GUC round-"
                "trip and a KEK canary.",
                KIND_CODE, ENFORCE_HYBRID,
                "app/observability/ + app/main.py /health",
                operational_note="Prometheus scrape, dashboards, and alert routing "
                "are operational.",
            ),
            ControlSpec(
                "CC7.2", "Security",
                "Anomaly / tamper detection (hash-chained tamper-evidence)",
                "Every verdict is hash-chained per (tenant, artifact, test); a "
                "regulator (and this report) re-derives the chain and detects any "
                "rewrite. Detected breaks are surfaced, never hidden.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "verdict_events chain + adapter.verify_verdict_chains",
            ),
            ControlSpec(
                "CC7.4", "Security",
                "Security incident response",
                "Incident detection, triage, and forensics runbooks operate on the "
                "audit_log + hash chains as the evidentiary record.",
                KIND_OPERATIONAL, ENFORCE_OPERATIONAL,
                "QECentral/docs/THREAT_MODEL.md (security-review checklist)",
                operational_note="Requires the incident-response runbook + on-call "
                "process on the running deployment.",
            ),
            ControlSpec(
                "CC8.1", "Security",
                "Change management (reproducible decision records)",
                "Every ship/verdict writes an immutable, reproducible decision "
                "dossier: input hashes, rules applied, the risk model, and a "
                "template-rendered rationale — replaying the inputs reproduces the "
                "decision.",
                KIND_DOSSIERS, ENFORCE_CODE,
                "verdict_events.build_dossier / decision_dossiers",
            ),
            # ── Availability ────────────────────────────────────────────────
            ControlSpec(
                "A1.1", "Availability",
                "Capacity management (politeness-first admission + budgets)",
                "Global + per-tenant admission caps with a token bucket (in-memory "
                "or a fail-closed distributed redis limiter) bound customer-facing "
                "load; the append-only cost meter + budget gate bound spend.",
                KIND_CODE, ENFORCE_CODE,
                "app/controlplane/scheduling/admission.py + "
                "app/controlplane/cost/meter.py",
            ),
            ControlSpec(
                "A1.2", "Availability",
                "Backup & recovery (PITR + restore drill)",
                "Postgres point-in-time recovery covers both logical DBs from one "
                "backup setup; recovery is proven by a periodic restore drill.",
                KIND_OPERATIONAL, ENFORCE_OPERATIONAL,
                "infrastructure/ (Postgres PITR) + restore-drill runbook",
                operational_note="Backups + restore-drill execution are operational "
                "and must be evidenced from the running deployment.",
            ),
            # ── Confidentiality ─────────────────────────────────────────────
            ControlSpec(
                "C1.1", "Confidentiality",
                "PII redaction at source",
                "Every value persisted to the substrate is PII-redacted BEFORE "
                "persistence (ssn/card/password/email/phone/dob → [REDACTED:class]); "
                "no raw PII at rest. Detector failure is surfaced, never silent.",
                KIND_CODE, ENFORCE_CODE,
                "app/substrate/redact.py",
            ),
            ControlSpec(
                "C1.2", "Confidentiality",
                "Confidential data isolation + encryption",
                "Confidential tenant data is isolated by RLS and, for credentials, "
                "envelope-encrypted at rest; transport is TLS.",
                KIND_CODE, ENFORCE_HYBRID,
                "app/db/__init__.py (RLS) + app/routers/apps.py (envelope)",
                operational_note="TLS termination + at-rest disk encryption are "
                "operational (deployment/cloud).",
            ),
            # ── Processing Integrity ────────────────────────────────────────
            ControlSpec(
                "PI1.1", "Processing Integrity",
                "Input validation (refuse dishonest evidence)",
                "The evidence-bundle schema makes a dishonest crawl impossible to "
                "write: monotonic indices, pinned vocabularies, an after-outcome on "
                "every action — a broken rule raises an enumerated RefusalError.",
                KIND_CODE, ENFORCE_CODE,
                "app/substrate/schema.py (RefusalError)",
            ),
            ControlSpec(
                "PI1.2", "Processing Integrity",
                "Refuse-not-green-wash processing integrity (the crown jewel)",
                "The system REFUSES rather than fabricate a green result: a verdict "
                "is deterministic + min-gated (the LLM never scores), unproven steps "
                "are declared UNPROVEN, and each verdict is hash-chained so a green-"
                "wash cannot be inserted after the fact.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "app/harness/rules.py + verdict_events (hash chain)",
            ),
            ControlSpec(
                "PI1.3", "Processing Integrity",
                "Reproducible decision records",
                "Each decision writes a byte-reproducible dossier (input hashes, "
                "rules applied, risk model, rationale) chained to its verdict.",
                KIND_DOSSIERS, ENFORCE_CODE,
                "verdict_events.build_dossier / decision_dossiers",
            ),
            ControlSpec(
                "PI1.4", "Processing Integrity",
                "Governed exceptions (waivers are never silent)",
                "An exception to a finding is a WAIVER with a named owner, a reason, "
                "and an expiry; it annotates the verdict as WAIVED, never deletes "
                "the finding, and expires automatically.",
                KIND_WAIVERS, ENFORCE_CODE,
                "verdict_events.create_waiver / verification_waivers",
            ),
            ControlSpec(
                "PI1.5", "Processing Integrity",
                "Immutable audit trail",
                "Every tenant-scoped mutation writes an append-only audit_log entry "
                "(engine, action, entity, actor) — the trail an auditor replays.",
                KIND_AUDIT_LOG, ENFORCE_CODE,
                "audit_log (nexus_sdk.db.models.AuditLogRow)",
            ),
        ]


# ── NAIC Model Audit Rule (Model #205) — insurance ICFR analog ──────────────


class NAICModelAuditAdapter(ComplianceAdapter):
    """NAIC Model Audit Rule (Model #205) projection — internal-control-over-
    reporting mapping for regulated insurance buyers."""

    framework = "naic_mar"
    title = "NAIC Model Audit Rule (Model #205) — Internal Control Mapping"

    def control_specs(self) -> list[ControlSpec]:
        return [
            ControlSpec(
                "MAR-1", "Control Documentation",
                "Management documentation of automated controls",
                "Each automated decision is documented by a reproducible dossier "
                "(what was decided, from what inputs, under which rule version).",
                KIND_DOSSIERS, ENFORCE_CODE,
                "decision_dossiers (verdict_events.build_dossier)",
            ),
            ControlSpec(
                "MAR-2", "Access Controls",
                "Access controls + segregation of duties",
                "Tenant data is RLS-isolated; the owning DB role and the writer "
                "role are separated with least privilege — no single role reads and "
                "escalates across tenants.",
                KIND_CODE, ENFORCE_HYBRID,
                "app/db/__init__.py (RLS) + scripts/qec_db_bootstrap.sql",
                operational_note="Role/password administration is operational.",
            ),
            ControlSpec(
                "MAR-3", "Change Control",
                "Change control over automated processing",
                "Every change in the automated verdict is captured as a new hash-"
                "chained verdict event — the processing history is append-only and "
                "ordered.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "verdict_events (hash chain)",
            ),
            ControlSpec(
                "MAR-4", "Evidence Integrity",
                "Tamper-evident evidence retention",
                "The per-(tenant, artifact, test) hash chain lets a reviewer re-"
                "derive and prove no evidence row was rewritten.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "adapter.verify_verdict_chains",
            ),
            ControlSpec(
                "MAR-5", "Audit Trail",
                "Complete audit trail",
                "Every mutation is recorded in the append-only audit_log, tenant-"
                "scoped and immutable.",
                KIND_AUDIT_LOG, ENFORCE_CODE,
                "audit_log",
            ),
            ControlSpec(
                "MAR-6", "Exception Governance",
                "Documented, approved, time-bound exceptions",
                "A control exception is a governed waiver: named owner, reason, and "
                "automatic expiry; it is never a silent suppression.",
                KIND_WAIVERS, ENFORCE_CODE,
                "verification_waivers",
            ),
            ControlSpec(
                "MAR-7", "Confidentiality",
                "Policyholder data confidentiality",
                "Policyholder PII is redacted at source before persistence; no raw "
                "PII at rest.",
                KIND_CODE, ENFORCE_CODE,
                "app/substrate/redact.py",
            ),
            ControlSpec(
                "MAR-8", "Recovery",
                "Data recovery capability",
                "Point-in-time recovery of the evidence stores, proven by a restore "
                "drill.",
                KIND_OPERATIONAL, ENFORCE_OPERATIONAL,
                "infrastructure/ (Postgres PITR) + restore-drill runbook",
                operational_note="Backup + restore-drill execution are operational.",
            ),
        ]


# ── EU AI Act — technical-documentation / record-keeping profile ────────────


class EUAIActAnnex22Adapter(ComplianceAdapter):
    """EU AI-Act projection — Annex-IV technical documentation + the Article-12
    record-keeping / traceability obligations (the "Annex-22" evidence profile)."""

    framework = "eu_ai_act"
    title = "EU AI Act — Technical Documentation & Record-Keeping (Annex-22 profile)"

    def control_specs(self) -> list[ControlSpec]:
        return [
            ControlSpec(
                "AIA-Art12", "Record-Keeping",
                "Automatic logging of events over the system lifecycle (Art. 12)",
                "The system automatically records events (verdicts + audit_log) for "
                "the lifetime of each decision, enabling traceability.",
                KIND_AUDIT_LOG, ENFORCE_CODE,
                "audit_log + verdict_events",
            ),
            ControlSpec(
                "AIA-Art12b", "Record-Keeping",
                "Traceability via tamper-evident logs (Art. 12)",
                "Logs are hash-chained so their integrity is independently "
                "verifiable — traceability that cannot be silently rewritten.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "verdict_events (hash chain) + adapter.verify_verdict_chains",
            ),
            ControlSpec(
                "AIA-Art9", "Risk Management",
                "Risk management system (Art. 9)",
                "Each decision carries a deterministic, evidence-linked risk model "
                "(likelihood × blast-radius ÷ detectability) recorded in its dossier.",
                KIND_DOSSIERS, ENFORCE_CODE,
                "verdict_events.risk / decision_dossiers",
            ),
            ControlSpec(
                "AIA-Art10", "Data Governance",
                "Data governance & PII minimisation (Art. 10)",
                "Personal data is redacted at source and tenant-isolated by RLS — "
                "no raw PII enters the evidence substrate.",
                KIND_CODE, ENFORCE_CODE,
                "app/substrate/redact.py + app/db/__init__.py (RLS)",
            ),
            ControlSpec(
                "AIA-Art13", "Transparency",
                "Transparency & reproducibility to the deployer (Art. 13)",
                "Every decision is explained by a reproducible dossier: the inputs, "
                "the rules applied, and a template-rendered rationale (never free "
                "prose), so a deployer can reproduce and understand the output.",
                KIND_DOSSIERS, ENFORCE_CODE,
                "verdict_events.build_dossier / decision_dossiers",
            ),
            ControlSpec(
                "AIA-Art14", "Human Oversight",
                "Human oversight of exceptions (Art. 14)",
                "A human override is a governed waiver with a named owner, a reason, "
                "and an expiry — oversight is recorded, attributable, and time-bound.",
                KIND_WAIVERS, ENFORCE_CODE,
                "verification_waivers",
            ),
            ControlSpec(
                "AIA-Art15", "Accuracy & Robustness",
                "Accuracy, robustness & refuse-not-green-wash (Art. 15)",
                "The system declares UNPROVEN steps honestly and REFUSES to "
                "fabricate a passing result; the verdict is deterministic and min-"
                "gated, and the outcome is hash-chained.",
                KIND_VERDICT_CHAIN, ENFORCE_CODE,
                "app/harness/rules.py + verdict_events",
            ),
            ControlSpec(
                "AIA-AnnexIV", "Technical Documentation",
                "Technical documentation of the system (Annex IV)",
                "The registry version + reproducible dossiers constitute the "
                "technical record of how each decision was produced.",
                KIND_DOSSIERS, ENFORCE_HYBRID,
                "decision_dossiers + QECentral/docs/",
                operational_note="The full Annex-IV technical file (intended "
                "purpose, architecture narrative) is authored operationally around "
                "this evidence.",
            ),
        ]


# ══════════════════════════════════════════════════════════════════════════
#  Registry + lookup + report build
# ══════════════════════════════════════════════════════════════════════════


class UnknownFrameworkError(KeyError):
    """Raised for a compliance framework key that has no registered adapter.

    Carries the offending key + the known frameworks so the HTTP layer can map
    it to a 404 with an honest, actionable message.
    """

    def __init__(self, framework: str) -> None:
        self.framework = str(framework or "")
        self.known = sorted(_REGISTRY.keys())
        super().__init__(
            f"unknown compliance framework {self.framework!r}; "
            f"known frameworks: {self.known}"
        )


#: framework key → adapter class.  The single source of truth for what
#: /compliance/{framework}/report accepts.
_REGISTRY: dict[str, type[ComplianceAdapter]] = {
    SOC2Adapter.framework: SOC2Adapter,
    NAICModelAuditAdapter.framework: NAICModelAuditAdapter,
    EUAIActAnnex22Adapter.framework: EUAIActAnnex22Adapter,
}


def get_adapter(framework: str) -> ComplianceAdapter:
    """Return a fresh adapter for ``framework`` (case-insensitive).

    Raises :class:`UnknownFrameworkError` (→ HTTP 404) for an unknown key.
    """
    key = str(framework or "").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise UnknownFrameworkError(framework)
    return cls()


def available_frameworks() -> list[dict]:
    """A deterministic catalogue of registered frameworks (for a list endpoint)."""
    out = []
    for key in sorted(_REGISTRY.keys()):
        adapter = _REGISTRY[key]()
        out.append({
            "framework": adapter.framework,
            "title": adapter.title,
            "version": adapter.version,
            "control_count": adapter.control_count(),
        })
    return out


def build_report(framework: str, bundle: EvidenceBundle) -> dict:
    """Look up the adapter and project ``bundle`` (the pure endpoint core).

    Raises :class:`UnknownFrameworkError` (→ 404) for an unknown framework.
    """
    return get_adapter(framework).project(bundle)


__all__ = [
    "ADAPTER_VERSION",
    "CHAIN_REGISTRY_VERSION",
    # status + enforcement + kind vocabularies
    "STATUS_SATISFIED", "STATUS_PARTIAL", "STATUS_NOT_EVIDENCED", "STATUS_OPERATIONAL",
    "ENFORCE_CODE", "ENFORCE_OPERATIONAL", "ENFORCE_HYBRID",
    "KIND_VERDICT_CHAIN", "KIND_DOSSIERS", "KIND_AUDIT_LOG", "KIND_WAIVERS",
    "KIND_CODE", "KIND_OPERATIONAL",
    # chain re-derivation
    "canonical_verdict_payload", "compute_chain_hash", "verify_verdict_chains",
    # snapshot
    "EvidenceWindow", "EvidenceBundle", "ControlSpec",
    # digest
    "report_digest",
    # adapters
    "ComplianceAdapter", "SOC2Adapter", "NAICModelAuditAdapter", "EUAIActAnnex22Adapter",
    # registry
    "UnknownFrameworkError", "get_adapter", "available_frameworks", "build_report",
]
