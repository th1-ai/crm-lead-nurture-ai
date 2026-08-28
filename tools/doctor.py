#!/usr/bin/env python3
"""tools/doctor.py - is CRM / Lead Nurture AI configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks plus this agent's own: the pipeline
rules, the prompt files, and whether the outreach fixtures (avatars/signals)
are present. Exits 0 when everything passed, 1 when a FAIL line needs fixing.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402


def check_pipeline_rules(settings: Settings) -> Check:
    keys = ("value_priority", "availability_check", "language_match", "discount_floor_pct")
    missing = [k for k in keys if settings.agent_get(f"pipeline.{k}") is None]
    if missing:
        return Check("pipeline rules", FAIL, f"missing {', '.join(missing)} in config/agent.yaml",
                     "Copy config/agent.example.yaml to config/agent.yaml.")
    floor = settings.agent_get("pipeline.discount_floor_pct")
    return Check("pipeline rules", PASS,
                 f"value_priority={settings.agent_get('pipeline.value_priority')} "
                 f"availability_check={settings.agent_get('pipeline.availability_check')} "
                 f"language_match={settings.agent_get('pipeline.language_match')} "
                 f"discount_floor={floor}%")


def check_prompts() -> Check:
    missing = [p for p in ("prompts/classify.md", "prompts/draft.md", "prompts/coach-suggestion.md",
                           "prompts/schemas/classify.json", "prompts/schemas/draft.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "classify.md + draft.md + coach-suggestion.md present")


def check_outreach_fixtures() -> Check:
    prospects = REPO_ROOT / "fixtures" / "outreach" / "prospects.json"
    sources = REPO_ROOT / "fixtures" / "outreach" / "sources.json"
    if not prospects.is_file() or not sources.is_file():
        return Check("outreach fixtures", WARN, "fixtures/outreach/*.json missing",
                     "Only affects `scan`/`enrich` on fixture data - restore them from git "
                     "or point at your own signal sources.")
    return Check("outreach fixtures", PASS, "prospects.json + sources.json present")


def check_win_back(settings: Settings) -> Check:
    enabled = bool(settings.agent_get("subagents.win_back.enabled", False))
    status = PASS if enabled else WARN
    detail = ("enabled" if enabled else
             "disabled - the parent agent works fully without it, see docs/sub-agents.md")
    return Check("win-back sub-agent", status, detail)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="CRM / Lead Nurture AI - doctor")

    checks = run_checks(settings, extra=[check_pipeline_rules, check_win_back])
    checks.append(check_prompts())
    checks.append(check_outreach_fixtures())
    return print_table(checks, title="CRM / Lead Nurture AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
