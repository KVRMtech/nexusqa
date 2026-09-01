"""Surface-extractor base class + global registry.

The registry is intentionally simple — a list of registered extractors,
each consulted in registration order.  The first extractor whose
:meth:`matches` returns ``True`` for the scene's ``application_type``
wins; if none match, the default web extractor in
``control_extractor.ControlExtractor`` handles the scene.

Surfaces are independent — a mainframe extractor doesn't fall through to
the web extractor on partial failure.  That keeps the contract clean:
either the surface owns the screen, or it lets the web fallback handle
it from the start.

Determinism: extractors **must** emit deterministic ``control_id`` values
using ``uuid5`` keyed on stable inputs (artifact_id, scene_id, label,
selector).  The orchestrator's DB upsert path relies on
``ON CONFLICT DO NOTHING`` so non-deterministic IDs would balloon the
``evidence_controls`` table on each retry.
"""
from __future__ import annotations

from typing import Iterable, Optional


class SurfaceExtractor:
    """Base class for app-type-specific control extractors.

    Subclasses override :meth:`matches` to declare which
    ``application_type`` values they own, and :meth:`extract` to emit
    controls.  The return shape is identical to the default web
    extractor: a list of dicts with the keys

        control_id, scene_id, frame_id, artifact_id, tenant_id,
        element_type, label_text, value_text, action_kind,
        observed_value, display_label, bounding_box,
        selector_source, playwright_selector, selector_confidence,
        automation_ready

    so downstream consumers (DB persistence, triangulator action matching,
    test-case generation) work identically across surfaces.
    """

    # Human-readable surface name; used in registry diagnostics and tests.
    NAME: str = ""

    # Lower-case ``application_type`` tokens this surface claims.  Most
    # subclasses can rely on :meth:`matches`'s default, which does a
    # substring match against these tokens.
    APP_TYPE_TOKENS: tuple[str, ...] = ()

    def matches(self, app_type: str) -> bool:
        """Return True if this extractor should handle ``app_type``.

        Default implementation: case-insensitive substring match against
        :attr:`APP_TYPE_TOKENS`.  Subclasses may override for richer
        patterns (e.g. matching multiple tokens).
        """
        if not app_type:
            return False
        norm = app_type.strip().lower()
        return any(token in norm for token in self.APP_TYPE_TOKENS)

    def extract(
        self,
        scene: dict,
        frame: dict,
        artifact_id: str = "",
        tenant_id: str = "",
        all_frames: Optional[list] = None,
    ) -> list[dict]:
        """Emit controls for one scene's representative ``frame``.

        Subclasses must implement this.  The default raises so a partially
        implemented surface fails loudly in tests rather than silently
        emitting nothing in production.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement extract()"
        )


class SurfaceRegistry:
    """Ordered list of registered :class:`SurfaceExtractor` instances.

    Lookup is O(n) — fine because there are at most a handful of
    surfaces.  Registration order matters: register more specific
    surfaces first.
    """

    def __init__(self) -> None:
        self._extractors: list[SurfaceExtractor] = []

    def register(self, extractor: SurfaceExtractor) -> None:
        if not isinstance(extractor, SurfaceExtractor):
            raise TypeError(
                f"Surface registry only accepts SurfaceExtractor "
                f"instances, got {type(extractor).__name__}"
            )
        # Replace any previous registration with the same NAME so
        # double-import (e.g. via test reload) doesn't pile up duplicates.
        self._extractors = [e for e in self._extractors if e.NAME != extractor.NAME]
        self._extractors.append(extractor)

    def find(self, app_type: str) -> Optional[SurfaceExtractor]:
        """Return the first registered surface that claims ``app_type``."""
        for extractor in self._extractors:
            if extractor.matches(app_type):
                return extractor
        return None

    def names(self) -> list[str]:
        return [e.NAME for e in self._extractors]

    def all(self) -> Iterable[SurfaceExtractor]:
        return tuple(self._extractors)


# ── Module-level singleton ────────────────────────────────────────────────
_REGISTRY = SurfaceRegistry()


def register_surface(extractor: SurfaceExtractor) -> None:
    """Register ``extractor`` into the global surface registry."""
    _REGISTRY.register(extractor)


def find_surface(app_type: str) -> Optional[SurfaceExtractor]:
    """Return the global registry's first match for ``app_type`` (or ``None``)."""
    return _REGISTRY.find(app_type)


def registered_surfaces() -> list[str]:
    """Return the list of registered surface names (debug aid)."""
    return _REGISTRY.names()


def default_registry() -> SurfaceRegistry:
    """Return the singleton registry (for tests that need direct access)."""
    return _REGISTRY
