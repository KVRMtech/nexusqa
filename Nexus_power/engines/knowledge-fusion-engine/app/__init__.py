"""Knowledge Fusion Engine — Phase 1 substrate builder.

Subscribes to ``spine.canonical_artifact.ready``, leases jobs from
``indexing_jobs``, chunks transcripts into ``transcript_segments``,
emits ``substrate.indexed`` and ``substrate.failed`` events.

See ``main.py`` for the service entrypoint and ``app/worker.py`` for
the leasing / retry / DLQ loop.
"""
