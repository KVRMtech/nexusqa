"""Surface-specific control extractors.

The default :class:`ControlExtractor` in
``nexus_sdk.evidence.control_extractor`` is web-centric: it filters
browser-chrome OCR noise, emits ``getByRole`` / ``getByLabel`` Playwright
selectors, and assumes a clickable, labeled UI surface.  That works for
roughly 60–70 % of enterprise demos (SaaS, CRM, modern web apps) but
silently emits nothing for mainframe 3270 terminals, SAP GUI screens,
DB-client tools, and Office desktop apps — the audit's largest gap.

This package introduces a **surface registry** so each non-web app type
plugs in as a small module without the core pipeline growing
``if app_type == "sap"`` branches.  At extraction time the registry
matches the scene's ``application_type`` against registered surfaces and
delegates to the first that claims it; if no surface matches, the
default web extractor runs.

Adding a new surface is one file: subclass :class:`SurfaceExtractor`,
implement :meth:`matches` and :meth:`extract`, and import it from this
``__init__`` so it self-registers.
"""
from __future__ import annotations

from .base import (  # noqa: F401
    SurfaceExtractor,
    SurfaceRegistry,
    register_surface,
    find_surface,
    registered_surfaces,
    default_registry,
)

# ── Built-in surfaces (self-register on import) ──────────────────────────
# Order matters: more specific patterns must register before broader
# fallbacks so ``find_surface`` returns the most precise match.
from .mainframe_3270 import Mainframe3270Extractor  # noqa: F401
from .sap_gui import SAPGuiExtractor  # noqa: F401
from .db_client import DBClientExtractor  # noqa: F401
from .office_desktop import OfficeDesktopExtractor  # noqa: F401

# Each module calls ``register_surface(Cls())`` at import time, so the
# global registry is populated once this package is imported.
