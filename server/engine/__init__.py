"""AIRPG engine package.

The active runtime is still the deterministic stage-2 engine.  Stage-3 LLM
calls are not connected yet, but the structured boundary is in place:

- ``llm_protocol``: perception / action plan / validation / outcome models
- ``perception``: builds PlayerPerception, the single disclosure wall for
  the LLM prompt, the quote card and the status UI
- ``capabilities``: the static router that turns a validated ActionPlan
  into a resolver payload; LLM proposals never bypass the adjudication core
"""
