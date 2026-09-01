"""Nexus agentic-QE suite — additive, generic, never-green-wash.

Agents (each independently toggleable via the Governor; LLM agents default OFF):
  sentinel — auto-run the $0 deterministic diagnosis on every failure (no click).
  context  — LLM cross-field / business-logic data-validity reasoner (inert unless grounded).
  triage   — deterministic PRODUCT vs SCRIPT vs ENVIRONMENT source adjudicator + fix/build/flag.
  intent   — requirement/intent oracle (RTM-grounded; scaffold, P3).
Supporting:
  governor      — per-agent on/off + per-run budget + provenance stamping.
  live_options  — "Eyes": grounds Context in the captured LIVE option set.

INVARIANT: none of these turns a step green. They diagnose, route, and SUGGEST; the
orthogonal oracle + heal_policy in diff_and_heal/self_heal remain the only thing that
can make a step pass, and every agent stays INERT when it cannot ground its claim.
This whole package is additive — it imports the existing engine, it does not modify it.
"""
