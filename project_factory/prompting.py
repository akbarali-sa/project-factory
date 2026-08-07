"""Compact-but-faithful rendering of spec artifacts for prompts.

The scenarios file is the human-authored oracle, and downstream checks
assert on its content: check_traceability requires every scenario id to
reach the test author, and check_contract requires every `HTTP <code>`
in a `then:` clause to reach the architect. Nothing in this module may
drop, cap, or truncate scenario content — it only reshapes YAML into a
tighter bulleted form.
"""

from __future__ import annotations

from typing import Iterable


def _append_field(lines: list[str], name: str, value) -> None:
    if value is None or value == "" or value == []:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            lines.append(f"    {name}: {item}")
    else:
        lines.append(f"    {name}: {value}")


_KNOWN = ("id", "title", "given", "when", "then", "traces_to")


def render_scenarios(scenarios: Iterable[dict] | None) -> str:
    """Render every scenario completely: id, title, given/when/then, extras.

    Never truncates or omits items — fidelity is load-bearing (see module
    docstring). Savings come only from dropping YAML syntax overhead.
    """
    blocks: list[str] = []
    for sc in scenarios or []:
        head = f"- {sc.get('id', '?')}"
        if sc.get("title"):
            head += f": {sc['title']}"
        lines = [head]
        _append_field(lines, "given", sc.get("given"))
        _append_field(lines, "when", sc.get("when"))
        _append_field(lines, "then", sc.get("then"))
        if sc.get("traces_to"):
            lines.append(f"    traces_to: {', '.join(map(str, sc['traces_to']))}")
        for key, value in sc.items():
            if key not in _KNOWN:
                _append_field(lines, key, value)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
