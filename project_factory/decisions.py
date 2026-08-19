"""
Project-level decision register: specs/decisions.yaml.

Every question the build needs answered — imported from the board, raised by
the analyst agent, or added by a human — with WHO answered it and WHEN. The
register is the project's Q&A record in git; the dashboard's register page
and the `decide` CLI both write here and nowhere else.

Relationship to the assumptions register (planner.draft_assumptions): the
decision register is the HUMAN side of the same fact base. Questions answered
here are imported into assumptions.yaml verbatim as `resolved`; questions
still open (or explicitly deferred) fall through and get a recorded working
assumption, exactly as before. The `decisions` gate (default: human) holds
the run only on open `blocker: start` questions — deferring is an explicit
human decision and unblocks just like answering.

Blocker levels, most to least urgent:
  start       nothing can begin until this is answered or deferred
  wave0       blocks the design-system / foundation work
  slices      blocks oracle authoring for at least one slice
  completion  needed before the project can be called done
  none        recorded for the register's completeness only
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

import yaml

from . import config as cfgmod
from .models import Usage, claude

BLOCKER_LEVELS = ("start", "wave0", "slices", "completion", "none")
STATUSES = ("open", "answered", "deferred")

DEFAULT_SECTIONS = [
    {"id": "A", "title": "Design system & UI/UX"},
    {"id": "B", "title": "Business rules"},
    {"id": "C", "title": "Completion & providers"},
    {"id": "D", "title": "Infrastructure & repo"},
]


class DecisionError(RuntimeError):
    """A register operation failed. Message names the failure."""


def decisions_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / "decisions.yaml"


def load_decisions(project: cfgmod.Project) -> dict | None:
    p = decisions_path(project)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text())


def save_decisions(project: cfgmod.Project, data: dict) -> pathlib.Path:
    target = decisions_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# DECISION REGISTER — every question this build needs answered,\n"
        "# who answered it, and when. The dashboard register page and\n"
        "# `python -m project_factory decide` both write here.\n"
        "# Answered entries are imported into assumptions.yaml verbatim;\n"
        "# open/deferred entries get a recorded working assumption.\n\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return target


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# Import: board questions → register (deterministic, no agent)
# -----------------------------------------------------------------------------
def import_board_questions(project: cfgmod.Project) -> dict:
    """
    Create the register from the board's questions, or merge board questions
    a prior import missed into an existing register. Answered board questions
    arrive as `answered` (verbatim — an import must not paraphrase a recorded
    decision); pending ones arrive `open`. Criticality maps to blocker level:
    critical → start, everything else → slices.
    """
    board = cfgmod.load_board(project.board_path)
    data = load_decisions(project) or {
        "version": 1,
        "project": project.slug,
        "drafted_at": _now(),
        "analyst_done": False,
        "sections": list(DEFAULT_SECTIONS),
        "questions": [],
    }
    known = {q["id"] for q in data["questions"]}
    added = 0
    for e in board.get("business_events", []):
        for q in e.get("questions", []):
            qid = q.get("id")
            if not qid or qid in known:
                continue
            known.add(qid)
            entry = {
                "id": qid,
                "section": "B",
                "question": q.get("question"),
                "context": f"raised on event {e.get('id')} — {e.get('name')}",
                "blocker": ("start" if q.get("criticality") == "critical"
                            else "slices"),
                "owner_suggested": q.get("owner", ""),
                "recommended": q.get("recommended", ""),
                "status": "open",
                "answer": None,
                "answered_by": None,
                "answered_at": None,
                "source": "board",
                "event_id": e.get("id"),
            }
            if q.get("status") == "answered":
                entry.update(status="answered", answer=q.get("answer"),
                             answered_by="board (recorded answer)",
                             answered_at=data["drafted_at"])
            data["questions"].append(entry)
            added += 1
    data["board_imported"] = True
    save_decisions(project, data)
    return data


# -----------------------------------------------------------------------------
# Resolve / add (dashboard + CLI both land here)
# -----------------------------------------------------------------------------
def resolve(project: cfgmod.Project, qid: str, *, by: str,
            answer: str | None = None, defer: bool = False) -> dict:
    """Record a human decision. Mirrors approve_plan's contract: call ONLY on
    an explicit human action. Answer and defer are both decisions — a
    deferred question converts to a working assumption downstream."""
    if not by:
        raise DecisionError("--by is required: every decision records who made it")
    if defer == bool(answer):
        raise DecisionError("exactly one of answer / defer is required")
    data = load_decisions(project)
    if not data:
        raise DecisionError("no decision register — run run-project first")
    for q in data["questions"]:
        if q["id"] == qid:
            q["status"] = "deferred" if defer else "answered"
            q["answer"] = answer
            q["answered_by"] = by
            q["answered_at"] = _now()
            save_decisions(project, data)
            return q
    raise DecisionError(f"no question '{qid}' in the register")


def reopen(project: cfgmod.Project, qid: str, *, by: str) -> dict:
    """Reopen a resolved question (e.g. the real client answer arrived and
    contradicts the recorded one). Keeps the old answer in `history`."""
    data = load_decisions(project)
    if not data:
        raise DecisionError("no decision register")
    for q in data["questions"]:
        if q["id"] == qid:
            if q["status"] != "open":
                q.setdefault("history", []).append({
                    "status": q["status"], "answer": q.get("answer"),
                    "answered_by": q.get("answered_by"),
                    "answered_at": q.get("answered_at")})
            q.update(status="open", answer=None, answered_by=None,
                     answered_at=None, reopened_by=by, reopened_at=_now())
            save_decisions(project, data)
            return q
    raise DecisionError(f"no question '{qid}' in the register")


def add_question(project: cfgmod.Project, *, question: str, by: str,
                 section: str = "B", blocker: str = "slices",
                 context: str = "", recommended: str = "",
                 owner_suggested: str = "") -> dict:
    if not by:
        raise DecisionError("--by is required: every question records who raised it")
    if blocker not in BLOCKER_LEVELS:
        raise DecisionError(f"blocker must be one of {BLOCKER_LEVELS}")
    data = load_decisions(project)
    if not data:
        raise DecisionError("no decision register — run run-project first")
    n = 1 + sum(1 for q in data["questions"] if q["source"] == "human")
    entry = {
        "id": f"H{n}", "section": section, "question": question,
        "context": context, "blocker": blocker,
        "owner_suggested": owner_suggested, "recommended": recommended,
        "status": "open", "answer": None, "answered_by": None,
        "answered_at": None, "source": "human", "added_by": by,
    }
    data["questions"].append(entry)
    save_decisions(project, data)
    return entry


# -----------------------------------------------------------------------------
# Readiness — what the decisions gate checks
# -----------------------------------------------------------------------------
def open_start_blockers(data: dict | None) -> list[dict]:
    if not data:
        return []
    return [q for q in data.get("questions", [])
            if q.get("status") == "open" and q.get("blocker") == "start"]


def stats(data: dict | None) -> dict:
    qs = (data or {}).get("questions", [])
    return {
        "total": len(qs),
        "answered": sum(1 for q in qs if q.get("status") == "answered"),
        "deferred": sum(1 for q in qs if q.get("status") == "deferred"),
        "open": sum(1 for q in qs if q.get("status") == "open"),
        "open_start_blockers": len(open_start_blockers(data)),
    }


# -----------------------------------------------------------------------------
# Analyst agent: raise the questions the specs did NOT record
# -----------------------------------------------------------------------------
def draft_analyst_questions(project: cfgmod.Project, *,
                            usage: Usage | None = None,
                            budget_usd: float | None = None,
                            log_path: pathlib.Path | None = None) -> dict:
    """
    One agent pass, once per project: read the board (and backlog when
    present) and ADD the questions nobody recorded — the gaps a delivery
    lead would raise before committing a build. Never touches existing
    entries; runs before the decisions gate so a human rules on its output.
    """
    data = load_decisions(project)
    if data is None:
        data = import_board_questions(project)
    if data.get("analyst_done"):
        return data

    board = cfgmod.load_board(project.board_path)
    existing = [{"id": q["id"], "question": q["question"]}
                for q in data["questions"]]
    backlog_ctx = ""
    if project.backlog_path:
        backlog = json.loads(project.backlog_path.read_text())
        backlog_ctx = ("\nReviewed-backlog constraints (do not question what "
                       "these already settle):\n"
                       + json.dumps({k: backlog.get(k) for k in
                                     ("hard_constraints", "not_priced",
                                      "spikes")}, indent=1))

    events = [{"id": e.get("id"), "name": e.get("name"),
               "bounded_context": e.get("bounded_context")}
              for e in board.get("business_events", [])
              if not e.get("out_of_scope")]

    prompt = (
        "You are the delivery analyst for an autonomous factory build. Below "
        "are the project's in-scope business events and every question "
        "already recorded. Your job is to raise ONLY the questions nobody "
        "recorded — the gaps that will otherwise be resolved implicitly and "
        "differently per slice.\n\n"
        "Look specifically for unrecorded questions about: screen/layout "
        "ownership and design-system gaps, actor/permission ambiguities, "
        "state machines with unnamed transitions, data ownership across "
        "events, provider choices left open, empty/error/edge states, and "
        "NFR targets nobody wrote down.\n\n"
        "Rules:\n"
        "  * Do NOT restate or rephrase a recorded question.\n"
        "  * Every question must name what it blocks; be honest about "
        "blocker level — `start` is reserved for questions where a wrong "
        "guess poisons everything downstream.\n"
        "  * recommended = the single answer the factory would proceed on if "
        "the client never replies (a decision, never a menu).\n"
        "  * 5–20 questions. Fewer good ones beat padding.\n\n"
        f"In-scope events:\n{json.dumps(events, indent=1)}\n"
        f"Already recorded ({len(existing)}):\n{json.dumps(existing, indent=1)}\n"
        f"{backlog_ctx}\n\n"
        "Return ONLY YAML:\n"
        "questions:\n"
        "  - question: <text>\n"
        "    section: <A|B|C|D>  # A design/UI, B business rules, "
        "C completion/providers, D infra\n"
        "    blocker: <start|wave0|slices|completion|none>\n"
        "    context: <what it blocks and why>\n"
        "    recommended: <the safe default answer>\n"
        "    owner_suggested: <who should answer, e.g. Client, Design Lead>\n"
    )

    errors: list[str] = []
    text = ""
    for attempt in range(2):
        out = claude(
            "decision_analyst",
            prompt if not errors
            else prompt + "\n\nYour previous draft failed validation — fix "
                          "exactly these and return the full YAML again:\n  "
                          + "\n  ".join(errors),
            cwd=str(project.dir),
            attempt=attempt, usage=usage, budget_usd=budget_usd,
            log_path=log_path)
        text = _strip_fences(out)
        errors = _validate_analyst(text)
        if not errors:
            break
        if log_path:
            from . import livelog
            livelog.append(log_path, f"analyst draft attempt {attempt + 1} "
                           "failed validation: " + "; ".join(errors)[:400])
    if errors:
        raise DecisionError("analyst questions failed validation after 2 "
                            "attempts:\n  " + "\n  ".join(errors))

    drafted = yaml.safe_load(text)["questions"]
    n = 0
    for q in drafted:
        n += 1
        data["questions"].append({
            "id": f"AN{n}", "section": q.get("section", "B"),
            "question": q["question"], "context": q.get("context", ""),
            "blocker": q["blocker"],
            "owner_suggested": q.get("owner_suggested", ""),
            "recommended": q.get("recommended", ""),
            "status": "open", "answer": None, "answered_by": None,
            "answered_at": None, "source": "analyst",
        })
    data["analyst_done"] = True
    save_decisions(project, data)
    return data


def _validate_analyst(text: str) -> list[str]:
    errors = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return ["top level must be a mapping with a `questions` list"]
    qs = data["questions"]
    if not 1 <= len(qs) <= 20:
        errors.append(f"expected 1–20 questions, got {len(qs)}")
    for i, q in enumerate(qs):
        if not isinstance(q, dict) or not q.get("question"):
            errors.append(f"questions[{i}]: missing `question`")
            continue
        if q.get("blocker") not in BLOCKER_LEVELS:
            errors.append(f"questions[{i}]: blocker must be one of "
                          f"{BLOCKER_LEVELS}, got {q.get('blocker')!r}")
        if q.get("section") not in ("A", "B", "C", "D"):
            errors.append(f"questions[{i}]: section must be A–D")
    return errors


def _strip_fences(out: str) -> str:
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", out, re.S)
    return m.group(1) if m else out
