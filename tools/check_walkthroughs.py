#!/usr/bin/env python3
"""Check story walkthroughs by running them through the rule engine.

Thin CLI over server.engine.walkthrough — the simulation semantics live in
server/engine/resolver.py (content-schema.md section 10.3), so the checker
and the game always agree by construction. Walkthroughs are the acceptance
cases for the stage-2 engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.walkthrough import run_walkthrough


def resolve_story_path(walkthrough_path: Path, story_ref: str) -> Path:
    if story_ref.endswith((".yaml", ".yml")):
        return Path(story_ref)
    return walkthrough_path.parent.parent / f"{story_ref}.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check story walkthroughs against engine semantics.")
    parser.add_argument("paths", nargs="+", help="Walkthrough YAML file(s).")
    args = parser.parse_args()

    exit_code = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        print(f"Checking {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            story = Story.load(resolve_story_path(path, str(data["story"])))
        except Exception as exc:
            print(f"  error: failed to load: {exc}")
            exit_code = 1
            continue

        for walkthrough in data.get("walkthroughs") or []:
            walkthrough_id = walkthrough.get("id", "<no id>")
            errors, warnings = run_walkthrough(walkthrough, story)
            for warning in warnings:
                print(f"  {walkthrough_id} warning: {warning}")
            if errors:
                exit_code = 1
                for error in errors:
                    print(f"  {walkthrough_id} error: {error}")
                print(f"  {walkthrough_id}: FAILED")
            else:
                print(f"  {walkthrough_id}: ok (ending: {walkthrough.get('target_ending')})")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
