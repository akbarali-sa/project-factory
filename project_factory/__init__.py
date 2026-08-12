"""
Project Factory — presales artifacts -> generated, test-passing vertical slices.

Pipeline shape (see graph.py for the wiring):

    ingest(det) -> gap_detect(SONNET) -> [GATE A: spec + scenarios]
    -> clone_starter(det) -> provision_db(det) -> baseline(det) -> commit_specs(det)
    -> architect(OPUS) -> contract_lint(det) -> [GATE B: contract freeze]
    -> migrate(det) -> write_tests(SONNET) -> red_first(det)
    -> implement_api  -> verify_api(det)  --fail--> diagnose(OPUS) --+
    -> implement_web  -> verify_web(det)  --fail--> diagnose(OPUS) --+
    -> launch_stack(det) -> verify_e2e(det) --fail--> diagnose(OPUS) +
    -> teardown(det) -> Draft PR -> [GATE C: review] -> END

Two design rules the whole thing rests on:

  1. MINIMISE THE AGENT SURFACE. Only five nodes use a model; everything else is
     deterministic code. Retrieval, scheduling, git, docker, migrations and test
     execution are all `det` — an LLM there buys nondeterminism for nothing.

  2. THE ORACLE IS INDEPENDENT. Scenarios are human-approved at Gate A, the Test
     Author turns them into tests, and those tests are committed BEFORE any
     implementation and are read-only to the Implementer (deny-flags plus a
     git-diff audit). "Green" therefore means something.

Usage — the CLI resolves every path by discovery:

    python -m project_factory list | new | run | doctor

Module map:
    config    workspace resolution, project discovery, layered config
    graph     the LangGraph state machine (nodes, gates, retry loops)
    models    per-agent model tiering + the Claude Code invocation wrapper
    infra     docker/postgres/app lifecycle, driving the starter's own scripts
    repo      starter mirror, project repo materialisation, git + PR
    harness   deterministic checks (red-first, mutation, traceability, scope)
"""

__version__ = "0.1.0"

# Nothing is re-exported here, deliberately. `graph` imports langgraph at module
# level, so eagerly importing it from this file would make `list`, `new` and
# `--dry-run` require the full dependency set. __main__.py imports graph lazily
# inside the run command, which keeps the cheap commands working on a bare
# checkout with only pyyaml installed.
__all__ = ["__version__"]
