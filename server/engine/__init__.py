"""AIRPG engine package.

The active runtime is still the deterministic stage-2 engine.  Stage-3 LLM
calls are not connected yet, but ``llm_protocol`` defines the structured
boundary for player perception, action plans, validation and committed world
outcomes.  LLM proposals never bypass the adjudication core.
"""
