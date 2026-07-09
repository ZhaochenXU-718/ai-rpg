"""AIRPG stage-2 rule engine.

No LLM anywhere in this package: player input is structured
(intent + objects), adjudication is storylet matching plus the
resolution_limits fallback, and narrative output is template text.
Stage 3 plugs an LLM in at the two ends (understanding, rendering)
without touching the adjudication core.
"""
