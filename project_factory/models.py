"""
Model tiering + the Claude Code invocation wrapper.

WHY TIER AT ALL
    Cost. A gap-detection pass is classification (Haiku handles it); contract
    design and failure diagnosis are hard reasoning (Opus). Paying Opus rates
    for every node is how "$15 per project" turns into $900.

BILLING NOTE (matters for your setup)
    Your spike proved `claude -p` works on subscription login with no API key.
    We keep that: EVERY agent below goes through the Claude Code CLI with
    `--model`, so nothing needs ANTHROPIC_API_KEY.

    Later, the three pure-reasoning agents (spec_analyst, architect,
    diagnostician) can migrate to the raw Anthropic SDK to get structured
    outputs + tighter control. That switch needs an API key and a billing
    decision. Until then, CLI + --model gives you tiering for free.
    Keep ANTHROPIC_API_KEY UNSET to bill your subscription.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

# -----------------------------------------------------------------------------
# Current-generation Claude models (as of July 2026).
# CLI aliases are what `claude --model` accepts; API ids are for the later
# raw-SDK migration of the reasoning-only agents.
# -----------------------------------------------------------------------------
CLI_ALIAS = {
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
}

API_ID = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}


# -----------------------------------------------------------------------------
# Per-agent tier. This is the table from the design, made executable.
# -----------------------------------------------------------------------------
AGENT_TIER: dict[str, str] = {
    # Classification over the IR. Cheap model is sufficient; if recall on
    # obvious gaps is < ~90%, fix the prompt, not the model.
    "spec_analyst": "haiku",

    # Hard reasoning, small output, high blast radius (everything downstream
    # depends on the contract). Worth Opus.
    "architect": "opus",

    # Mechanical-ish translation of approved scenarios -> test files.
    "test_author": "sonnet",

    # Bulk of the work. Starts cheap, escalates on retry (see below).
    "implementer": "sonnet",

    # Reads failure output and reasons about cause. Hard, but rare.
    "diagnostician": "opus",
}

# Escalate the Implementer after repeated failure: cheap first, expensive only
# when needed. This is a real cost lever — most slices pass on attempt 1.
IMPLEMENTER_ESCALATION = {0: "sonnet", 1: "sonnet", 2: "opus"}


def model_for(agent: str, attempt: int = 0) -> str:
    if agent == "implementer":
        return IMPLEMENTER_ESCALATION.get(attempt, "opus")
    return AGENT_TIER[agent]


# -----------------------------------------------------------------------------
# Invocation
# -----------------------------------------------------------------------------

@dataclass
class Usage:
    """Accumulated spend for one graph run. Lives in LangGraph state."""
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    by_agent: dict[str, float] = field(default_factory=dict)

    def add(self, agent: str, payload: dict) -> None:
        c = float(payload.get("total_cost_usd") or 0.0)
        self.cost_usd += c
        self.turns += int(payload.get("num_turns") or 0)
        self.duration_ms += int(payload.get("duration_ms") or 0)
        self.by_agent[agent] = self.by_agent.get(agent, 0.0) + c


class BudgetExceeded(RuntimeError):
    """Circuit breaker — abort the run rather than burn the ceiling."""


def claude(
    agent: str,
    prompt: str,
    *,
    cwd: str | None = None,
    attempt: int = 0,
    usage: Usage | None = None,
    budget_usd: float | None = None,
    write_scope: list[str] | None = None,
    read_only: list[str] | None = None,
    timeout: int = 1800,
) -> str:
    """
    Invoke Claude Code headless for `agent`, returning its text result.

    cwd         run inside the generated repo so the agent can actually edit files
                and run tests (this is the whole reason we wrap Claude Code
                instead of calling the raw API)
    write_scope paths the agent may modify, e.g. ["apps/api/src/**"]
    read_only   paths it must NOT modify, e.g. ["**/tests/**"] -- the oracle guard

    NOTE: verify flag names against `claude --help`; the CLI surface evolves.
    We also audit writes after the fact in graph.py (belt and braces) because a
    permission flag silently changing name must never silently disable the
    oracle protection.
    """
    if budget_usd is not None and usage is not None and usage.cost_usd >= budget_usd:
        raise BudgetExceeded(f"run budget ${budget_usd} reached before {agent}")

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", CLI_ALIAS[model_for(agent, attempt)],
    ]

    # Only the Implementer touches the filesystem; reasoning agents stay read-only.
    if write_scope:
        cmd += ["--permission-mode", "acceptEdits"]
        for pattern in write_scope:
            cmd += ["--allowedTools", f"Edit({pattern})", "--allowedTools", f"Write({pattern})"]
    for pattern in (read_only or []):
        cmd += ["--disallowedTools", f"Edit({pattern})", "--disallowedTools", f"Write({pattern})"]

    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude[{agent}] failed: {proc.stderr or proc.stdout}")

    payload = json.loads(proc.stdout)
    if usage is not None:
        usage.add(agent, payload)
        if budget_usd is not None and usage.cost_usd >= budget_usd:
            raise BudgetExceeded(
                f"run budget ${budget_usd} exceeded after {agent} "
                f"(spent ${usage.cost_usd:.2f})"
            )
    return str(payload.get("result", proc.stdout)).strip()
