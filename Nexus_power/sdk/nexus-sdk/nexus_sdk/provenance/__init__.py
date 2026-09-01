"""Pipeline-run provenance — make every canonical artifact reproducible.

A reproducibility layer that captures, for each canonical artifact, the
exact set of model versions, SDK versions, feature flags, and
environment fingerprint that produced it.  Stored verbatim in
``canonical_artifacts.full_artifact_json.run_provenance`` so:

  * an artifact can be **replayed** later with the same model stack
    (cloud vision providers may rotate model IDs without notice; we
    pin the resolved id at run-time);
  * two artifacts can be **diffed** to surface what changed between
    runs (model swap?  config flip?  SDK upgrade?);
  * compliance audits can trace any field back to the toolchain that
    emitted it.

Public entry point:

    from nexus_sdk.provenance import (
        PipelineRunProvenance,
        capture_run_provenance,
    )

    prov = capture_run_provenance(
        artifact_id=...,
        chain_id="nexus.canonical-processing",
        feature_flags={...},
        engine_versions={"ears": "...", "eyes": "..."},
    )
    artifact_json["run_provenance"] = prov.to_dict()
"""

from .recorder import (
    PipelineRunProvenance,
    capture_run_provenance,
    diff_provenance,
)

__all__ = [
    "PipelineRunProvenance",
    "capture_run_provenance",
    "diff_provenance",
]
