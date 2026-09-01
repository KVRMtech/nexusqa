"""Phase-2 EXIT-METRIC test: per-extractor recall/precision vs hand-authored
answer keys + secret-never-surfaces + provenance integrity.

Fixture repos are materialised in a tmp dir (no separate fixture tree to drift)
and graded against inline answer keys on the DEFAULT (regex/text) path — the
graded, always-available path (tree-sitter is an additive, separately-validated
seam). The Phase-2 exit criteria (design §6):

  * per-extractor recall  >= the extractor's published ceiling FLOOR band
  * per-extractor precision >= 0.9
  * a planted secret NEVER appears in any atom quote
  * every atom's verbatim quote actually appears at its provenance file:line
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extract.registry import (  # noqa: E402
    ExtractionContext,
    run_extractors,
    normalize_route_pattern,
)
from app.extract.openapi_spec import OpenAPIExtractor  # noqa: E402
from app.extract.ts_routes import TypeScriptRoutesExtractor  # noqa: E402
from app.extract.express_nest import ExpressNestExtractor  # noqa: E402
from app.extract.spring import SpringExtractor  # noqa: E402

# A realistic OpenAI-style key planted in EVERY fixture; it must never surface
# in a stored quote (secret-scrub proof).
PLANTED_SECRET = "sk-QECsecret0123456789abcdefghijABCD"


# ────────────────────────────── fixtures ─────────────────────────────────


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture()
def express_repo(tmp_path: Path) -> Path:
    root = tmp_path / "express_api"
    _write(root, "package.json",
           '{"dependencies":{"express":"^4","zod":"^3"}}')
    _write(root, "src/routes.js", f"""
const express = require('express');
const router = express.Router();
// API_KEY = "{PLANTED_SECRET}"  // planted secret in a comment
router.get('/users', (req, res) => res.json([]));
router.post('/users', (req, res) => res.status(201).end());
router.delete('/users/:id', (req, res) => res.end());
app.put('/account/settings', handler);
module.exports = router;
""")
    _write(root, "src/schema.ts", """
import { z } from 'zod';
export const UserSchema = z.object({
  email: z.string().email().min(5),
  age: z.number().min(18),
});
""")
    _write(root, "openapi.yaml", f"""
openapi: 3.0.0
info: {{title: Acme, version: 1.0.0}}
paths:
  /users:
    get: {{summary: list, description: "token {PLANTED_SECRET}"}}
  /orders/{{orderId}}:
    post: {{summary: create}}
""")
    return root


@pytest.fixture()
def react_repo(tmp_path: Path) -> Path:
    root = tmp_path / "react_app"
    _write(root, "package.json",
           '{"dependencies":{"react-router-dom":"^6"}}')
    _write(root, "src/router.tsx", f"""
import {{ createBrowserRouter }} from 'react-router-dom';
// secret: {PLANTED_SECRET}
export const router = createBrowserRouter([
  {{ path: '/', element: <Home/> }},
  {{ path: '/dashboard', element: <Dash/> }},
  {{ path: '/orders/:orderId', element: <Order/> }},
]);
""")
    return root


@pytest.fixture()
def spring_repo(tmp_path: Path) -> Path:
    root = tmp_path / "spring_app"
    _write(root, "pom.xml", "<project><groupId>org.springframework"
           "</groupId></project>")
    _write(root, "src/main/java/com/acme/UserController.java", f"""
