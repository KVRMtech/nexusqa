"""Security primitives for repo-intel (secret scrubbing).

Client SOURCE CODE and access tokens are the most sensitive data this
engine touches. Everything a human ever reads back — every Atom quote,
every log line — passes through :mod:`app.security.secret_scrub` first.
"""
