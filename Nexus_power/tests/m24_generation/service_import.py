"""Import a module from ONE service at a time, without cross-service poisoning.

THREE SERVICES IN THIS REPOSITORY SHIP A TOP-LEVEL PACKAGE CALLED ``app``:
``engines/qe-explorer/app``, ``platform/qe-central/app`` and
``platform/api/app``.  Python caches an imported package under its name, so the
first ``import app`` in a process WINS and every later one silently returns the
wrong service.  Nothing raises; the code simply runs against a package it was
never written for.  This repository has already lost time to that exact failure
once — a repo/VM "divergence" that turned out to be ``sys.modules`` poisoning.

The M2.4 proof has to cross all three (the M2.5 inventory lives in the explorer,
the ranking and the payload in qe-central, the compiler in the factory), so it
cannot avoid the hazard — it has to handle it explicitly.

``load`` does that: it purges every cached ``app`` module, pins ``sys.path`` to
exactly the requested service root, imports, and returns the module.  Callers use
each stage's output as PLAIN DATA and never hold a live object across a switch,
which is why this is safe rather than merely lucky: a dict does not care which
package produced it.

This is a test-support module.  Production code never crosses these boundaries
in-process — the services talk over HTTP, which is precisely why they are allowed
to share a package name.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))               # …/Nexus_power

#: Every service that ships a package named ``app``, by short name.
SERVICE_ROOTS = {
    "explorer": os.path.join(REPO, "engines", "qe-explorer"),
    "qe_central": os.path.join(REPO, "platform", "qe-central"),
    "factory": os.path.join(REPO, "platform", "api"),
}

SDK_ROOT = os.path.join(REPO, "sdk", "nexus-sdk")


def purge_app_modules() -> None:
    """Forget every cached ``app`` package and submodule.

    Without this the second service to be imported would be handed the first
    one's package object — the silent-wrong-service failure described above.
    """
    for name in [n for n in sys.modules
                 if n == "app" or n.startswith("app.")]:
        del sys.modules[name]


def load(service: str, module: str) -> Any:
    """Import ``module`` (e.g. ``"app.services.endpoint_map"``) from ``service``.

    The service root is placed FIRST on ``sys.path`` and every other service root
    is removed, so a stray relative resolution cannot reach sideways into a
    sibling service.
    """
    root = SERVICE_ROOTS.get(service)
    if root is None:
        raise ValueError(f"unknown service {service!r}; "
                         f"expected one of {sorted(SERVICE_ROOTS)}")
    purge_app_modules()
    for other in SERVICE_ROOTS.values():
        while other in sys.path:
            sys.path.remove(other)
    sys.path.insert(0, root)
    if SDK_ROOT not in sys.path:
        sys.path.insert(1, SDK_ROOT)
    # The qe-central settings singleton reads the environment at import time.
    os.environ.setdefault("NEXUS_ENV", "test")
    os.environ.setdefault("NEXUS_JWT_SECRET", "m24-proof-secret-not-a-credential")
    os.environ.setdefault("QEC_EXPLORER_TOKEN", "m24-proof-explorer-token")
    os.environ.setdefault("QEC_LOG_LEVEL", "WARNING")
    return importlib.import_module(module)


__all__ = ["REPO", "SERVICE_ROOTS", "SDK_ROOT", "purge_app_modules", "load"]
