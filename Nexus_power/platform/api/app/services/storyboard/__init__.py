"""Storyboard Phase 1 — additive visual-evidence derivation services.

The frozen canonical pipeline (v1.0-gpu-validated) writes raw multimodal
evidence to ``visual_frames``, ``visual_scenes``, ``app_instances``,
``evidence_controls``, ``evidence_steps``, ``cursor_events``,
``visual_flow_edges`` and ``visual_flows``.  That data is rich but not
yet a presentation artifact: form-fill sessions produce dozens of
near-identical "scenes", OCR corrupts domain strings into fake app
boundaries, and the LLaVA descriptions are too verbose to be picture
captions.

This package adds four derivation services that turn the raw graph into
a picture-first storyboard the user can browse, share and export:

* ``scene_grouper`` collapses adjacent same-screen scenes into a single
  panel with an in-scene action count.
* ``app_deduper`` folds OCR-corrupted app instances into a canonical
  grouping per registered domain.
* ``caption_rewriter`` calls an LLM to produce 5-8 word imperative
  captions per scene.
* ``frame_annotator`` server-renders cursor + click + OCR overlays
  into a single shareable PNG per representative frame.

The ``composer`` orchestrates these services lazily on the first
``/storyboard`` request and caches the result in the
``storyboard_panels`` / ``app_groupings`` / ``scene_captions`` /
``annotated_frame_cache`` tables (migration 030).  Re-derivation is
triggered by bumping the version envs documented in
``StoryboardConfig``.

This package never modifies pipeline-owned tables.  It is consumed by
``platform/api/app/routers/storyboard.py``.
"""

from .config import StoryboardConfig, load_config

__all__ = ["StoryboardConfig", "load_config"]
