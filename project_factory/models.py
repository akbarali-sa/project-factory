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
import pathlib
import subprocess
from dataclasses import dataclass, field

from . import livelog

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

    # Project-level planning: partitions the whole board into wave-ordered
    # slices. Runs once per project; the plan is a human-reviewed artifact.
    "planner": "opus",

    # Drafts a slice's scenarios.yaml (the oracle) from the board + plan.
    # The draft replaces hand-authoring, not human judgment: it is reviewed
    # at the project plan gate / Gate A per the project's gate policy.
    "oracle_author": "opus",

    # Turns the board's open questions into an explicit working-assumptions
    # register (specs/assumptions.yaml) before oracles are drafted. Each
    # assumption is a decision the oracle then builds on — wrong-but-recorded
    # beats implicit-and-scattered, so this is Opus-grade reasoning.
    "assumptions_author": "opus",
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


class RateLimited(RuntimeError):
    """
    The CLI refused the call because of an ACCOUNT limit, not because anything
    the factory produced is wrong.

    Worth its own type because the remedy is the opposite of a crash's. A crash
    says "re-run to resume, only the crashed node re-runs" — sound advice that,
    applied to a rate limit, produces an immediate identical failure and (on an
    unattended `run-project`) a resume loop against a window that has not moved.
    A rate limit says "resume AFTER the window resets".

    On a subscription seat the binding limit is usually the WEEKLY one, so the
    wait is days, not minutes: an overnight run that stops at 3am must not look
    like broken code in the morning.
    """


# Substrings that mean "the account/service refused", not "the work failed".
# Matched against the CLI's own error output only — never against test output,
# where these words legitimately appear (a test named "rate limit" is not a
# rate limit). Keep specific: `limit` alone would match far too much.
_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "rate-limit",
    "usage limit", "usage_limit",
    "quota", "429",
    "overloaded", "overloaded_error",
    "credit balance", "insufficient credit",
    "limit reached", "limit exceeded",
)


def _rate_limit_reason(*texts: str | None) -> str | None:
    """The matched marker and a slice of surrounding context, or None.

    Returns context rather than a bare bool so the operator sees the CLI's own
    words — including any "resets at" timestamp it volunteered, which is the
    one piece of information that decides when to resume.
    """
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for marker in _RATE_LIMIT_MARKERS:
            i = low.find(marker)
            if i != -1:
                return " ".join(text[max(0, i - 120): i + 240].split())
    return None


def _summarize_stream_event(evt: dict) -> str | None:
    """One human-readable line for a stream-json event, or None to skip it
    (thinking-token counters, tool_result echoes, etc. are noise for a live
    tail). `result` events are handled by the caller, not here."""
    if evt.get("type") != "assistant":
        return None
    parts: list[str] = []
    for block in (evt.get("message") or {}).get("content") or []:
        kind = block.get("type")
        if kind == "text" and block.get("text", "").strip():
            first_line = block["text"].strip().splitlines()[0]
            parts.append(first_line[:160])
        elif kind == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input") or {}
            detail = (inp.get("file_path") or inp.get("path") or inp.get("command")
                      or inp.get("pattern") or inp.get("query") or "")
            parts.append(f"→ {name}({str(detail)[:100]})" if detail else f"→ {name}")
    return "  ".join(parts) if parts else None


