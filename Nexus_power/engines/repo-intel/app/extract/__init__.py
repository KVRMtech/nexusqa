"""Per-stack source extractors (the code-comprehension core of S3).

Each extractor turns cloned client source into provenance-tagged
:class:`~app.extract.registry.Atom` objects. Every Atom carries
``file:line`` + a verbatim, secret-scrubbed quote + a rule-band
confidence (NEVER an LLM score). The registry runs every applicable
extractor in isolation: a failing/unknown extractor marks the universe
``degraded`` — never a silent partial.
"""
