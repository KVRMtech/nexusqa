"""Static lint for the VKPower Verdict Helm chart.

Why this exists: `helm` is not always on a CI runner or a contributor
laptop, and the chart's real bugs (typo'd .Values paths, missing helper
includes, a secretKeyRef.key that no Secret emits) only surface at render
time. This mirrors infrastructure/helm/nexus-qa/scripts/chart_lint.py and
catches the same classes of bug by walking the templates as text + the
values.yaml as a Python dict. It is complementary to `helm lint`, which
CI runs when helm is available (see README note at the bottom of this file).

Checks:
  1. Every `.Values.X.Y` reference resolves to a key in values.yaml (or an
     explicitly-merged overlay), with range/with scope tracking.
  2. Every `include "name"` names a `define "name"` in a .tpl/.yaml template.
  3. Every `required "msg" .Values.X` points at a present or overlay key.
  4. Every `secretKeyRef.key` is emitted by secret.yaml / external-secret.yaml.
  5. Every Deployment/Job/StatefulSet `image:` uses the verdict.image helper
     or a values-driven third-party image reference.

Usage:
    python infrastructure/helm/verdict/scripts/chart_lint.py
    python ... --values values-onprem.yaml   # also merge an overlay
    python ... --strict                       # WARNING -> non-zero exit
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = CHART_DIR / "templates"
VALUES_FILE = CHART_DIR / "values.yaml"


# ─── YAML helpers ───────────────────────────────────────────────


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


# ─── Issue tracking ─────────────────────────────────────────────


class Issue:
    __slots__ = ("file", "line", "level", "msg")

    def __init__(self, file: str, line: int, level: str, msg: str):
        self.file = file
        self.line = line
        self.level = level
        self.msg = msg

    def render(self) -> str:
        return f"{self.level:7s} {self.file}:{self.line}: {self.msg}"


# ─── Regex bank ─────────────────────────────────────────────────

_RE_VALUES = re.compile(r"\.Values\.([A-Za-z_][A-Za-z0-9_.]*)")
_RE_INCLUDE = re.compile(r'include\s+"([^"]+)"')
_RE_DEFINE = re.compile(r'define\s+"([^"]+)"')
_RE_REQUIRED = re.compile(r'required\s+"[^"]*"\s+\.Values\.([A-Za-z_][A-Za-z0-9_.]*)')
_RE_SCOPE_OPEN_RANGE = re.compile(
    r"\{\{-?\s*range\s+(?:\$\w+\s*,\s*)?\$?\w*\s*:=\s*\.Values\.([A-Za-z_][A-Za-z0-9_.]*)"
)
_RE_SCOPE_OPEN_WITH = re.compile(r"\{\{-?\s*with\s+\.Values\.([A-Za-z_][A-Za-z0-9_.]*)")
_RE_SECRET_DATA_KEY = re.compile(r"^\s{2}([a-zA-Z0-9._-]+):\s*\{\{")
_RE_SECRET_REF_KEY = re.compile(r"key:\s+([a-zA-Z0-9._-]+)")
_RE_IMAGE_LINE = re.compile(r"^\s*image:\s*(.*)$")
_RE_COMMENT_BLOCK = re.compile(r"\{\{/\*.*?\*/\}\}", re.DOTALL)


# ─── Reference resolution ───────────────────────────────────────


def values_key_exists(key_path: str, values: dict) -> bool:
    cur = values
    for part in key_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            continue
        return False
    return True


# `.Values.X` paths that are optional / overlay- or scope-supplied.
_OVERLAY_OK_PREFIXES = (
    "global.imagePullSecrets",
    "podAnnotations",
    "podSecurityContext",
    "containerSecurityContext",
    "priorityClassName",
    "ingress.annotations",
    "monitoring.serviceMonitor.labels",
    "monitoring.serviceMonitor.metricRelabelings",
    "serviceAccount.annotations",
    "egressProxy.excludeCidrs",
    "kek.persistence.secret",
)


def is_overlay_ok(path: str) -> bool:
    return any(path.startswith(p) for p in _OVERLAY_OK_PREFIXES)


def _strip_helm_comments(text: str) -> str:
    def _blank(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")

    return _RE_COMMENT_BLOCK.sub(_blank, text)


def collect_defined_helpers() -> set[str]:
    defs: set[str] = set()
    for tpl in TEMPLATES_DIR.rglob("*.tpl"):
        for m in _RE_DEFINE.finditer(tpl.read_text(encoding="utf-8")):
            defs.add(m.group(1))
    return defs


def collect_secret_data_keys() -> set[str]:
    out: set[str] = set()
    sf = TEMPLATES_DIR / "secret.yaml"
    if sf.is_file():
        for line in sf.read_text(encoding="utf-8").splitlines():
            m = _RE_SECRET_DATA_KEY.match(line)
            if m:
                out.add(m.group(1))
    # external-secret.yaml builds its key list from a `list "…"` literal —
    # extract the quoted key names so ESO-managed keys count as emitted.
    es = TEMPLATES_DIR / "external-secret.yaml"
    if es.is_file():
        text = es.read_text(encoding="utf-8")
        for m in re.finditer(r'"([a-z0-9-]+)"', text):
            token = m.group(1)
            if "-" in token or token in {"jwt", "explorer"}:
                out.add(token)
    return out


def lint_template(
    path: Path,
    values: dict,
    helpers_defined: set[str],
    secret_keys: set[str],
    issues: list[Issue],
) -> None:
    raw = path.read_text(encoding="utf-8")
    text = _strip_helm_comments(raw)
    rel = str(path.relative_to(CHART_DIR)).replace("\\", "/")
    in_scope: list[str] = []

    for i, line in enumerate(text.splitlines(), start=1):
        m_scope = _RE_SCOPE_OPEN_RANGE.search(line) or _RE_SCOPE_OPEN_WITH.search(line)
        if m_scope:
            in_scope.append(m_scope.group(1))
        if re.search(r"\{\{-?\s*end\s*-?\}\}", line) and in_scope:
            in_scope.pop()

        for m in _RE_VALUES.finditer(line):
            kp = m.group(1).rstrip(".")
            if is_overlay_ok(kp):
                continue
            window = line[max(0, m.start() - 30):m.start()]
            if "dig " in window:
                continue
            if not values_key_exists(kp, values):
                issues.append(Issue(rel, i, "ERROR", f".Values.{kp} not found in values.yaml"))

    for i, line in enumerate(text.splitlines(), start=1):
        for m in _RE_INCLUDE.finditer(line):
            if m.group(1) not in helpers_defined:
                issues.append(Issue(rel, i, "ERROR", f'include "{m.group(1)}" — helper not defined'))

    for i, line in enumerate(text.splitlines(), start=1):
        for m in _RE_REQUIRED.finditer(line):
            kp = m.group(1).rstrip(".")
            if not values_key_exists(kp, values):
                issues.append(Issue(rel, i, "INFO", f"required-guarded .Values.{kp} not present (operator must --set)"))

    if "secretKeyRef" in text:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "secretKeyRef" not in line:
                continue
            for j in range(i, min(i + 6, len(lines))):
                m = _RE_SECRET_REF_KEY.search(lines[j])
                if not m:
                    continue
                key = m.group(1)
                if key not in secret_keys:
                    issues.append(Issue(rel, j + 1, "ERROR", f"secretKeyRef.key '{key}' not emitted by secret.yaml/external-secret.yaml"))
                break

    for i, line in enumerate(text.splitlines(), start=1):
        m = _RE_IMAGE_LINE.match(line)
        if not m:
            continue
        val = m.group(1).strip()
        if not val:
            continue
        if "verdict.image" in val:
            continue
        if val.startswith("{{") and ".Values." in val:
            continue
        issues.append(Issue(rel, i, "WARNING", f"image: '{val[:60]}' bypasses the verdict.image helper"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--values", default="", help="an overlay values file (relative to the chart dir) to merge")
    ap.add_argument("--strict", action="store_true", help="promote WARNING -> non-zero exit")
    args = ap.parse_args()

    if not VALUES_FILE.is_file():
        print(f"values.yaml not found: {VALUES_FILE}", file=sys.stderr)
        return 2

    values = load_yaml(VALUES_FILE)
    if args.values:
        overlay = load_yaml(CHART_DIR / args.values)
        values = deep_merge(values, overlay)

    helpers_defined = collect_defined_helpers()
    secret_keys = collect_secret_data_keys()
    print(
        f"chart_lint: chart={CHART_DIR.name} overlay={args.values or '<base>'} "
        f"helpers={len(helpers_defined)} secrets={sorted(secret_keys)}"
    )

    issues: list[Issue] = []
    for tpl in sorted(TEMPLATES_DIR.iterdir()):
        if tpl.suffix not in {".yaml", ".tpl"} or tpl.name in {"NOTES.txt", "_helpers.tpl"}:
            continue
        lint_template(tpl, values, helpers_defined, secret_keys, issues)

    errors = [i for i in issues if i.level == "ERROR"]
    warnings = [i for i in issues if i.level == "WARNING"]
    infos = [i for i in issues if i.level == "INFO"]
    for it in issues:
        print(it.render())
    print(f"\nchart_lint: {len(errors)} errors, {len(warnings)} warnings, {len(infos)} info")

    if errors:
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
