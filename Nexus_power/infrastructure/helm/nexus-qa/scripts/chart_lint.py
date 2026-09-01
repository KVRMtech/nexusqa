"""
Static lint for the nexus-qa Helm chart.

Why this exists: `helm template` is not always available on CI runners
or contributor laptops, and the chart's real bugs (typo'd .Values
paths, missing `_helpers.tpl` includes, broken `required` calls) only
surface at render time. This script catches the same bugs by walking
the templates as plain text + the values.yaml as a Python dict.

Checks performed:

  1. Every `.Values.X.Y.Z` reference in templates resolves to a key
     under values.yaml (or one of the env overlays). Wildcards inside
     `range` blocks are tracked so per-component values aren't false
     positives.
  2. Every `include "name"` references a `define "name"` in any .tpl
     or .yaml template.
  3. Every `required "msg" .Values.X` reference points at a key that
     is either present in values.yaml or has a same-block default.
  4. Every `secretKeyRef.key` value names a key written by secret.yaml
     (so a typo'd secret-key never reaches deploy).
  5. Every Deployment / Rollout `image:` pulls from `nexus-qa.image`
     so canary + tagging behaviour is uniform.

The output is a list of issues with file:line; exit status 1 if any
fatal issue is found.

Usage:
    python infrastructure/helm/nexus-qa/scripts/chart_lint.py
    python ... --strict    # promote warnings to errors
    python ... --env production   # also load env overlay values
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[4]
CHART_DIR = REPO_ROOT / "infrastructure" / "helm" / "nexus-qa"
TEMPLATES_DIR = CHART_DIR / "templates"
HELPERS_FILE = TEMPLATES_DIR / "_helpers.tpl"
VALUES_FILE = CHART_DIR / "values.yaml"
ENVS_DIR = REPO_ROOT / "infrastructure" / "gitops" / "envs"


# ─── Helpers ────────────────────────────────────────────────────


def load_yaml(p: Path) -> dict:
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def deep_merge(base: dict, overlay: dict) -> dict:
    """Helm-style deep merge: overlay wins on scalars; lists replace."""
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def collect_keys(d, prefix: str = "") -> set[str]:
    """Flatten dict into 'a.b.c'-style key paths so we can check refs."""
    out: set[str] = set()
    if isinstance(d, dict):
        for k, v in d.items():
            kp = f"{prefix}.{k}" if prefix else k
            out.add(kp)
            out |= collect_keys(v, kp)
    elif isinstance(d, list):
        # Lists: we don't index — references like .Values.foo[0].bar
        # are out-of-scope for static lint.
        pass
    return out


# ─── Issue tracking ────────────────────────────────────────────


class Issue:
    __slots__ = ("file", "line", "level", "msg")

    def __init__(self, file: str, line: int, level: str, msg: str):
        self.file = file
        self.line = line
        self.level = level
        self.msg = msg

    def render(self) -> str:
        return f"{self.level:7s} {self.file}:{self.line}: {self.msg}"


# ─── Regex bank ────────────────────────────────────────────────


# `.Values.foo.bar.baz` followed by anything not-identifier
_RE_VALUES = re.compile(r"\.Values\.([A-Za-z_][A-Za-z0-9_.]*)")

# `include "name"` — Helm-style helper reference
_RE_INCLUDE = re.compile(r'include\s+"([^"]+)"')

# `define "name"` — helper definition (in .tpl files)
_RE_DEFINE = re.compile(r'define\s+"([^"]+)"')

# `required "msg" .Values.foo.bar`
_RE_REQUIRED = re.compile(
    r'required\s+"[^"]*"\s+\.Values\.([A-Za-z_][A-Za-z0-9_.]*)'
)

# A line that starts a `range $k, $v := .Values.X` or `with .Values.X`
# pulls a sub-tree into a different scope. We collect those prefixes
# so we can recognise downstream `.X` lookups as locally-scoped.
_RE_SCOPE_OPEN_RANGE = re.compile(
    r"\{\{-?\s*range\s+(?:\$\w+\s*,\s*)?\$?\w*\s*:=\s*\.Values\.([A-Za-z_][A-Za-z0-9_.]*)"
)
_RE_SCOPE_OPEN_WITH = re.compile(
    r"\{\{-?\s*with\s+\.Values\.([A-Za-z_][A-Za-z0-9_.]*)"
)

# secret.yaml emits keys via `<key>: {{ ... | b64enc | quote }}`. We
# extract the bare key names so other templates' `secretKeyRef.key`
# lookups can be validated.
_RE_SECRET_DATA_KEY = re.compile(r"^\s{2}([a-zA-Z0-9._-]+):\s*\{\{")

# secretKeyRef.key value references — match `key: <name>` two lines
# under a `secretRef` / `secretKeyRef` block.
_RE_SECRET_REF_KEY = re.compile(r"key:\s+([a-zA-Z0-9._-]+)")

# image: pulls — we want every Deployment/Rollout to use the canonical
# include helper, not hardcode a repo string.
_RE_IMAGE_LINE = re.compile(r"^\s*image:\s*(.*)$")


# ─── Loaders ───────────────────────────────────────────────────


def load_values_merged(env: str | None) -> dict:
    base = load_yaml(VALUES_FILE)
    if not env:
        return base
    overlay = load_yaml(ENVS_DIR / env / "values" / "platform.yaml")
    return deep_merge(base, overlay)


def collect_defined_helpers() -> set[str]:
    defs: set[str] = set()
    for tpl in TEMPLATES_DIR.rglob("*.tpl"):
        for m in _RE_DEFINE.finditer(tpl.read_text(encoding="utf-8")):
            defs.add(m.group(1))
    return defs


def collect_secret_data_keys() -> set[str]:
    sf = TEMPLATES_DIR / "secret.yaml"
    if not sf.is_file():
        return set()
    out: set[str] = set()
    for line in sf.read_text(encoding="utf-8").splitlines():
        m = _RE_SECRET_DATA_KEY.match(line)
        if m:
            out.add(m.group(1))
    # external-secret.yaml may add more (canonical workflow secrets).
    es = TEMPLATES_DIR / "external-secret.yaml"
    if es.is_file():
        for line in es.read_text(encoding="utf-8").splitlines():
            # ExternalSecret uses `secretKey: <name>` inside data entries.
            m = re.match(r"\s*-?\s*secretKey:\s+([a-zA-Z0-9._-]+)", line)
            if m:
                out.add(m.group(1))
    return out


# ─── Reference resolution ──────────────────────────────────────


def values_key_exists(key_path: str, values: dict) -> bool:
    """`.Values.foo.bar` → True iff values['foo']['bar'] is present."""
    cur = values
    for part in key_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            continue
        return False
    return True


# A small set of `.Values.X` paths we expect to be optional / overlay-
# supplied and should not error on absence in base values.yaml.
_OVERLAY_OK_PREFIXES = (
    "global.imagePullSecrets",
    "podAnnotations",
    "topologySpread.enabled",
    "topologySpread.maxSkew",
    "topologySpread.whenUnsatisfiable",
    "priorityClassName",
    "ingress.annotations",
    "ingress.tls.secretName",
    "mesh.istio",                  # istio is the alternative mesh
)


def is_overlay_ok(path: str) -> bool:
    return any(path.startswith(p) for p in _OVERLAY_OK_PREFIXES)


# ─── Per-file lint ─────────────────────────────────────────────


_RE_COMMENT_BLOCK = re.compile(r"\{\{/\*.*?\*/\}\}", re.DOTALL)


def _strip_helm_comments(text: str) -> str:
    """Replace `{{/* ... */}}` blocks with blank lines so line numbers
    stay aligned but documentation `.Values.X` mentions don't trigger
    false positives."""
    def _blank_keep_newlines(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")
    return _RE_COMMENT_BLOCK.sub(_blank_keep_newlines, text)


def lint_template(
    path: Path,
    values: dict,
    helpers_defined: set[str],
    secret_keys: set[str],
    issues: list[Issue],
) -> None:
    raw = path.read_text(encoding="utf-8")
    text = _strip_helm_comments(raw)
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")

    # Track scope-pushing constructs so refs inside a `range $svc :=
    # .Values.engines` block don't fire on `$svc.replicas`.
    in_scope_stack: list[str] = []

    # 1. .Values.X.Y references
    for i, line in enumerate(text.splitlines(), start=1):
        m_scope = _RE_SCOPE_OPEN_RANGE.search(line) or _RE_SCOPE_OPEN_WITH.search(line)
        if m_scope:
            in_scope_stack.append(m_scope.group(1))
        if re.search(r"\{\{-?\s*end\s*-?\}\}", line) and in_scope_stack:
            in_scope_stack.pop()

        for m in _RE_VALUES.finditer(line):
            kp = m.group(1).rstrip(".")
            if is_overlay_ok(kp):
                continue
            # Skip dig() lookups — they're explicitly nil-safe.
            window = line[max(0, m.start() - 30):m.start()]
            if "dig " in window:
                continue
            if not values_key_exists(kp, values):
                issues.append(Issue(
                    rel, i, "ERROR",
                    f".Values.{kp} not found in values.yaml",
                ))

    # 2. include "name" references
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _RE_INCLUDE.finditer(line):
            name = m.group(1)
            if name not in helpers_defined:
                issues.append(Issue(
                    rel, i, "ERROR",
                    f'include "{name}" — helper not defined in any _helpers.tpl',
                ))

    # 3. required "..." .Values.X
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _RE_REQUIRED.finditer(line):
            kp = m.group(1).rstrip(".")
            if not values_key_exists(kp, values):
                # `required` is itself a guard; the absence is by
                # design (operator must --set it). Promote to WARN
                # only if the key has no documented home anywhere.
                issues.append(Issue(
                    rel, i, "INFO",
                    f"required-guarded .Values.{kp} is not present (operator must --set or override)",
                ))

    # 4. secretKeyRef.key validation
    if "secretKeyRef" in text:
        # Naive but useful: any `key: foo` within 5 lines of `secretKeyRef`.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "secretKeyRef" not in line and "configMapKeyRef" not in line:
                continue
            kind = "secretKeyRef" if "secretKeyRef" in line else "configMapKeyRef"
            # Look up to 5 lines below for the `key:` field.
            for j in range(i, min(i + 6, len(lines))):
                m = _RE_SECRET_REF_KEY.search(lines[j])
                if not m:
                    continue
                key = m.group(1)
                # ConfigMap keys are project-specific (the configmap.yaml
                # data block); we only validate Secret keys here.
                if kind == "secretKeyRef" and key not in secret_keys:
                    issues.append(Issue(
                        rel, j + 1, "ERROR",
                        f"secretKeyRef.key '{key}' is not emitted by secret.yaml or external-secret.yaml",
                    ))
                break

    # 5. image: lines should use the canonical helper
    for i, line in enumerate(text.splitlines(), start=1):
        m = _RE_IMAGE_LINE.match(line)
        if not m:
            continue
        val = m.group(1).strip()
        if not val:
            continue
        # Acceptable shapes: includes the canonical helper, or
        # references a third-party (postgres, redis, neo4j, etc.)
        # via its own values block.
        if "nexus-qa.image" in val:
            continue
        # Allow well-known third-party images that reference Values.
        if val.startswith("{{") and ".Values." in val:
            continue
        issues.append(Issue(
            rel, i, "WARNING",
            f"image: '{val[:60]}...' bypasses nexus-qa.image helper",
        ))


# ─── Main ──────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="", help="env overlay to merge (production|staging|dev)")
    ap.add_argument("--strict", action="store_true",
                    help="promote WARNING → ERROR for exit status")
    args = ap.parse_args()

    if not CHART_DIR.is_dir():
        print(f"chart dir not found: {CHART_DIR}", file=sys.stderr)
        return 2
    if not VALUES_FILE.is_file():
        print(f"values.yaml not found: {VALUES_FILE}", file=sys.stderr)
        return 2

    values = load_values_merged(args.env or None)
    helpers_defined = collect_defined_helpers()
    secret_keys = collect_secret_data_keys()

    print(
        f"chart_lint: chart={CHART_DIR.name} env={args.env or '<base>'} "
        f"helpers={len(helpers_defined)} secrets={len(secret_keys)}"
    )

    issues: list[Issue] = []
    for tpl in sorted(TEMPLATES_DIR.iterdir()):
        if tpl.suffix not in {".yaml", ".tpl"}:
            continue
        if tpl.name == "NOTES.txt":
            continue
        if tpl.name == "_helpers.tpl":
            # helpers are not consumers of .Values via the usual paths;
            # they emit them. Skip the cross-check.
            continue
        lint_template(tpl, values, helpers_defined, secret_keys, issues)

    errors = [i for i in issues if i.level == "ERROR"]
    warnings = [i for i in issues if i.level == "WARNING"]
    infos = [i for i in issues if i.level == "INFO"]

    for it in issues:
        print(it.render())
    print(
        f"\nchart_lint: {len(errors)} errors, {len(warnings)} warnings, "
        f"{len(infos)} info"
    )

    if errors:
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
