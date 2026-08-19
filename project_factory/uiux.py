"""
Project-level UI/UX preview: specs/uiux-preview.html + specs/uiux.yaml.

Before any slice is built, one agent renders what the product will FEEL like
— design tokens applied, primitives drawn, low-fi mockups of the main screens
derived from the board's in-scope events — as a single self-contained HTML
page. The human reviews it (dashboard /p/{slug}/uiux, or the file directly),
records objections as decision-register questions, and approves. The `uiux`
gate (default: human) holds slice work until that approval exists.

Design inputs, in precedence order:
  1. run.json's "design" block — tokens (colors, fonts, radii) as data; this
     is the CONTRACT. A presales engagement's design JSON lands here.
  2. run.json's "figma_url" — an optional POINTER handed to the agent as
     reference; the build never depends on Figma being reachable or sane
     (the Trueverz engagement proved it often is neither).
  3. Nothing — the agent falls back to the starter kit's existing tokens and
     says so on the page, which is itself a decision worth gating on.

The gate with uiux=auto skips this phase entirely: a preview nobody will
review is pure spend.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import yaml

from . import config as cfgmod
from .models import Usage, claude


class UiuxError(RuntimeError):
    """Preview generation or approval failed. Message names the failure."""


def preview_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / "uiux-preview.html"


def map_path(project: cfgmod.Project) -> pathlib.Path:
    """The preview's machine-readable companion: routes, screens and states.
    The HTML is what the human gates on; this YAML is what downstream agents
    are BOUND to (inlined into oracle/architect prompts — the HTML is far too
    large to inline and the oracle author has no filesystem access)."""
    return project.dir / "specs" / "uiux-map.yaml"


def record_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / "uiux.yaml"


def load_record(project: cfgmod.Project) -> dict | None:
    p = record_path(project)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text())


def is_approved(project: cfgmod.Project) -> bool:
    rec = load_record(project)
    return bool(rec and rec.get("approved_by"))


def approve(project: cfgmod.Project, by: str) -> dict:
    """Record human approval of the preview. Mirrors approve_plan's contract:
    call ONLY on an explicit human action."""
    if not by:
        raise UiuxError("--by is required: the UI/UX gate must be attributable")
    if not preview_path(project).exists():
        raise UiuxError("no preview to approve — run run-project first")
    rec = load_record(project) or {"feedback": []}
    rec["approved_by"] = by
    rec["approved_at"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    record_path(project).write_text(
        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
    return rec


def invalidate(project: cfgmod.Project, *, by: str, note: str) -> dict:
    """Feedback that requires a redraw: keep the note, drop the approval and
    the stale preview so the next run-project regenerates it."""
    rec = load_record(project) or {"feedback": []}
    rec.setdefault("feedback", []).append({
        "by": by, "note": note,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    rec.pop("approved_by", None)
    rec.pop("approved_at", None)
    record_path(project).write_text(
        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
    preview_path(project).unlink(missing_ok=True)
    map_path(project).unlink(missing_ok=True)
    return rec


def draft_preview(project: cfgmod.Project, *, usage: Usage | None = None,
                  budget_usd: float | None = None,
                  log_path: pathlib.Path | None = None) -> pathlib.Path:
    """
    One agent pass: render the UI/UX preview into specs/uiux-preview.html.
    The agent runs inside the project dir and WRITES the file (a full page is
    far past what a text return survives). Never overwrites an existing
    preview — invalidate() is the explicit path to a redraw.
    """
    target = preview_path(project)
    if target.exists():
        return target

    board = cfgmod.load_board(project.board_path)
    events = [{"id": e.get("id"), "name": e.get("name"),
               "bounded_context": e.get("bounded_context"),
               "actor": e.get("actor")}
              for e in board.get("business_events", [])
              if not e.get("out_of_scope")]

    design = project.cfg.get("design")
    figma_url = project.cfg.get("figma_url")
    design_ctx = ""
    if design:
        design_ctx += ("\nDesign tokens (the CONTRACT — every color, font "
                       "and radius on the page must come from here):\n"
                       + json.dumps(design, indent=1))
    if figma_url:
        design_ctx += (f"\nFigma reference (pointer only — use it if your "
                       f"tools can reach it, never block on it): {figma_url}")
    if not design:
        design_ctx += ("\nNo design tokens were provided. Use the starter "
                       "kit's defaults and SAY SO in a banner at the top of "
                       "the page — that absence is itself a decision the "
                       "reviewer must see.")

    feedback = (load_record(project) or {}).get("feedback", [])
    feedback_ctx = ""
    if feedback:
        feedback_ctx = ("\nPrior review feedback (a redraw was demanded — "
                        "address every note visibly):\n"
                        + json.dumps(feedback, indent=1))

    prompt = (
        "You are the UI/UX previewer for an autonomous factory build. "
        "Produce TWO files (create them; they do not exist):\n"
        "  1. specs/uiux-preview.html — a self-contained page that lets a "
        "human FEEL the product before any slice is built.\n"
        "  2. specs/uiux-map.yaml — the preview's machine-readable route "
        "map. Once the human approves the preview, this map is BINDING on "
        "every downstream agent: scenarios and screens must use exactly "
        "these routes. Structure:\n"
        "     routes:\n"
        "       - path: /search\n"
        "         screen: <name matching the preview's mockup heading>\n"
        "         audience: public|authenticated|admin\n"
        "         serves_events: [<event ids>]\n"
        "         key_states: [<states the screen must handle>]\n"
        "         layout: <one sentence, the mockup's layout>\n"
        "     Every screen mocked in the HTML must appear here and vice "
        "versa.\n\n"
        "The page must contain, in order:\n"
        "  1. A masthead naming the project and the design-token source.\n"
        "  2. The token layer: color swatches, the type scale, spacing and "
        "radii — rendered, not described.\n"
        "  3. The primitives drawn with those tokens: button variants, "
        "input, select, table row, card, badge, empty state, error state.\n"
        "  4. Low-fi mockups of the 4–8 screens the board's events imply "
        "(one per major flow/actor): real layout, grey-box content, honest "
        "about what is invented vs specified. Label each mockup with the "
        "event ids it serves.\n"
        "  5. A review checklist: every judgment call you made that a "
        "human should confirm or overturn.\n\n"
        "Rules:\n"
        "  * Entirely self-contained — inline CSS, no external requests, no "
        "JS frameworks. It must render from file://.\n"
        "  * Both light and dark scheme via prefers-color-scheme ONLY if "
        "the tokens define both; otherwise ship the one the tokens define.\n"
        "  * Invent as little as possible; where you must, flag it "
        "inline with a visible 'assumed' badge.\n\n"
        f"In-scope events:\n{json.dumps(events, indent=1)}\n"
        f"{design_ctx}\n{feedback_ctx}\n\n"
        "You may read specs/ for the board, plan, and any engagement docs. "
        "When the file is written, reply with exactly: PREVIEW WRITTEN"
    )

    out = claude(
        "uiux_previewer", prompt,
        cwd=str(project.dir),
        write_scope=["specs/uiux-preview.html", "specs/uiux-map.yaml"],
        usage=usage, budget_usd=budget_usd, log_path=log_path)

    if not target.exists():
        raise UiuxError("agent finished but specs/uiux-preview.html was not "
                        f"written (agent said: {out[:200]!r})")
    html = target.read_text(errors="replace")
    if len(html) < 2000 or "<style" not in html:
        raise UiuxError("preview exists but looks like a stub "
                        f"({len(html)} bytes) — delete it and rerun")
    mp = map_path(project)
    if not mp.exists():
        raise UiuxError("agent wrote the preview but not specs/uiux-map.yaml "
                        "— the map is what binds downstream agents; delete "
                        "the preview and rerun")
    try:
        routes = (yaml.safe_load(mp.read_text()) or {}).get("routes")
    except yaml.YAMLError as e:
        raise UiuxError(f"specs/uiux-map.yaml is not valid YAML: {e}") from e
    if not isinstance(routes, list) or not routes:
        raise UiuxError("specs/uiux-map.yaml has no routes — delete both "
                        "uiux files and rerun")
    return target