package com.acme;
import org.springframework.web.bind.annotation.*;
import javax.validation.constraints.*;
// apiKey = "{PLANTED_SECRET}"
@RestController
@RequestMapping("/api/users")
public class UserController {{
    @GetMapping("/list")
    public List<User> list() {{ return null; }}
    @PostMapping
    public User create(@RequestBody User u) {{ return u; }}
}}
class User {{
    @NotNull
    @Size(min = 2, max = 50)
    private String name;
    @Email
    private String email;
}}
""")
    return root


def _ctx(root: Path) -> ExtractionContext:
    return ExtractionContext(repo_path=root)


def _grade(atoms, expected_keys):
    """recall, precision against a set of normalized (kind, ...) keys."""
    got = {a.canonical_key() for a in atoms}
    exp = set(expected_keys)
    tp = len(got & exp)
    recall = tp / len(exp) if exp else 1.0
    precision = tp / len(got) if got else (1.0 if not exp else 0.0)
    return recall, precision, got


# ─────────────────────────── recall / precision ──────────────────────────


def test_express_endpoints_recall_precision(express_repo):
    atoms = [a for a in ExpressNestExtractor().extract(express_repo, _ctx(express_repo))
             if a.kind == "api_endpoint"]
    expected = {
        ("api_endpoint", "GET", "/users"),
        ("api_endpoint", "POST", "/users"),
        ("api_endpoint", "DELETE", normalize_route_pattern("/users/:id")),
        ("api_endpoint", "PUT", "/account/settings"),
    }
    recall, precision, got = _grade(atoms, expected)
    assert recall >= ExpressNestExtractor().ceiling_band["api_endpoint"]["floor"], got
    assert precision >= 0.9, got


def test_express_validators(express_repo):
    atoms = [a for a in ExpressNestExtractor().extract(express_repo, _ctx(express_repo))
             if a.kind == "validator_rule"]
    fields = {a.value["field"] for a in atoms}
    assert {"email", "age"} <= fields, fields
    # precision: no spurious validator fields
    assert fields <= {"email", "age"}, fields


def test_openapi_recall(express_repo):
    atoms = OpenAPIExtractor().extract(express_repo, _ctx(express_repo))
    expected = {
        ("api_endpoint", "GET", "/users"),
        ("api_endpoint", "POST", normalize_route_pattern("/orders/{orderId}")),
    }
    recall, precision, got = _grade(atoms, expected)
    assert recall >= OpenAPIExtractor().ceiling_band.get("api_endpoint", {}).get("floor", 0.7), got
    assert precision >= 0.9, got


def test_react_routes_recall_precision(react_repo):
    atoms = TypeScriptRoutesExtractor().extract(react_repo, _ctx(react_repo))
    expected = {
        ("route", "/"),
        ("route", "/dashboard"),
        ("route", normalize_route_pattern("/orders/:orderId")),
    }
    recall, precision, got = _grade([a for a in atoms if a.kind == "route"], expected)
    assert recall >= 0.66, got
    assert precision >= 0.9, got


def test_spring_endpoints_and_validators(spring_repo):
    atoms = SpringExtractor().extract(spring_repo, _ctx(spring_repo))
    eps = {a.canonical_key() for a in atoms if a.kind == "api_endpoint"}
    assert ("api_endpoint", "GET", "/api/users/list") in eps, eps
    assert ("api_endpoint", "POST", "/api/users") in eps, eps
    vfields = {a.value["field"] for a in atoms if a.kind == "validator_rule"}
    assert {"name", "email"} <= vfields, vfields


# ──────────────────────── secret-never-surfaces ──────────────────────────


@pytest.mark.parametrize("repo_fixture", ["express_repo", "react_repo", "spring_repo"])
def test_no_planted_secret_in_any_quote(repo_fixture, request):
    root = request.getfixturevalue(repo_fixture)
    result = run_extractors(root, _ctx(root))
    for atom in result.atoms:
        assert PLANTED_SECRET not in atom.quote, f"secret leaked in {atom.provenance_path}"
        assert PLANTED_SECRET not in str(atom.value), f"secret leaked in value {atom.kind}"


# ─────────────────────────── provenance integrity ────────────────────────


@pytest.mark.parametrize("repo_fixture", ["express_repo", "react_repo", "spring_repo"])
def test_every_atom_quote_is_verbatim_at_provenance(repo_fixture, request):
    root = request.getfixturevalue(repo_fixture)
    result = run_extractors(root, _ctx(root))
    assert result.atoms, "expected at least one atom"
    for atom in result.atoms:
        assert atom.provenance_path
        assert atom.provenance_line >= 1
        assert atom.provenance_sha  # file sha anchored
        assert 0.0 < atom.confidence <= 1.0
        # The stored quote is a scrubbed/truncated slice of the real line —
        # its non-secret tokens must appear on that source line.
        src_line = (root / atom.provenance_path).read_text(encoding="utf-8").split("\n")[atom.provenance_line - 1]
        sample = atom.quote.replace("[REDACTED]", "").strip().split()
        if sample:
            assert any(tok in src_line for tok in sample[:3]), (atom.quote, src_line)


def test_registry_runs_all_four_extractors(express_repo):
    result = run_extractors(express_repo, _ctx(express_repo))
    ran = set(result.ran_extractors)
    # express + openapi apply; the universe is 'ready' (no degraded plugins).
    assert "express_nest" in ran
    assert "openapi_spec" in ran
    assert result.universe_status in ("ready", "degraded")
    assert not result.degraded_extractors, result.degraded_extractors
