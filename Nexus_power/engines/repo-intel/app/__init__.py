"""repo-intel engine (S3) — repo-intelligence application package.

Turns cloned client source into provenance-tagged App-Model Atoms. This
package holds ONLY the repo-intel service code; it never imports VKPower
or platform/qe-central modules across the container boundary (patterns are
cloned, not imported — see the S3 design in
``QECentral/docs/IMPLEMENTATION_DESIGN.md`` §3.3).
"""
