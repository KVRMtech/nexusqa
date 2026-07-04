#!/usr/bin/env python3
"""Pages & Forms accuracy benchmark — the release measuring stick.

VIDEO-ONLY product: the client uploads a video; this benchmark measures how
accurately the pipeline turns those pixels into Pages & Forms. Run it on every
release: accuracy is MEASURED against human-verified answer keys, never
estimated. Never-green-wash applies to ourselves:

  * An answer key starts as key_status="draft" (seeded from the pipeline's own
    extraction — convenient but CIRCULAR). Draft keys are scored and shown, but
    the HEADLINE number counts VERIFIED keys only. A human reviews each draft
    against the actual video (visual-flow UI), corrects it, and flips
    key_status to "verified".
  * A regression gate compares the verified-key aggregate to baseline.json and
    exits non-zero on a drop > tolerance — wire that into any CI/release step.

Usage (inside the nexus-platform-api container, or any env with asyncpg + the
POSTGRES_* env vars):

  python run_benchmark.py --keys-dir keys/                 # score all keys
  python run_benchmark.py --seed-draft <artifact_id>       # write a draft key
  python run_benchmark.py --keys-dir keys/ --write-baseline # accept as baseline
  python run_benchmark.py --keys-dir keys/ --gate           # CI: fail on regression

Key file format (keys/<artifact8>.json):
{
  "artifact_id": "...", "key_status": "draft|verified",
  "video_class": "free text — e.g. 'low-res screen-share, insurance funnel'",
  "pages":  [{"host": "usaa.com", "path_prefix": "/insurance/life"}],
  "values": [{"label": "Weight (lbs)", "value": "185"}],
  "forms":  [{"label": "Coverage amount", "value": "$150,000"}],
  "notes": "anything a human reviewer should know"
}
Scoring: pages -> recall over expected pages (host equal + path prefix on any
visit); values -> found in page_actions (type/select value) OR form snapshot;
forms -> snapshot has the field (label match; value match when specified);
host_purity -> 1.0 when visits resolve to a single canonical host.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg


def _norm(v) -> str:
    return " ".join(str(v or "").split()).strip().strip(".").casefold()


def _dsn() -> str:
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgres')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ['POSTGRES_DB']}"
    )


async def _fetch(conn, artifact_id: str) -> dict:
    visits = await conn.fetch(
        """select sequence_index, location, coalesce(canonical_host,'') canonical_host,
                  coalesce(url_host,'') url_host, coalesce(url_path,'') url_path,
                  source, extraction_confidence, form_snapshot::text fs
           from page_visits where artifact_id=$1 order by sequence_index""",
        artifact_id)
    actions = await conn.fetch(
        """select verb, coalesce(target_label,'') label, coalesce(value,'') value,
                  automation_ready, confidence
           from page_actions where artifact_id=$1""",
        artifact_id)
    return {"visits": [dict(r) for r in visits], "actions": [dict(r) for r in actions]}


def _score(key: dict, data: dict) -> dict:
    visits, actions = data["visits"], data["actions"]
    snap_fields: dict = {}
    for v in visits:
        try:
            for k, val in (json.loads(v["fs"] or "{}") or {}).items():
                snap_fields[_norm(k)] = _norm(val)
        except (ValueError, TypeError):
            pass
    action_values = {_norm(a["value"]) for a in actions if _norm(a["value"])}
    hosts = {v["canonical_host"] for v in visits if v["canonical_host"]}

    # pages: recall over expected
    exp_pages = key.get("pages") or []
    page_hits = 0
    page_misses = []
    for p in exp_pages:
        h, pref = _norm(p.get("host")), (p.get("path_prefix") or "")
        ok = any(
            _norm(v["canonical_host"]) == h and (v["url_path"] or "").startswith(pref)
            for v in visits)
        page_hits += int(ok)
        if not ok:
            page_misses.append(f"{p.get('host')}{pref}")

    # values: found as an action value OR a snapshot value
    exp_vals = key.get("values") or []
    val_hits = 0
    val_misses = []
    for item in exp_vals:
        nv = _norm(item.get("value"))
        ok = bool(nv) and (nv in action_values or nv in snap_fields.values())
        val_hits += int(ok)
        if not ok:
            val_misses.append(f"{item.get('label')}={item.get('value')}")

    # forms: field present in a snapshot (value must match when given)
    exp_forms = key.get("forms") or []
    form_hits = 0
    form_misses = []
    for item in exp_forms:
        nl, nv = _norm(item.get("label")), _norm(item.get("value"))
        present = nl in snap_fields
        ok = present and (not nv or snap_fields.get(nl) == nv)
        form_hits += int(ok)
        if not ok:
            form_misses.append(f"{item.get('label')}={item.get('value')}")

    def pct(hit, total):
        return round(hit / total, 3) if total else None

    return {
        "artifact_id": key["artifact_id"],
        "key_status": key.get("key_status", "draft"),
        "video_class": key.get("video_class", ""),
        "page_identity": pct(page_hits, len(exp_pages)),
        "value_capture": pct(val_hits, len(exp_vals)),
        "form_coverage": pct(form_hits, len(exp_forms)),
        "host_purity": 1.0 if len(hosts) <= 1 else round(1.0 / len(hosts), 3),
        "visits": len(visits),
        "misses": {"pages": page_misses, "values": val_misses, "forms": form_misses},
    }


def _aggregate(rows: list, status: str) -> dict:
    sel = [r for r in rows if r["key_status"] == status] if status != "all" else rows
    if not sel:
        return {"count": 0}
    dims = ("page_identity", "value_capture", "form_coverage", "host_purity")
    agg = {"count": len(sel)}
    for d in dims:
        vals = [r[d] for r in sel if r[d] is not None]
        agg[d] = round(sum(vals) / len(vals), 3) if vals else None
    scored = [v for v in (agg.get(d) for d in dims) if v is not None]
    agg["overall"] = round(sum(scored) / len(scored), 3) if scored else None
    return agg


async def _seed_draft(conn, artifact_id: str, keys_dir: Path) -> Path:
    """Write a DRAFT key from the pipeline's own current extraction. CIRCULAR
    by construction — a human MUST review it against the video and flip
    key_status to 'verified' before it counts toward the headline number."""
    data = await _fetch(conn, artifact_id)
    pages, seen = [], set()
    for v in data["visits"]:
        if v["source"] in ("url_regex", "url_scene", "llm_inferred", "ground_truth") \
                and v["canonical_host"]:
            seg = "/".join((v["url_path"] or "").split("/")[:3])
            sig = (v["canonical_host"], seg)
            if sig not in seen:
                seen.add(sig)
                pages.append({"host": v["canonical_host"], "path_prefix": seg})
    values = [
        {"label": a["label"], "value": a["value"]}
        for a in data["actions"]
        if _norm(a["value"]) and len(_norm(a["value"])) >= 2
        and str(a["verb"]).lower() in ("type", "select")
    ]
    forms = []
    for v in data["visits"]:
        try:
            for k, val in (json.loads(v["fs"] or "{}") or {}).items():
                if _norm(val) and _norm(val) not in ("true", "false"):
                    forms.append({"label": k, "value": val})
        except (ValueError, TypeError):
            pass
    key = {
        "artifact_id": artifact_id,
        "key_status": "draft",
        "video_class": "TODO: describe the recording (human)",
        "pages": pages,
        "values": values,
        "forms": forms,
        "notes": ("DRAFT seeded from the pipeline's own extraction — CIRCULAR until a "
                  "human verifies every entry against the actual video and sets "
                  "key_status='verified'. Add anything the pipeline MISSED."),
    }
    out = keys_dir / f"{artifact_id[:8]}.json"
    out.write_text(json.dumps(key, indent=2), encoding="utf-8")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys-dir", default="keys")
    ap.add_argument("--seed-draft", default="")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.02)
    args = ap.parse_args()

    keys_dir = Path(args.keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(_dsn())
    try:
        if args.seed_draft:
            out = await _seed_draft(conn, args.seed_draft, keys_dir)
            print(f"draft key written: {out} — HUMAN VERIFICATION REQUIRED")
            return 0

        rows = []
        for kf in sorted(keys_dir.glob("*.json")):
            key = json.loads(kf.read_text(encoding="utf-8"))
            data = await _fetch(conn, key["artifact_id"])
            if not data["visits"]:
                print(f"  !! {kf.name}: no visits in DB (not derived?) — skipped")
                continue
            rows.append(_score(key, data))
    finally:
        await conn.close()

    print(f"{'artifact':<10} {'status':<9} {'pages':<7} {'values':<7} {'forms':<7} {'purity':<7} class")
    for r in rows:
        print(f"{r['artifact_id'][:8]:<10} {r['key_status']:<9} "
              f"{str(r['page_identity']):<7} {str(r['value_capture']):<7} "
              f"{str(r['form_coverage']):<7} {str(r['host_purity']):<7} {r['video_class'][:40]}")
        for cat, misses in r["misses"].items():
            for m in misses[:4]:
                print(f"    miss[{cat}]: {m}")

    verified = _aggregate(rows, "verified")
    draft = _aggregate(rows, "draft")
    print(f"\nVERIFIED keys ({verified.get('count', 0)}): overall={verified.get('overall')}  {verified}")
    print(f"DRAFT keys    ({draft.get('count', 0)}): overall={draft.get('overall')}  "
          f"(informational only — circular until human-verified)")

    baseline_path = Path("baseline.json")
    if args.write_baseline:
        baseline_path.write_text(json.dumps(
            {"verified": verified, "draft": draft}, indent=2), encoding="utf-8")
        print(f"baseline written: {baseline_path}")

    if args.gate:
        if not baseline_path.exists():
            print("GATE: no baseline.json — run --write-baseline first")
            return 2
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
        ref = (base.get("verified") or {})
        cur = verified if verified.get("count") else draft
        refv = ref.get("overall") if ref.get("count") else \
            (base.get("draft") or {}).get("overall")
        if refv is None or cur.get("overall") is None:
            print("GATE: nothing comparable — need at least one scored key")
            return 2
        if cur["overall"] < refv - args.tolerance:
            print(f"GATE FAIL: overall {cur['overall']} < baseline {refv} - {args.tolerance}")
            return 1
        print(f"GATE PASS: overall {cur['overall']} vs baseline {refv}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
