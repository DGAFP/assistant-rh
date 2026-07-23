#!/usr/bin/env python3
"""Resolve the judge protocol used by the RAG quality workflow."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

OFFICIAL_JUDGE_PROVIDER = "scaleway"
OFFICIAL_JUDGE_MODEL = "qwen3-235b-a22b-instruct-2507"
OFFICIAL_JUDGE_VOTES = 3
ALLOWED_JUDGE_PROVIDERS = {"openrouter", "scaleway"}
ALLOWED_JUDGE_VOTES = {1, 3}
MODEL_RE = re.compile(r"^[A-Za-z0-9._/-]*$")


@dataclass(frozen=True)
class JudgeProtocol:
    provider: str
    model: str
    votes: int
    official: bool


def _is_true(value: str | bool) -> bool:
    return value is True or str(value).strip().lower() == "true"


def resolve_protocol(
    *,
    event_name: str,
    eval_mode: str = "smoke",
    target_environment: str = "staging",
    pr_full_requested: str | bool = False,
    skip_judge: str | bool = False,
    requested_provider: str = "openrouter",
    requested_model: str = "",
    requested_votes: str | int = 1,
) -> JudgeProtocol:
    """Return the only judge settings allowed for the selected workflow mode."""
    official = (event_name == "workflow_dispatch" and (eval_mode == "full" or target_environment == "production")) or (
        event_name == "pull_request" and _is_true(pr_full_requested)
    )
    if official:
        if _is_true(skip_judge):
            raise ValueError("skip_judge cannot be enabled for an official adoption gate")
        if requested_model.strip():
            raise ValueError("judge_model cannot be overridden for an official adoption gate")
        return JudgeProtocol(
            provider=OFFICIAL_JUDGE_PROVIDER,
            model=OFFICIAL_JUDGE_MODEL,
            votes=OFFICIAL_JUDGE_VOTES,
            official=True,
        )

    provider = requested_provider.strip().lower() or "openrouter"
    if provider not in ALLOWED_JUDGE_PROVIDERS:
        raise ValueError(f"unsupported judge_provider: {provider!r}")
    try:
        votes = int(requested_votes)
    except (TypeError, ValueError) as exc:
        raise ValueError("judge_votes must be 1 or 3") from exc
    if votes not in ALLOWED_JUDGE_VOTES:
        raise ValueError("judge_votes must be 1 or 3")

    model = requested_model.strip()
    if provider == "scaleway" and not model:
        model = OFFICIAL_JUDGE_MODEL
    if not MODEL_RE.fullmatch(model):
        raise ValueError("invalid judge_model")
    return JudgeProtocol(provider=provider, model=model, votes=votes, official=False)


def main() -> int:
    protocol = resolve_protocol(
        event_name=os.getenv("EVENT_NAME", ""),
        eval_mode=os.getenv("EVAL_MODE_INPUT", "") or "smoke",
        target_environment=os.getenv("TARGET_ENVIRONMENT_INPUT", "") or "staging",
        pr_full_requested=os.getenv("PR_FULL_REQUESTED", "false"),
        skip_judge=os.getenv("SKIP_JUDGE_INPUT", "false"),
        requested_provider=os.getenv("JUDGE_PROVIDER_INPUT", "") or "openrouter",
        requested_model=os.getenv("JUDGE_MODEL_INPUT", ""),
        requested_votes=os.getenv("JUDGE_VOTES_INPUT", "") or "1",
    )
    print(f"JUDGE_PROVIDER={protocol.provider}")
    print(f"JUDGE_MODEL_OVERRIDE={protocol.model}")
    print(f"JUDGE_VOTES={protocol.votes}")
    print(f"JUDGE_PROTOCOL_OFFICIAL={'true' if protocol.official else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
