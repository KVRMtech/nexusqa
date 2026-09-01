"""Stack detection + PUBLISHED ceiling bands (design §3.3, item 4).

:func:`detect` fingerprints a cloned repository from its MANIFEST FILES and
vendor markers and returns a :class:`StackFingerprint`: which stacks were
found (with file:marker evidence), which static extractors apply, and each
extractor's PUBLISHED static-rule accuracy ceiling band.

Honesty doctrine (design §3.3, §6 Phase-2 exit):
  * Ceiling bands are PUBLISHED methodology constants, not tunable config —
    a rule extractor is graded in CI against the band FLOOR and may never
    claim recall above the ceiling.  They live here as data, exactly like
    the deterministic ``_TRIAGE_RULES`` marker table.
  * JS/TS (react-router/next/vue/angular, express/nest) and Spring have
    HIGH bands (structured routing is statically legible).
  * Opaque low-code / vendor platforms (Guidewire, Pega, Salesforce) have
    a hard ~5-15% ceiling and are labelled ``route_to_crawl_and_human`` —
    static extraction is advisory at best; the crawler + a human own them.
  * A detected stack with no static extractor yet built (rails, dotnet) is
    likewise routed to crawl+human — NEVER silently dropped.

The detector is deterministic and bounded: it scans a limited number of
files at a limited depth (skipping ``.git``/``node_modules``/build dirs) so
it stays fast on huge monorepos and reports ``scan_truncated`` honestly.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("repo_intel.detect")

# Extractor names (must match the plugin ``name`` attributes the later
# stages register into ``app.extract.registry``).
EXTRACTOR_OPENAPI = "openapi_spec"
EXTRACTOR_TS_ROUTES = "ts_routes"
EXTRACTOR_EXPRESS_NEST = "express_nest"
EXTRACTOR_SPRING = "spring"

# Routing labels for the ceiling-band ``label`` field.
LABEL_HIGH = "high"
LABEL_ROUTE_TO_CRAWL_HUMAN = "route_to_crawl_and_human"

# Bounded-scan limits (env-overridable operational caps, not methodology).
_MAX_SCAN_DEPTH = int(os.environ.get("REPO_INTEL_DETECT_MAX_DEPTH", "5"))
_MAX_SCAN_FILES = int(os.environ.get("REPO_INTEL_DETECT_MAX_FILES", "20000"))
_PRUNE_DIRS = frozenset({
    ".git", "node_modules", "vendor", "dist", "build", "target",
    "out", ".gradle", ".idea", ".venv", "venv", "__pycache__",
    "coverage", ".next", ".nuxt", ".svelte-kit",
})


# ── Ceiling band ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CeilingBand:
    """Published static-rule accuracy ceiling for one extractor/stack.

    ``recall_floor``/``recall_ceiling`` bound the fraction of ground-truth
    atoms a rule extractor is expected to recover; CI FAILS a plugin whose
    measured recall drops below ``recall_floor`` or whose precision drops
    below ``precision_floor`` (design §6 Phase-2 exit).  These are PUBLISHED
    numbers — never silently raised to make a build pass.
    """

    extractor: str
    recall_floor: float
    recall_ceiling: float
    precision_floor: float
    label: str

    def to_dict(self) -> dict:
        return asdict(self)


# PUBLISHED CEILING BANDS (methodology constants — see module docstring).
# precision_floor is 0.90 everywhere: the product NEVER emits a static
# fact it cannot precisely ground, regardless of stack.
PLUGIN_CEILINGS: Dict[str, CeilingBand] = {
    # OpenAPI/Swagger specs are a declarative contract — near-total recall.
    EXTRACTOR_OPENAPI: CeilingBand(EXTRACTOR_OPENAPI, 0.90, 0.98, 0.90, LABEL_HIGH),
    # JS/TS routing + server endpoints — statically legible, high band.
    EXTRACTOR_TS_ROUTES: CeilingBand(EXTRACTOR_TS_ROUTES, 0.70, 0.88, 0.90, LABEL_HIGH),
    EXTRACTOR_EXPRESS_NEST: CeilingBand(EXTRACTOR_EXPRESS_NEST, 0.70, 0.88, 0.90, LABEL_HIGH),
    # Spring MVC/WebFlux annotations — high band.
    EXTRACTOR_SPRING: CeilingBand(EXTRACTOR_SPRING, 0.65, 0.85, 0.90, LABEL_HIGH),
}

# Opaque vendor / low-code platforms: hard low ceiling + human routing.
# Keyed by STACK (no static extractor is registered for these in v1).
VENDOR_CEILINGS: Dict[str, CeilingBand] = {
    "guidewire": CeilingBand("guidewire", 0.05, 0.15, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN),
    "pega": CeilingBand("pega", 0.05, 0.15, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN),
    "salesforce": CeilingBand("salesforce", 0.05, 0.15, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN),
    # Detected but no static extractor built yet — advisory, human-routed.
    "rails": CeilingBand("rails", 0.0, 0.0, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN),
    "dotnet": CeilingBand("dotnet", 0.0, 0.0, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN),
}


# ── Detected stack ───────────────────────────────────────────────────────
@dataclass
class DetectedStack:
    """One stack fingerprinted in the repo, with file:marker evidence."""

    stack: str
    # The static extractor that applies, or None ⇒ route to crawl + human.
    static_extractor: Optional[str]
    evidence: List[str] = field(default_factory=list)
    routing: str = "static_extraction"  # or "route_to_crawl_and_human"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StackFingerprint:
    """Result of :func:`detect` — the whole fingerprint for one universe."""

    stacks: List[DetectedStack] = field(default_factory=list)
    # Static extractors that apply, sorted + de-duplicated.
    extractors: List[str] = field(default_factory=list)
    # Ceiling bands keyed by extractor (applicable) AND by stack (human-routed).
    ceiling_bands: Dict[str, dict] = field(default_factory=dict)
    # Stacks with no static extractor — the crawler + a human own them.
    routed_to_crawl_human: List[str] = field(default_factory=list)
    scanned_files: int = 0
    scan_truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "stacks": [s.to_dict() for s in self.stacks],
            "extractors": list(self.extractors),
            "ceiling_bands": dict(self.ceiling_bands),
            "routed_to_crawl_human": list(self.routed_to_crawl_human),
            "scanned_files": self.scanned_files,
            "scan_truncated": self.scan_truncated,
        }


# ── package.json dependency → stack mapping ──────────────────────────────
# Each entry: dep-name matcher (exact) → (stack, extractor).
_JS_DEP_STACKS: List[tuple] = [
    ("react-router", "react-router", EXTRACTOR_TS_ROUTES),
    ("react-router-dom", "react-router", EXTRACTOR_TS_ROUTES),
    ("next", "next", EXTRACTOR_TS_ROUTES),
    ("vue-router", "vue", EXTRACTOR_TS_ROUTES),
    ("vue", "vue", EXTRACTOR_TS_ROUTES),
    ("@angular/router", "angular", EXTRACTOR_TS_ROUTES),
    ("@angular/core", "angular", EXTRACTOR_TS_ROUTES),
    ("@sveltejs/kit", "sveltekit", EXTRACTOR_TS_ROUTES),
    ("express", "express", EXTRACTOR_EXPRESS_NEST),
    ("@nestjs/core", "nest", EXTRACTOR_EXPRESS_NEST),
    ("koa", "koa", EXTRACTOR_EXPRESS_NEST),
    ("fastify", "fastify", EXTRACTOR_EXPRESS_NEST),
]
# Validator libraries — recorded as evidence; their rules are recovered by
# the ts/express extractors (they add validator_rule atoms), so they do not
# introduce a new extractor, only strengthen the JS/TS detection signal.
_JS_VALIDATOR_DEPS = frozenset({"zod", "joi", "yup", "class-validator", "ajv", "superstruct"})


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _package_json_deps(pkg: dict) -> Dict[str, str]:
    deps: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = pkg.get(section)
        if isinstance(block, dict):
            for name, ver in block.items():
                if isinstance(name, str):
                    deps[name] = str(ver)
    return deps


def _scan_files(repo: Path) -> tuple[List[Path], bool]:
    """Bounded, prune-aware walk returning candidate files + truncation flag."""
    out: List[Path] = []
    truncated = False
    repo_str = str(repo)
    for root, dirs, files in os.walk(repo):
        # Prune noisy/large directories in place.
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        depth = root[len(repo_str):].count(os.sep)
        if depth >= _MAX_SCAN_DEPTH:
            dirs[:] = []
        for name in files:
            out.append(Path(root) / name)
            if len(out) >= _MAX_SCAN_FILES:
                return out, True
    return out, truncated


def detect(repo_path) -> StackFingerprint:
    """Fingerprint the repo at ``repo_path`` and return a StackFingerprint.

    Deterministic and side-effect-free.  A missing/unreadable path yields an
    empty fingerprint (everything routes to blind crawl — fail-open), never
    an exception.
    """
    repo = Path(repo_path)
    fp = StackFingerprint()
    if not repo.exists() or not repo.is_dir():
        logger.warning("repo_intel.detect.path_missing path=%s", str(repo)[:200])
        return fp

    try:
        files, truncated = _scan_files(repo)
    except OSError as exc:
        logger.warning("repo_intel.detect.scan_failed error=%s", str(exc)[:200])
        return fp
    fp.scanned_files = len(files)
    fp.scan_truncated = truncated

    # stack -> DetectedStack (accumulate evidence across files).
    found: Dict[str, DetectedStack] = {}

    def _record(stack: str, extractor: Optional[str], evidence: str) -> None:
        ds = found.get(stack)
        if ds is None:
            routing = "static_extraction" if extractor else LABEL_ROUTE_TO_CRAWL_HUMAN
            ds = DetectedStack(stack=stack, static_extractor=extractor, evidence=[], routing=routing)
            found[stack] = ds
        if evidence not in ds.evidence:
            ds.evidence.append(evidence[:300])

    for path in files:
        rel = str(path.relative_to(repo)).replace(os.sep, "/")
        name = path.name.lower()

        # ── JS/TS: package.json dependency fingerprint ──
        if name == "package.json":
            pkg = _read_json(path)
            if isinstance(pkg, dict):
                deps = _package_json_deps(pkg)
                for dep_name, stack, extractor in _JS_DEP_STACKS:
                    if dep_name in deps:
                        _record(stack, extractor, f"{rel}: dependency {dep_name}@{deps[dep_name]}")
                for vdep in _JS_VALIDATOR_DEPS:
                    if vdep in deps:
                        # Attach to whichever JS extractor is present; validators
                        # sharpen recall of validator_rule atoms.
                        _record("js-validators", EXTRACTOR_EXPRESS_NEST, f"{rel}: validator {vdep}")

        # ── Spring: pom.xml / build.gradle(.kts) ──
        elif name == "pom.xml":
            text = _safe_read(path)
            if text and ("springframework" in text or "spring-boot" in text):
                _record("spring", EXTRACTOR_SPRING, f"{rel}: spring dependency in pom.xml")
        elif name in ("build.gradle", "build.gradle.kts"):
            text = _safe_read(path)
            if text and "org.springframework" in text:
                _record("spring", EXTRACTOR_SPRING, f"{rel}: org.springframework in gradle build")

        # ── Rails: Gemfile ──
        elif name == "gemfile":
            text = _safe_read(path)
            if text and re.search(r"gem\s+['\"]rails['\"]", text):
                _record("rails", None, f"{rel}: gem 'rails'")

        # ── .NET: *.csproj ──
        elif name.endswith(".csproj"):
            _record("dotnet", None, f"{rel}: .csproj project file")

        # ── OpenAPI / Swagger spec ──
        elif re.fullmatch(r"(openapi|swagger)\.(ya?ml|json)", name):
            _record("openapi", EXTRACTOR_OPENAPI, f"{rel}: OpenAPI/Swagger spec file")

        # ── Salesforce vendor markers ──
        elif name == "sfdx-project.json":
            _record("salesforce", None, f"{rel}: sfdx-project.json")
        elif name.endswith(".cls") or name.endswith(".object-meta.xml") or name.endswith(".apex"):
            _record("salesforce", None, f"{rel}: Salesforce metadata ({name})")

        # ── Guidewire vendor markers (Gosu / config) ──
        elif name.endswith(".gs") or name.endswith(".gsx") or name.endswith(".gst"):
            _record("guidewire", None, f"{rel}: Gosu source ({name})")

        # ── Pega vendor markers ──
        elif name.endswith(".pega") or name == "prconfig.xml":
            _record("pega", None, f"{rel}: Pega artifact ({name})")

    # Directory-name vendor markers (force-app is the Salesforce convention).
    if (repo / "force-app").is_dir():
        _record("salesforce", None, "force-app/: Salesforce DX source directory")

    # ── Assemble the fingerprint ──
    fp.stacks = list(found.values())

    extractors: set = set()
    ceiling_bands: Dict[str, dict] = {}
    routed: List[str] = []
    for ds in fp.stacks:
        if ds.static_extractor and ds.static_extractor in PLUGIN_CEILINGS:
            extractors.add(ds.static_extractor)
            ceiling_bands[ds.static_extractor] = PLUGIN_CEILINGS[ds.static_extractor].to_dict()
        else:
            # No static extractor → crawl + human. Attach the vendor band if
            # one is published, else a generic human-routed band.
            band = VENDOR_CEILINGS.get(ds.stack)
            if band is None:
                band = CeilingBand(ds.stack, 0.0, 0.0, 0.90, LABEL_ROUTE_TO_CRAWL_HUMAN)
            ceiling_bands[ds.stack] = band.to_dict()
            if ds.stack not in routed:
                routed.append(ds.stack)

    fp.extractors = sorted(extractors)
    fp.ceiling_bands = ceiling_bands
    fp.routed_to_crawl_human = routed
    return fp


def _safe_read(path: Path, max_bytes: int = 2_000_000) -> Optional[str]:
    """Read a bounded amount of a manifest file as text; None on error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except OSError:
        return None
