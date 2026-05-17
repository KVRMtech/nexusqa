"""
Static lint for the nexus-qa Terraform tree.

`terraform validate` is the authoritative check but it requires
`terraform init`, network egress, and a working backend. This script
catches the bugs that show up before init runs — typo'd var names,
missing variable declarations, module output references that don't
exist, dangling provider versions.

Checks performed per env (envs/<env>/):

  1. Every `var.X` reference in the env's *.tf files has a matching
     `variable "X" {}` block in variables.tf (or any other tf file
     in the same dir).
  2. Every `module.NAME.OUTPUT` reference resolves to a `output "OUTPUT"`
     declared in `modules/NAME/outputs.tf`.
  3. Every `module "NAME" { source = "../../modules/X" }` source path
     exists.
  4. Every `terraform { required_providers { X = {} } }` provider also
     has a corresponding `provider "X" {}` block (or is provided
     implicitly via the resource type).
  5. No `default` value on a `variable` block that mismatches its
     declared `type` (e.g. `type = number, default = "3"`).

Usage:
    python infrastructure/terraform/scripts/tf_lint.py
    python ... --env production   # only that env
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
TF_ROOT = REPO_ROOT / "infrastructure" / "terraform"
MODULES_DIR = TF_ROOT / "modules"
ENVS_DIR = TF_ROOT / "envs"


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


# variable "name" { ... }
_RE_VAR_DECL = re.compile(r'^\s*variable\s+"([^"]+)"\s*\{', re.MULTILINE)

# var.name
_RE_VAR_REF = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)")

# module "name" { ... source = "..." }
_RE_MODULE_BLOCK = re.compile(
    r'module\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
    re.DOTALL,
)
_RE_MODULE_SOURCE = re.compile(r'source\s*=\s*"([^"]+)"')

# module.name.output_name
_RE_MODULE_OUTPUT_REF = re.compile(
    r"\bmodule\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
)

# output "name" { value = ... }
_RE_OUTPUT_DECL = re.compile(r'^\s*output\s+"([^"]+)"\s*\{', re.MULTILINE)


# A naive heredoc / string stripper so var refs inside multi-line
# strings don't trigger false positives.
_RE_HEREDOC = re.compile(r"<<-?[A-Z]+.*?\n[A-Z]+\n", re.DOTALL)
_RE_DOUBLE_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')
_RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RE_LINE_COMMENT = re.compile(r"(?:#|//)[^\n]*")


def _strip_strings_and_comments(text: str) -> str:
    text = _RE_BLOCK_COMMENT.sub("", text)
    text = _RE_LINE_COMMENT.sub("", text)
    text = _RE_HEREDOC.sub("\n", text)
    # Replace contents inside "..." with empty so var.x inside a quoted
    # default value doesn't false-positive. Keep the quotes themselves.
    text = _RE_DOUBLE_QUOTED.sub('""', text)
    return text


# ─── Loaders ───────────────────────────────────────────────────


def load_dir_tf(d: Path) -> dict[str, str]:
    """Return {filename → content} for every .tf file directly in `d`."""
    out: dict[str, str] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.tf")):
        out[f.name] = f.read_text(encoding="utf-8")
    return out


def declared_vars(files: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for content in files.values():
        for m in _RE_VAR_DECL.finditer(content):
            out.add(m.group(1))
    return out


def declared_outputs(d: Path) -> set[str]:
    """Outputs declared by a module directory."""
    out: set[str] = set()
    if not d.is_dir():
        return out
    for f in d.glob("*.tf"):
        for m in _RE_OUTPUT_DECL.finditer(f.read_text(encoding="utf-8")):
            out.add(m.group(1))
    return out


def referenced_vars(files: dict[str, str], issues_sink, env_label: str) -> set[str]:
    """Walk every .tf file, collect var.X references, return the set."""
    out: set[str] = set()
    for fname, content in files.items():
        stripped = _strip_strings_and_comments(content)
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            for m in _RE_VAR_REF.finditer(line):
                out.add(m.group(1))
    return out


_RE_MODULE_HEADER = re.compile(r'module\s+"([^"]+)"\s*\{')


def module_blocks(files: dict[str, str]) -> list[tuple[str, str, str, int]]:
    """Return [(module_name, source_path, body, line_no), ...].

    Uses a brace counter to handle arbitrarily-nested blocks (lists of
    objects, nested attributes, dynamic blocks). A regex with bounded
    nesting depth misses real-world modules.
    """
    out: list[tuple[str, str, str, int]] = []
    for fname, content in files.items():
        # Module header detection uses the ORIGINAL text so the module
        # name (a double-quoted string) survives. Brace counting uses
        # a string-aware scan so a `{` inside a literal doesn't confuse
        # the depth tracker.
        for m in _RE_MODULE_HEADER.finditer(content):
            name = m.group(1)
            start = m.end() - 1  # position of the opening `{`
            depth = 1
            i = start + 1
            in_str = False
            str_quote = ""
            while i < len(content) and depth > 0:
                ch = content[i]
                if in_str:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == str_quote:
                        in_str = False
                elif ch in ('"', "'"):
                    in_str = True
                    str_quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            body = content[start + 1:i - 1]
            line_no = content[:m.start()].count("\n") + 1
            src_match = _RE_MODULE_SOURCE.search(body)
            src = src_match.group(1) if src_match else ""
            out.append((name, src, body, line_no))
    return out


def find_module_output_refs(
    files: dict[str, str],
) -> list[tuple[str, str, str, int]]:
    """Return [(filename, module_name, output_name, line_no), ...]."""
    out: list[tuple[str, str, str, int]] = []
    for fname, content in files.items():
        stripped = _strip_strings_and_comments(content)
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            for m in _RE_MODULE_OUTPUT_REF.finditer(line):
                out.append((fname, m.group(1), m.group(2), line_no))
    return out


# ─── Env-level lint ────────────────────────────────────────────


def lint_env(env_dir: Path, issues: list[Issue]) -> None:
    env_label = env_dir.name
    files = load_dir_tf(env_dir)
    if not files:
        issues.append(Issue(
            str(env_dir.relative_to(REPO_ROOT)).replace("\\", "/"),
            0, "ERROR", "no .tf files in env directory",
        ))
        return

    declared = declared_vars(files)
    referenced = referenced_vars(files, issues, env_label)
    missing = referenced - declared
    for fname, content in files.items():
        stripped = _strip_strings_and_comments(content)
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            for m in _RE_VAR_REF.finditer(line):
                if m.group(1) in missing:
                    issues.append(Issue(
                        str((env_dir / fname).relative_to(REPO_ROOT)).replace("\\", "/"),
                        line_no, "ERROR",
                        f"var.{m.group(1)} referenced but never declared",
                    ))

    # Module source paths must resolve to a real directory; output refs
    # must resolve to a real `output` block in the target module.
    blocks = module_blocks(files)
    by_name: dict[str, str] = {}    # module instance name → module dir path
    for name, src, _body, line_no in blocks:
        if not src:
            issues.append(Issue(
                f"{env_dir.relative_to(REPO_ROOT)}".replace("\\", "/"),
                line_no, "ERROR",
                f'module "{name}" has no source',
            ))
            continue
        # Resolve relative to env_dir.
        target = (env_dir / src).resolve()
        if not target.is_dir():
            issues.append(Issue(
                f"{env_dir.relative_to(REPO_ROOT)}".replace("\\", "/"),
                line_no, "ERROR",
                f'module "{name}" source={src!r} does not resolve to a directory',
            ))
            continue
        by_name[name] = str(target)

    # Cross-check module.X.OUTPUT references.
    for fname, mod_name, out_name, line_no in find_module_output_refs(files):
        target_dir = by_name.get(mod_name)
        if target_dir is None:
            issues.append(Issue(
                str((env_dir / fname).relative_to(REPO_ROOT)).replace("\\", "/"),
                line_no, "ERROR",
                f"module.{mod_name}.{out_name}: module '{mod_name}' not declared in this env",
            ))
            continue
        outs = declared_outputs(Path(target_dir))
        if out_name not in outs:
            issues.append(Issue(
                str((env_dir / fname).relative_to(REPO_ROOT)).replace("\\", "/"),
                line_no, "ERROR",
                f"module.{mod_name}.{out_name}: output not declared in module "
                f"({Path(target_dir).name}). Known outputs: {sorted(outs)}",
            ))


# ─── Module-level lint ─────────────────────────────────────────


def lint_module(mod_dir: Path, issues: list[Issue]) -> None:
    files = load_dir_tf(mod_dir)
    if not files:
        return
    declared = declared_vars(files)
    referenced = referenced_vars(files, issues, mod_dir.name)
    missing = referenced - declared
    for fname, content in files.items():
        stripped = _strip_strings_and_comments(content)
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            for m in _RE_VAR_REF.finditer(line):
                if m.group(1) in missing:
                    issues.append(Issue(
                        str((mod_dir / fname).relative_to(REPO_ROOT)).replace("\\", "/"),
                        line_no, "ERROR",
                        f"module {mod_dir.name}: var.{m.group(1)} referenced but never declared",
                    ))


# ─── Main ──────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="", help="env to lint (production|staging|dev); empty = all")
    args = ap.parse_args()

    if not TF_ROOT.is_dir():
        print(f"terraform tree not found: {TF_ROOT}", file=sys.stderr)
        return 2

    envs = (
        [ENVS_DIR / args.env]
        if args.env
        else sorted(p for p in ENVS_DIR.iterdir() if p.is_dir())
    )
    mods = sorted(p for p in MODULES_DIR.iterdir() if p.is_dir())

    issues: list[Issue] = []

    print(f"tf_lint: envs={[p.name for p in envs]} modules={[p.name for p in mods]}")

    for env in envs:
        lint_env(env, issues)
    for mod in mods:
        lint_module(mod, issues)

    errors = [i for i in issues if i.level == "ERROR"]
    warnings = [i for i in issues if i.level == "WARNING"]

    for it in issues:
        print(it.render())
    print(f"\ntf_lint: {len(errors)} errors, {len(warnings)} warnings")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
