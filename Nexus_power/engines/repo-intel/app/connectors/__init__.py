"""External-source connectors for repo-intel.

Currently the git connector (:mod:`app.connectors.git`), which clones a
client repository into an isolated per-tenant workdir.  The access token
is held in memory only and scrubbed from every error/log — client tokens
are among the most sensitive data the platform handles.
"""
