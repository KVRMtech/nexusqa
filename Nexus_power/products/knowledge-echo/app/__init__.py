"""Knowledge Echo MVP — Slack inbound, classify, match, compose, dispatch.

End-to-end pipeline lives in ``app/orchestrator.py``; HTTP entry points
live in ``app/routes/`` and ``main.py``. Every external touch — LLM,
Backbone, Slack — has a dedicated client module so the orchestrator
stays focused on policy.
"""