def _claude_streaming(cmd: list[str], cwd: str | None, timeout: int,
                      agent: str, model: str, attempt: int,
                      log_path: pathlib.Path) -> dict:
    """
    Same contract as the plain `--output-format json` call — returns the
    final result payload — but tees every tool call and reasoning line to
    `log_path` AS THEY HAPPEN, instead of the caller seeing nothing until
    the whole (often minutes-long) call returns.
    """
    livelog.append(log_path, f"▶ {agent} ({model}, attempt {attempt + 1}) — starting")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    result: dict | None = None
    # stderr is merged into stdout here, so the CLI's own plain-text refusals
    # ("usage limit reached", an overload notice) arrive as lines that fail to
    # parse as JSON. They used to be dropped on the floor, which is precisely
    # why a rate limit surfaced as the opaque "produced no result event".
    # Keep the last few so the failure can explain itself.
    noise: list[str] = []
    try:
        for raw in livelog.iter_lines_with_timeout(proc, timeout):
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                noise.append(raw[:500])
                del noise[:-20]
                continue
            if evt.get("type") == "result":
                result = evt
                continue
            summary = _summarize_stream_event(evt)
            if summary:
                livelog.append(log_path, summary)
    finally:
        proc.wait(timeout=5)

    if result is None:
        why = _rate_limit_reason("\n".join(noise))
        if why:
            livelog.append(log_path, f"⏸ {agent} rate-limited: {why[:300]}")
            raise RateLimited(f"claude[{agent}] refused by account limit: {why}")
        tail = (" | last output: " + " ⏎ ".join(noise[-3:])) if noise else ""
        raise RuntimeError(
            f"claude[{agent}] produced no result event (exit={proc.returncode}){tail}")
    if result.get("is_error"):
        detail = str(result.get("result", ""))
        why = _rate_limit_reason(detail, "\n".join(noise))
        if why:
            livelog.append(log_path, f"⏸ {agent} rate-limited: {why[:300]}")
            raise RateLimited(f"claude[{agent}] refused by account limit: {why}")
        livelog.append(log_path, f"✗ {agent} failed: {detail[:300]}")
        raise RuntimeError(f"claude[{agent}] failed: {result.get('result')}")
    livelog.append(
        log_path,
        f"✓ {agent} done in {result.get('duration_ms', 0) / 1000:.0f}s "
        f"(${result.get('total_cost_usd', 0):.3f})",
    )
    return result


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
    log_path: pathlib.Path | None = None,
) -> str:
    """
    Invoke Claude Code headless for `agent`, returning its text result.

    cwd         run inside the generated repo so the agent can actually edit files
                and run tests (this is the whole reason we wrap Claude Code
                instead of calling the raw API)
    write_scope paths the agent may modify, e.g. ["apps/api/src/**"]
    read_only   paths it must NOT modify, e.g. ["**/tests/**"] -- the oracle guard
    log_path    when given, streams tool calls + reasoning to this file as they
                happen (see livelog.py) instead of blocking silently until the
                whole call returns — this is what the dashboard's "Live CLI"
                panel tails.

    NOTE: verify flag names against `claude --help`; the CLI surface evolves.
    We also audit writes after the fact in graph.py (belt and braces) because a
    permission flag silently changing name must never silently disable the
    oracle protection.
    """
    if budget_usd is not None and usage is not None and usage.cost_usd >= budget_usd:
        raise BudgetExceeded(f"run budget ${budget_usd} reached before {agent}")

    model = model_for(agent, attempt)
    cmd = ["claude", "-p", prompt, "--model", CLI_ALIAS[model]]
    cmd += (["--output-format", "stream-json", "--verbose"] if log_path is not None
            else ["--output-format", "json"])

    # Only the Implementer touches the filesystem; reasoning agents stay read-only.
    if write_scope:
        cmd += ["--permission-mode", "acceptEdits"]
        for pattern in write_scope:
            cmd += ["--allowedTools", f"Edit({pattern})", "--allowedTools", f"Write({pattern})"]
    for pattern in (read_only or []):
        cmd += ["--disallowedTools", f"Edit({pattern})", "--disallowedTools", f"Write({pattern})"]

    if log_path is not None:
        payload = _claude_streaming(cmd, cwd, timeout, agent, model, attempt, log_path)
    else:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            why = _rate_limit_reason(proc.stderr, proc.stdout)
            if why:
                raise RateLimited(f"claude[{agent}] refused by account limit: {why}")
            raise RuntimeError(f"claude[{agent}] failed: {proc.stderr or proc.stdout}")
        payload = json.loads(proc.stdout)

    if usage is not None:
        usage.add(agent, payload)
        if budget_usd is not None and usage.cost_usd >= budget_usd:
            raise BudgetExceeded(
                f"run budget ${budget_usd} exceeded after {agent} "
                f"(spent ${usage.cost_usd:.2f})"
            )
    return str(payload.get("result", "")).strip()
