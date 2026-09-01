"""M2.4 / T-GEN-06 — execute a GENERATED spec in a real browser.

The stop condition of this milestone is explicit: a Playwright file being
produced proves nothing.  The claim is only earned when the generated spec
EXECUTES, its network assertion executes, its outcome assertion executes, and a
real regression turns it red.  Every one of those verbs needs a browser, so this
module runs the compiled text through the actual Playwright CLI and returns what
the runner said.

WHY IT SEARCHES FOR A TOOLCHAIN RATHER THAN VENDORING ONE.  ``@playwright/test``
is a Node package with a matching browser download; this repository already has
one installed for its client build, and CI images have their own.  So the runner
LOCATES an installation, links it into the throwaway project directory (a
junction on Windows, a symlink elsewhere) and runs from there.  Nothing is
copied and no network install is attempted.

WHY IT REFUSES INSTEAD OF SKIPPING QUIETLY.  ``available()`` returns a reason, and
the proof turns a missing toolchain into an explicit, named skip.  A test that
silently passed when no browser existed would be the exact failure this milestone
is written against: an execution claim that no execution backs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent                       # …/Nexus_power

#: Where an installed ``@playwright/test`` may already live in this repository or
#: on the machine.  Searched in order; the first that resolves wins.
_CANDIDATE_ROOTS = (
    _REPO / "client" / "node_modules",
    _REPO / "verdict-portal" / "node_modules",
    _REPO / "proving-grounds" / "vkpower-life" / "node_modules",
    _REPO / "node_modules",
)

_CONFIG_TS = """\
import { defineConfig } from '@playwright/test';

// The proof harness's own config, deliberately minimal.  The factory's shipped
// playwright.config.ts pulls in an auth global-setup and a custom reporter; both
// are real parts of a delivered bundle and neither is what is under test here.
// What is under test is the SPEC, so the config gives it a browser, a timeout
// and a machine-readable reporter, and nothing else that could colour a result.
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['json', { outputFile: 'results.json' }], ['line']],
  use: {
    headless: true,
    screenshot: 'off',
    video: 'off',
    trace: 'off',
    launchOptions: { args: (process.env.NEXUS_LAUNCH_ARGS || '').split(' ').filter(Boolean) },
  },
});
"""


def node_modules_root() -> Path | None:
    """The first node_modules holding a usable ``@playwright/test``."""
    for root in _CANDIDATE_ROOTS:
        if (root / "@playwright" / "test" / "cli.js").is_file():
            return root
    return None


def available() -> tuple[bool, str]:
    """``(usable, reason)`` — a named reason whenever it is not usable."""
    if shutil.which("node") is None:
        return False, "node is not on PATH"
    root = node_modules_root()
    if root is None:
        return False, (
            "no installed @playwright/test found in "
            + ", ".join(str(p) for p in _CANDIDATE_ROOTS))
    return True, ""


def _link_node_modules(project: Path, target: Path) -> None:
    """Make ``@playwright/test`` resolvable from the throwaway project.

    Node resolves a bare specifier by walking up from the importing file, so a
    link at the project root is enough and nothing has to be copied.  A junction
    is used on Windows because it needs no elevation, unlike a directory
    symlink.
    """
    link = project / "node_modules"
    if link.exists():
        return
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


@dataclass
class RunResult:
    """What the Playwright CLI actually reported."""

    exit_code: int
    stdout: str
    stderr: str
    report: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def failed(self) -> bool:
        return self.exit_code != 0

    def failure_text(self) -> str:
        """Every error message the run produced, concatenated.

        Read from the JSON reporter rather than scraped from stdout: the proof
        asserts on WHICH oracle failed, and a claim that specific must rest on
        the structured record, not on terminal formatting that changes between
        Playwright versions.
        """
        chunks: list[str] = []
        for suite in _walk_suites(self.report.get("suites") or []):
            for spec in suite.get("specs") or []:
                for test in spec.get("tests") or []:
                    for result in test.get("results") or []:
                        error = result.get("error") or {}
                        for key in ("message", "value", "stack"):
                            if error.get(key):
                                chunks.append(str(error[key]))
                        for err in result.get("errors") or []:
                            if isinstance(err, dict) and err.get("message"):
                                chunks.append(str(err["message"]))
        if not chunks:
            chunks.append(self.stdout)
        return "\n".join(chunks)

    def statuses(self) -> list[str]:
        out: list[str] = []
        for suite in _walk_suites(self.report.get("suites") or []):
            for spec in suite.get("specs") or []:
                for test in spec.get("tests") or []:
                    for result in test.get("results") or []:
                        out.append(str(result.get("status") or ""))
        return out

    def step_titles(self) -> list[str]:
        """Every ``test.step`` the run actually entered.

        This is how the proof shows the generated steps EXECUTED rather than
        merely existing in a file — a compiled spec that never ran would report
        no steps at all.
        """
        titles: list[str] = []

        def walk(steps) -> None:
            for step in steps or []:
                if isinstance(step, dict):
                    if step.get("title"):
                        titles.append(str(step["title"]))
                    walk(step.get("steps"))

        for suite in _walk_suites(self.report.get("suites") or []):
            for spec in suite.get("specs") or []:
                for test in spec.get("tests") or []:
                    for result in test.get("results") or []:
                        walk(result.get("steps"))
        return titles


def _walk_suites(suites):
    for suite in suites or []:
        if not isinstance(suite, dict):
            continue
        yield suite
        yield from _walk_suites(suite.get("suites") or [])


def run_spec(project_dir: Path, spec_path: str, spec_text: str) -> RunResult:
    """Write ONE generated spec into a project and execute it for real.

    ``spec_path`` is the path the compiler chose (``tests/<test_id>.spec.ts``),
    written verbatim — the file that runs is the file the factory produced, not
    a re-indented or re-imported copy of it.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "playwright.config.ts").write_text(_CONFIG_TS, encoding="utf-8")
    target = project_dir / spec_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec_text, encoding="utf-8", newline="\n")

    root = node_modules_root()
    if root is None:                       # guarded by available() at call time
        raise RuntimeError("no @playwright/test installation found")
    _link_node_modules(project_dir, root)

    proc = subprocess.run(
        # No --reporter override: a CLI reporter flag REPLACES the config's
        # reporter list, which drops the outputFile and sends the JSON to stdout
        # interleaved with the line reporter's output — recoverable only by
        # scraping, which is exactly what the structured record exists to avoid.
        ["node", str(root / "@playwright" / "test" / "cli.js"), "test"],
        cwd=str(project_dir), capture_output=True, text=True, timeout=300,
        env={**os.environ, "CI": "1", "FORCE_COLOR": "0",
             # Belt and braces: honoured by the json reporter even if a future
             # config edit drops the outputFile option.
             "PLAYWRIGHT_JSON_OUTPUT_NAME": str(project_dir / "results.json")},
    )
    report: dict = {}
    results_file = project_dir / "results.json"
    if results_file.is_file():
        try:
            report = json.loads(results_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            report = {}
    if not report:
        # The json reporter also writes to stdout when no outputFile applies;
        # recover it rather than losing the structured record.
        start = proc.stdout.find("{")
        if start >= 0:
            try:
                report = json.loads(proc.stdout[start:])
            except ValueError:
                report = {}
    return RunResult(exit_code=proc.returncode, stdout=proc.stdout,
                     stderr=proc.stderr, report=report)


__all__ = ["available", "node_modules_root", "run_spec", "RunResult"]
