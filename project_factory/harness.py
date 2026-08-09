"""
Deterministic test harness — how you test the FACTORY, not just its output.

Every check here is code, not a model opinion. They are the reason a green
run means something.

  check_red_first      generated tests must FAIL with no implementation.
                       Kills vacuous/tautological tests. Cheapest high-value
                       check in the whole system.
  check_mutation       inject known bugs into working code, assert the tests
                       catch them. This MEASURES ORACLE STRENGTH — the number
                       that tells you whether "green" implies "correct".
  check_traceability   every approved scenario maps to >=1 test; nothing
                       provisional leaked in.
  check_contract       prisma validate + OpenAPI lint + completeness vs scenarios.
  check_write_scope    git-diff audit: the agent never touched tests/.
  check_prompt_leak    no domain nouns in prompts -> the factory stays portable
                       to project #2.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field


@dataclass
class Result:
    ok: bool
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    # Extra findings a check wants to surface at a gate without failing the
    # run — e.g. infrastructure files an agent touched that a human should see.
    data: dict = field(default_factory=dict)


def _run(cmd: list[str], cwd: str, timeout: int = 900,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})} if env else None)


# -----------------------------------------------------------------------------
# The oracle itself
# -----------------------------------------------------------------------------
@dataclass
class TestRun:
    ok: bool
    output: str
    summary: str


def run_tests(repo: str, workspace: str | None = None) -> TestRun:
    """
    Run unit/integration tests via the starter's turbo pipeline.

    `workspace` is a turbo filter (e.g. "@repo/api", "@repo/web") — NOT a path.
    The starter's script is `turbo run test`, so filtering is how you scope it.
    """
    cmd = ["pnpm", "test"] + ([f"--filter={workspace}"] if workspace else [])
    p = _run(cmd, repo)
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"(\d+)\s+passed.*?(\d+)\s+failed", out, re.S)
    summary = m.group(0)[:80] if m else f"exit={p.returncode}"
    return TestRun(ok=p.returncode == 0, output=out, summary=summary)


# -----------------------------------------------------------------------------
# 1. Red-first
# -----------------------------------------------------------------------------
def check_red_first(repo: str, slice_id: str, workspace: str = "@repo/api") -> Result:
    """
    Tests just written, implementation absent -> the suite MUST fail.

    If it passes, the tests assert nothing real and the whole oracle is a
    placebo. This must be a hard stop, never a warning.
    """
    res = run_tests(repo, workspace=workspace)
    if res.ok:
        return Result(
            False,
            errors=[
                "Generated tests PASS with no implementation present — they are "
                "vacuous. Likely causes: assertions on mocks, empty test bodies, "
                "or `it.skip`. Regenerate with stricter instructions."
            ],
        )
    if "0 passed" in res.output and "0 failed" in res.output:
        return Result(False, errors=["No tests were collected — file naming or config issue."])
    return Result(True, summary="tests fail as expected (red)")


# -----------------------------------------------------------------------------
# 2. Mutation testing — measures how strong the oracle actually is
# -----------------------------------------------------------------------------
DEFAULT_MUTANTS = [
    # (description, regex to find, replacement)
    # Chosen to attack the starter's own documented conventions — if a test
    # suite cannot catch a violation of a load-bearing convention, it is weak.
    ("drop unique constraint", r"@@unique\(\[[^\]]+\]\)", ""),
    ("invert a guard", r"if \(!(\w+)\)", r"if (\1)"),
    ("off-by-one on length", r"\.length === (\d+)", r".length >= \1"),
    ("weaken status code", r"HttpStatus\.UNPROCESSABLE_ENTITY", "HttpStatus.OK"),
    # starter convention: multi-statement writes belong in $transaction
    ("skip transaction", r"\$transaction\(", "Promise.all(["),
    # starter convention: nullIfMissing() makes a missing row 404 instead of 500
    ("drop nullIfMissing wrapper", r"nullIfMissing\(", "await ("),
]


def check_mutation(repo: str, target_glob: str = "apps/api/src/**/*.ts",
                   mutants=None) -> Result:
    """
    Take the CURRENTLY GREEN code, inject a bug, and assert the suite goes red.

    Score = caught / applied. Track it over time:
      < 0.6  your tests are decorative; green builds mean little
      > 0.8  green is meaningful
    Run this per wave, not per slice (it re-runs the suite once per mutant).
    """
    mutants = mutants or DEFAULT_MUTANTS
    root = pathlib.Path(repo)
    files = list(root.glob(target_glob.replace("apps/api/", "apps/api/")))
    if not files:
        return Result(False, errors=[f"no files matched {target_glob}"])

    applied = caught = 0
    errors: list[str] = []

    for desc, pattern, repl in mutants:
        hit = next((f for f in files if re.search(pattern, f.read_text())), None)
        if hit is None:
            continue
        original = hit.read_text()
        mutated = re.sub(pattern, repl, original, count=1)
        if mutated == original:
            continue

        applied += 1
        hit.write_text(mutated)
        try:
            if not run_tests(repo).ok:
                caught += 1
            else:
                errors.append(f"SURVIVED: '{desc}' in {hit.relative_to(root)} — "
                              f"no test detects this bug")
        finally:
            hit.write_text(original)

    if applied == 0:
        return Result(False, errors=["no mutants applicable — extend DEFAULT_MUTANTS"])
    score = caught / applied
    return Result(
        ok=score >= 0.8,
        summary=f"mutation score {caught}/{applied} = {score:.0%}",
        errors=errors,
    )


# -----------------------------------------------------------------------------
# 3. Traceability
# -----------------------------------------------------------------------------
def check_traceability(repo: str, scenarios: dict) -> Result:
    test_dir = pathlib.Path(repo) / "apps/api/__tests__" / scenarios["slice"]["id"]
    if not test_dir.exists():
        return Result(False, errors=[f"missing test dir {test_dir}"])
    blob = "\n".join(p.read_text() for p in test_dir.rglob("*.ts"))

    missing = [s["id"] for s in scenarios["scenarios"] if s["id"] not in blob]
    leaked = [
        s["id"] for s in scenarios.get("provisional_scenarios", [])
        if s["id"] in blob
    ]
    errors = []
    if missing:
        errors.append(f"scenarios with no test: {missing}")
    if leaked:
        errors.append(f"PROVISIONAL scenarios were implemented (must stay blocked): {leaked}")
    return Result(
        ok=not errors,
        summary=f"{len(scenarios['scenarios']) - len(missing)}/"
                f"{len(scenarios['scenarios'])} scenarios covered",
        errors=errors,
    )


# -----------------------------------------------------------------------------
# 4. Contract validation — the payoff of a tight repo structure
# -----------------------------------------------------------------------------
def splice_schema(existing: str, new_models: str) -> str:
    """
    Fold a contract's prisma block into an existing schema: a model/enum the
    contract REDECLARES replaces the existing block wholesale (redeclare-to-
    modify), a new one is appended, and everything the contract doesn't
    mention survives untouched. Single source of truth for both migrate()
    (which writes the result) and check_contract (which validates it) — a
    contract may legitimately reference models it does not redeclare, so
    validating its block in isolation false-positives on every later slice.
    """
    for block in re.split(r"\n(?=model |enum )", new_models.strip()):
        block = block.strip()
        name = re.match(r"(?:model|enum)\s+(\w+)", block)
        if not name:
            continue
        existing_block = re.search(
            rf"^(?:model|enum)\s+{name.group(1)}\b[^\n]*\{{.*?\n\}}\n?",
            existing, re.M | re.S)
        if existing_block:
            existing = (existing[:existing_block.start()] + block + "\n"
                        + existing[existing_block.end():])
        else:
            existing = existing.rstrip() + "\n\n" + block + "\n"
    return existing


def check_contract(contract: str, scenarios: dict, repo: str) -> Result:
    errors: list[str] = []

    prisma = re.search(r"```prisma\n(.*?)```", contract, re.S)
    openapi = re.search(r"```ya?ml\n(.*?)```", contract, re.S)
    if not prisma:
        errors.append("no prisma block in Architect output")
    if not openapi:
        errors.append("no OpenAPI block in Architect output")
    if errors:
        return Result(False, errors=errors)

    # The SPLICED schema — the same document migrate() will write — is what
    # every schema check below must reason about. Judging the contract's block
    # in isolation false-positives from the second slice on: a contract
    # legitimately references (and does not redeclare) models, enums and
    # aggregates that already live in the repo's schema.
    base = pathlib.Path(repo) / "apps/api/prisma/schema.prisma"
    if base.exists():
        merged = splice_schema(base.read_text(), prisma.group(1))
    else:
        # No repo schema yet (unit tests, dry validation): fall back to the
        # contract block alone, which then must be self-contained.
        merged = prisma.group(1)

    # 4a. prisma validate
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td) / "schema.prisma"
        tmp.write_text(merged)

        # The temp schema lives outside the repo, so Prisma's own .env
        # discovery (relative to the schema path / cwd) won't find the
        # project's real apps/api/.env — read DATABASE_URL from it directly
        # rather than relying on that discovery.
        db_url = ""
        api_env = pathlib.Path(repo) / "apps/api/.env"
        if api_env.exists():
            for line in api_env.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    db_url = line.split("=", 1)[1].strip().strip('"')
                    break

        p = _run(["npx", "prisma", "validate", f"--schema={tmp}"], repo, timeout=180,
                 env={"DATABASE_URL": db_url} if db_url else None)
        if p.returncode != 0:
            errors.append(f"prisma validate failed: {(p.stdout + p.stderr)[-800:]}")

    # 4b. every aggregate is ACCOUNTED FOR by the contract — as a persisted
    # model in the spliced schema (so one a later slice merely READS counts,
    # without forcing a pointless redeclaration), or as a read-model surfaced
    # through the API. A projection like WorkerProductivity is computed from
    # other tables on request; demanding a table for it would be demanding a
    # denormalised cache nobody asked for. What must never pass is an
    # aggregate the contract simply forgot.
    for agg in scenarios["aggregates"]:
        in_schema = re.search(rf"model\s+{agg}\b", merged)
        # Substring, deliberately: a read-model surfaces under names built
        # AROUND the aggregate's ("calculateWorkerProductivity",
        # "WorkerProductivityResponse"), so a word-boundary match would reject
        # exactly the contracts this clause exists to accept.
        in_api = agg in openapi.group(1)
        if not in_schema and not in_api:
            errors.append(
                f"aggregate '{agg}' is in neither the Prisma schema nor the "
                "OpenAPI contract")

    # 4c. scenarios imply status codes -> contract must declare them
    declared = set(re.findall(r"['\"]?(\d{3})['\"]?:", openapi.group(1)))
    expected = set(re.findall(r"HTTP (\d{3})", str(scenarios["scenarios"])))
    if missing_codes := expected - declared:
        errors.append(f"scenarios expect status codes not in the contract: {sorted(missing_codes)}")

    return Result(
        ok=not errors,
        summary=f"{len(scenarios['aggregates'])} aggregates, {len(declared)} status codes",
        errors=errors,
    )


def check_contract_stability(contract_a: str, contract_b: str) -> Result:
    """
    Same IR twice -> semantically equivalent contract. A flaky Architect
    poisons every downstream slice, so this runs in the factory's own CI.
    """
    def norm(s: str) -> str:
        s = re.sub(r"//.*|#.*", "", s)
        return re.sub(r"\s+", " ", s).strip().lower()
    same = norm(contract_a) == norm(contract_b)
    return Result(ok=same, summary="stable" if same else "UNSTABLE",
                  errors=[] if same else ["Architect produced divergent contracts "
                                          "for identical input — lower temperature "
                                          "or tighten the conventions skill."])


# -----------------------------------------------------------------------------
# 5. Write-scope audit — never trust a CLI flag to protect the oracle
# -----------------------------------------------------------------------------
def check_write_scope(repo: str, read_only_globs: list[str]) -> Result:
    p = _run(["git", "diff", "--name-only", "HEAD"], repo)
    touched = [f for f in p.stdout.splitlines() if f.strip()]
    patterns = [
        g.replace("**/", "").replace("*", "") for g in read_only_globs
    ]
    violations = [
        f for f in touched
        if any(tok and tok in f for tok in patterns)
        or "__tests__" in f or f.endswith((".spec.ts", ".test.ts"))
    ]
    return Result(
        ok=not violations,
        summary=f"{len(touched)} files changed, all in scope",
        errors=[f"agent modified read-only test files: {violations}"] if violations else [],
    )


# Files that define the HARNESS's contract with the generated app. An agent
# editing one of these breaks the pipeline in a way that looks like a product
# bug, so they are checked separately from the read-only test files above.
#
# Evidence for the split: across four generated slices, agents made four
# infrastructure edits. Three were reasonable (serialising vitest for a shared
# test database, deploying the schema before tests, hiding the dev indicator).
# One — adding `--port 3000` to the web `dev` script — pinned the app to a port
# the factory had not allocated, so `_wait_http` passed against whatever else
# was listening and TWO slices spent their entire diagnose budgets, roughly
# eight hours, testing web pages against a REST API. A blanket ban would have
# blocked the three good edits; silence cost eight hours. So: fail on the few
# files that decide where and how the app runs, report the rest.
CONTRACT_FILES = {
    "dev_script": (re.compile(r"apps/(web|api)/package\.json$"),
                   re.compile(r'^\+\s*"(dev|start)":')),
    "web_server": (re.compile(r"playwright\.config\.ts$"),
                   re.compile(r"^\+.*(webServer|reuseExistingServer|stdout|port)")),
    "env": (re.compile(r"(^|/)\.env"), re.compile(r"^\+")),
}

INFRA_FILES = re.compile(
    r"(package\.json|turbo\.json|.*\.config\.(ts|js|mjs)|Dockerfile|docker-compose.*\.yml)$"
)


def check_infra_contract(repo: str) -> Result:
    """
    Did the agent change how the app is RUN, rather than what it does?

    Returns ok=False only for the contract files above — the ones that decide
    the port the app binds, how Playwright starts it, or what environment it
    reads. Other infrastructure edits are legitimate often enough that they are
    reported (for the Gate C payload) rather than blocked.
    """
    diff = _run(["git", "diff", "-U0", "HEAD"], repo).stdout
    touched = [f for f in _run(["git", "diff", "--name-only", "HEAD"], repo)
               .stdout.splitlines() if f.strip()]

    violations: list[str] = []
    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for name, (path_re, line_re) in CONTRACT_FILES.items():
            if path_re.search(current_file) and line_re.search(line):
                violations.append(f"{current_file}: {line.strip()[:110]}  [{name}]")

    notable = [f for f in touched if INFRA_FILES.search(f)]
    return Result(
        ok=not violations,
        summary=(f"{len(notable)} infrastructure file(s) touched"
                 if notable else "no infrastructure changes"),
        errors=([
            "agent changed how the app is RUN, not what it does — this breaks "
            "the harness silently and surfaces later as product failures:",
            *violations,
            "revert these, then re-run; put behaviour changes in src/ instead.",
        ] if violations else []),
        data={"notable": notable},
    )


# -----------------------------------------------------------------------------
# 6. Prompt portability — the guard that makes project #2 possible
# -----------------------------------------------------------------------------
DOMAIN_NOUNS = [
    "container", "barcode", "warehouse", "packing list", "sku",
    "scan", "worker", "supplier", "pallet",
]


def check_prompt_leak(factory_dir: str = "project_factory",
                      nouns: list[str] | None = None) -> Result:
    """
    Domain facts belong in the IR; prompts hold only CONVENTIONS.

    While debugging project #1 you WILL be tempted to hard-code "containers have
    packing lists" into a prompt to fix a stubborn slice. Do that a few times and
    project #2 breaks mysteriously. Run this in CI and fail the build.
    """
    nouns = [n.lower() for n in (nouns or DOMAIN_NOUNS)]
    errors: list[str] = []
    for path in pathlib.Path(factory_dir).rglob("*.py"):
        if path.name == "harness.py":       # this file legitimately lists them
            continue
        text = path.read_text()
        skip = _doc_and_comment_lines(text)
        for i, line in enumerate(text.splitlines(), 1):
            # Comments and docstrings never reach a model, so they cannot leak
            # domain facts into a prompt. Excluding them also avoids false
            # positives on words that are both domain nouns and infrastructure
            # terms — "container" is a shipping container in the domain AND a
            # Docker container in infra. A check that cries wolf gets ignored,
            # so precision here protects the check's credibility.
            if i in skip:
                continue
            stripped = line.strip()
            low = stripped.lower()
            if any(n in low for n in nouns) and ('"' in line or "'" in line):
                errors.append(f"{path}:{i}: domain noun in prompt/string -> {stripped[:90]}")
    return Result(ok=not errors, summary=f"{len(errors)} leak(s)", errors=errors)


def _doc_and_comment_lines(source: str) -> set[int]:
    """Line numbers occupied by comments or docstrings (module/class/function)."""
    skip: set[int] = set()
    for i, line in enumerate(source.splitlines(), 1):
        if line.strip().startswith("#"):
            skip.add(i)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return skip
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first, "end_lineno", first.lineno) or first.lineno
            skip.update(range(first.lineno, end + 1))
    return skip


if __name__ == "__main__":
    r = check_prompt_leak()
    print(r.summary)
    for e in r.errors:
        print(" -", e)


# -----------------------------------------------------------------------------
# Failure digest — what the diagnostician should actually read
# -----------------------------------------------------------------------------
_TEST_LINE = re.compile(r"›")
_ERROR_LINE = re.compile(r"^\s*(Error|AssertionError|TypeError|SyntaxError)[:\s]")
_COUNT_LINE = re.compile(r"^\s*\d+ (failed|passed|flaky|skipped|did not run)")


def digest_failures(output: str, max_errors: int = 12) -> str:
    """
    Compress raw test output into the distinct failures.

    The suite runs every spec across four browser projects, so one root cause
    arrives four times verbatim; slice 3's diagnostician read 80 copies of a
    single dead web server and charged $12.89 to conclude nothing. Deduplicating
    by error signature keeps every DISTINCT failure — the signal a root-cause
    diagnosis needs — and drops the repetition, which is the bulk.

    Falls back to a plain tail when nothing matches, so an unfamiliar reporter
    format degrades to today's behaviour rather than to an empty prompt.
    """
    lines = output.splitlines()
    counts = [ln.strip() for ln in lines if _COUNT_LINE.match(ln)]
    failing_tests: list[str] = []
    errors: dict[str, int] = {}

    in_failure_list = False
    for i, line in enumerate(lines):
        # Two reliable markers of a FAILING test, so passing progress lines
        # (which also contain ›) never leak in: Playwright's numbered detail
        # headers ("1) [webkit] › …"), and the list printed under the
        # "N failed"/"N flaky" summary.
        if _COUNT_LINE.match(line):
            in_failure_list = bool(re.search(r"(failed|flaky)", line))
            continue
        is_detail_header = bool(re.match(r"^\s*\d+\)\s*\[", line))
        if (is_detail_header or (in_failure_list and _TEST_LINE.search(line))) \
                and len(failing_tests) < 40:
            # Drop the numbering and the browser project, so the same test on
            # chromium and webkit collapses to one entry.
            name = re.sub(r"^\s*(\d+\)\s*)?\[[^\]]+\]\s*›\s*", "", line.strip())
            if name and name not in failing_tests:
                failing_tests.append(name)
        if _ERROR_LINE.match(line):
            # Start AT the error and stop before the next test header: a block
            # that swallows "1) [webkit] › …" differs per browser and would
            # never collapse — which is the entire point of this function.
            block_lines = []
            for ln in lines[i:i + 5]:
                # Stop at the next test header OR the run summary: either one
                # is browser/run specific and would stop identical failures
                # from collapsing.
                if block_lines and (_TEST_LINE.search(ln) or _COUNT_LINE.match(ln)):
                    break
                if ln.strip():
                    block_lines.append(ln.rstrip())
            block = "\n".join(block_lines)
            key = re.sub(r"\b\d+\b", "N", block)[:400]   # ignore line numbers
            errors[key] = errors.get(key, 0) + 1

    if not errors and not failing_tests:
        return output[-6000:]

    parts = []
    if counts:
        parts.append("SUMMARY: " + " | ".join(counts[-4:]))
    if failing_tests:
        parts.append(f"DISTINCT FAILING TESTS ({len(failing_tests)}):\n  - "
                     + "\n  - ".join(failing_tests[:20]))
    if errors:
        ranked = sorted(errors.items(), key=lambda kv: -kv[1])[:max_errors]
        parts.append("DISTINCT ERRORS (x = how many occurrences collapsed):\n"
                     + "\n\n".join(f"[x{n}] {block}" for block, n in ranked))
    return "\n\n".join(parts)
