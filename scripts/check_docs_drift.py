"""Check that the documentation set and the code still describe the same system.

T18's acceptance is "架构、API、数据模型和评测文档与代码一致", and the first time it was checked by
reading the documents it turned up four API resource groups that the contract declared and the
code had never implemented. Reading is what let that go unnoticed for seventeen tasks: a reader
sees a plausible endpoint table, not a diff. This script is the same check expressed as a
comparison, so it can be run instead of remembered.

Ten checks, each of which can only fail for a reason worth a human's attention:

1. **Endpoints** - every endpoint the contract declares resolves to a registered route, and every
   registered route is mentioned somewhere in the contract.
2. **Tables** - every table in `Base.metadata` has a section in `docs/data-model.md`, and every
   table documented but not implemented is listed as planned.
3. **Architecture summary** - `ARCHITECTURE.md` §7 lists every table in `Base.metadata`, and no
   planned table appears there without the marker.
4. **Evaluation targets** - `docs/evaluation.md` §1 lists the variants `TARGET_ORDER` defines, in
   order, and names the promotion target.
5. **Configuration** - every `Settings` field appears in `.env.example`, and `.env.example`
   carries nothing beyond the settings plus the documented Compose-only variables.
6. **Doc index** - every file in `docs/` is linked from `README.md`, and every `docs/` link in
   `README.md` points at a file that exists.
7. **Migrations** - exactly one Alembic head, so `upgrade head` is unambiguous.
8. **Secrets** - no credential-shaped value in the tracked `.env.example`.
9. **Status lines** - the status line in `README.md` and `INTERFACES.md` names the last task
   `ARCHITECTURE.md` §14 marks complete.
10. **Version** - `__version__` and `pyproject.toml` agree.

Exit code is 0 only when every check passes. The payload goes to stdout as one JSON object on the
last line, which is the convention `scripts/verify_*.ps1` reads.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Paths FastAPI registers on its own behalf. They are real endpoints and they are deliberately
#: not in the contract: the contract describes the product's API surface, not the framework's.
#: `/` is *not* in this set - the application owns it as a browser-preview redirect and the
#: contract documents it.
FRAMEWORK_PATHS = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)

#: The `.env.example` entries that exist for Compose rather than for `Settings`. `Settings` uses
#: `extra="forbid"`, so these are exactly the keys a developer must strip when copying the file
#: into `.env`; listing them here is what turns "somewhere between zero and five extras" into a
#: number that has to stay right.
COMPOSE_ONLY_ENV = frozenset(
    {
        "MYSQL_DATABASE",
        "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_USER",
        "PIP_INDEX_URL",
    }
)

#: Marker in `docs/data-model.md` introducing the tables that are designed but not implemented.
PLANNED_TABLE_MARKER = "计划内、尚未实现"

#: Root documents whose status line has to name the last task the architecture document marks
#: complete. `REQUIREMENTS.md` is deliberately absent: its status line records whether the user
#: has confirmed the requirements baseline, which no task list can derive.
STATUS_DOCUMENTS = {
    "README.md": "> 项目状态：",
    "INTERFACES.md": "> 文档状态：",
}

#: The task list entries in `ARCHITECTURE.md` §14, e.g. `- [x] T16 数据回填与端到端演示 — ...`.
TASK_LINE = re.compile(r"^- \[([ x])\] T(\d+) ", re.MULTILINE)

#: The first column of the variant table in `docs/evaluation.md` §1, e.g.
#: `| \`agent_no_tools\` | 只使用一次候选数据… | … |`.
VARIANT_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)

BACKTICKED = re.compile(r"`([^`]+)`")
METHOD_PATH = re.compile(r"\b([A-Z]{3,7})\s+(/[A-Za-z0-9{}/_.\-,]*)")
PATH_PARAM = re.compile(r"\{[^}]*\}")
BRACE_CHOICE = re.compile(r"\{([^{}]*,[^{}]*)\}")

#: A name that *ends* in one of these is a credential; one that merely contains them is not.
#: `AGENT_MAX_TOKENS` is a budget, `GITHUB_TOKEN` is a secret, and the difference is the suffix.
SECRET_NAME = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY)$")
PLACEHOLDER = re.compile(r"(replace-with|change-me|example|placeholder|xxx|^$)")


@dataclass
class Check:
    """One named comparison plus whatever it found."""

    name: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether this check found nothing to report."""
        return not self.failures


def _prose(document: str) -> str:
    """Return the document with fenced code blocks removed.

    Backticks inside a fence do not open inline code, so scanning the raw text pairs them wrongly
    and shifts every token after the first fence: the JSON examples' prose then reads like
    documented endpoints, and the real endpoint headings stop being found at all. Stripping the
    fences first is what keeps the pairing honest.
    """
    kept: list[str] = []
    in_fence = False
    for line in document.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


def _normalize(path: str) -> str:
    """Replace path parameters so the contract's names may differ from the router's.

    They legitimately do differ: the contract documents
    `/agent/runs/{agent_run_id}/steps` while the route declares `/agent/runs/{run_id}/steps`.
    The parameter's name is not part of the URL a client sends, so comparing them literally would
    report a difference that no caller can observe.
    """
    trimmed = path.rstrip("/") or "/"
    return PATH_PARAM.sub("{}", trimmed)


def _expand_brace_choices(path: str) -> list[str]:
    """Expand the contract's shorthand, e.g. `/reports/{id}/{verify,review}`.

    The documents use this shorthand in several tables and it is worth honouring rather than
    avoiding: writing each combination out would make the tables unreadable, and a checker that
    cannot read them would just be ignoring part of the contract.

    The braces are removed by the expansion, not kept: `{verify,review}` means the two paths
    `/verify` and `/review`, and leaving them on would make both normalize to `{}` and collapse
    into a single endpoint that matches nothing.
    """
    match = BRACE_CHOICE.search(path)
    if match is None:
        return [path]
    expanded: list[str] = []
    for alternative in match.group(1).split(","):
        replaced = f"{path[: match.start()]}{alternative}{path[match.end() :]}"
        expanded.extend(_expand_brace_choices(replaced))
    return expanded


def _documented_endpoints(contract: str) -> set[tuple[str, str]]:
    """Return every `(method, normalized path)` the contract names, in any position.

    Both shapes count: a heading (`### `GET /reports``) and an inline mention inside a table
    (`| `POST /reports/{id}/push` |`). Relative fragments such as `/{id}/verify` are kept as they
    are written and matched by suffix later, because the contract names the resource in one place
    and then refers to it by the short form.
    """
    found: set[tuple[str, str]] = set()
    for token in BACKTICKED.findall(_prose(contract)):
        for method, path in METHOD_PATH.findall(token):
            for expanded in _expand_brace_choices(path):
                found.add((method, _normalize(expanded)))
    return found


def _declared_endpoints(contract: str) -> set[tuple[str, str]]:
    """Return the endpoints the contract *asserts exist*.

    Only two positions assert that: a heading, and a table row. Prose does not. The distinction
    is load-bearing here because the contract also names endpoints in order to say they were
    removed - `POST /topics/{id}/run` and `POST /agent/runs` are both written down by the
    paragraph that explains why they are not offered. Counting a denial as a promise would make
    the one document that gets it right the one the checker refuses to accept.
    """
    declared = "\n".join(
        line for line in _prose(contract).splitlines() if line.lstrip().startswith(("#", "|"))
    )
    return _documented_endpoints(declared)


def _documented_paths(contract: str) -> set[str]:
    """Return every normalized path the contract names, in any position."""
    paths: set[str] = set()
    for _, path in _documented_endpoints(contract):
        paths.add(path)
    for token in BACKTICKED.findall(_prose(contract)):
        for candidate in token.split():
            if candidate.startswith("/"):
                for expanded in _expand_brace_choices(candidate):
                    paths.add(_normalize(expanded))
    return paths


#: Attributes that hold the routes an `app.routes` node delegates to. `include_router` behaves
#: differently across FastAPI versions: on some it copies the child routes into the parent, and
#: from 0.143 it keeps them behind an `_IncludedRouter` node that exposes the router it was given
#: as `original_router`. Both shapes have to be walked, or the same source tree yields 41 routes
#: on one interpreter and none on another.
ROUTE_CHILD_ATTRIBUTES = ("routes", "original_router")


def _registered_routes(routes: object) -> list[tuple[str, set[str]]]:
    """Walk the application's route tree and return `(path, methods)` for every real route.

    The walk is recursive rather than one flat pass over `app.routes`, because `include_router`
    does not always flatten. The host interpreter and the container run different FastAPI
    versions and disagree: a flat loop found all 41 routes locally and none at all inside the
    image. A checker that silently discovers nothing and then reports every documented endpoint as
    missing is worse than no checker, so the caller also refuses to believe an empty result.
    """
    found: list[tuple[str, set[str]]] = []
    stack = list(getattr(routes, "__iter__", lambda: [])())
    while stack:
        node = stack.pop()
        path = getattr(node, "path", None)
        methods = getattr(node, "methods", None)
        if path and methods:
            found.append((str(path), {str(method).upper() for method in methods}))
        for attribute in ROUTE_CHILD_ATTRIBUTES:
            child = getattr(node, attribute, None)
            if child is None:
                continue
            children = getattr(child, "routes", None)
            if children:
                stack.extend(children)
    return found


def check_endpoints() -> Check:
    """Compare the contract's endpoint headings against the registered routes."""
    check = Check("endpoints")
    from open_source_intelligence.api.app import create_app

    app = create_app()
    registered: dict[str, set[str]] = {}
    for path, methods in _registered_routes(app.routes):
        if path in FRAMEWORK_PATHS or path.startswith("/ui/static"):
            continue
        registered.setdefault(_normalize(path), set()).update(methods - {"HEAD", "OPTIONS"})

    # An empty result means the walk failed, not that the application serves nothing. Saying so
    # out loud is what keeps a broken walk from looking like a clean contract.
    if not registered:
        check.failures.append("no routes were discovered - the route walk found nothing to compare")
        return check

    contract = (PROJECT_ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")
    documented = _documented_paths(contract)

    # Direction one: what the document promises must exist. This is the check that caught the
    # four resource groups the contract declared and the code had never implemented.
    #
    # Every declared endpoint is reported, with no length heuristic applied first. An earlier
    # version only complained about paths with two or more slashes, on the theory that a shorter
    # path was a fragment rather than a promise - and that theory silently excused exactly the
    # shape of the drift being hunted: `GET /sources` and `GET /topics` have one slash each.
    # Restricting the input to headings and table rows already separates a promise from a
    # reference, so the path's length no longer has to carry that meaning.
    for method, path in sorted(_declared_endpoints(contract)):
        candidates = [route for route in registered if route == path or route.endswith(path)]
        if not candidates:
            check.failures.append(f"documented but not registered: {method} {path}")
            continue
        if not any(method in registered[route] for route in candidates):
            check.failures.append(f"documented method not registered: {method} {path}")

    # Direction two: every route is mentioned somewhere. A route nobody wrote down is a route
    # nobody reviews.
    for route, methods in sorted(registered.items()):
        if any(route == path or route.endswith(path) for path in documented):
            continue
        check.failures.append(f"registered but not documented: {','.join(sorted(methods))} {route}")

    check.notes.append(f"registered paths: {len(registered)}")
    return check


def check_tables() -> Check:
    """Compare `Base.metadata` against the table sections in `docs/data-model.md`."""
    check = Check("tables")
    import open_source_intelligence.persistence.models  # noqa: F401
    from open_source_intelligence.persistence.base import Base

    document = (PROJECT_ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### `([a-z_]+)`", document, re.MULTILINE))
    implemented = set(Base.metadata.tables)
    planned = _planned_tables(document)

    for missing in sorted(implemented - documented):
        check.failures.append(f"implemented but undocumented: {missing}")
    for extra in sorted(documented - implemented):
        if extra in planned:
            check.notes.append(f"planned, not implemented: {extra}")
            continue
        check.failures.append(f"documented but not implemented: {extra}")

    check.notes.append(f"tables: {len(implemented)} implemented, {len(documented)} documented")
    return check


def _backticked_names(text: str) -> set[str]:
    """Return the backticked tokens that look like table names.

    `[a-z_]+` is what separates a table from everything else a document puts in backticks: a
    column (`next_page`), a module path (`core/logging.py`) and a requirement id (`FR-D-05`) all
    fail the test, and no table name is uppercase or dotted.
    """
    return {
        token for token in BACKTICKED.findall(text) if re.fullmatch(r"[a-z_]+", token) is not None
    }


def _planned_tables(data_model: str) -> set[str]:
    """Return the tables `docs/data-model.md` declares as designed-but-not-implemented.

    Requiring the document to say so out loud is the point: `feedback` was listed like any other
    table for three tasks, and nothing about that listing let a reader tell.
    """
    planned: set[str] = set()
    for line in data_model.splitlines():
        if PLANNED_TABLE_MARKER in line:
            planned.update(re.findall(r"`([a-z_]+)`", line))
    return planned


def _section_lines(document: str, heading_prefix: str) -> list[str]:
    """Return the body lines of the section whose heading starts with `heading_prefix`.

    The section ends at the next heading of the same or shallower level. Without that boundary a
    sub-table's rows would leak into the comparison and the check would pass on the strength of a
    heading it never meant to include.
    """
    lines = document.splitlines()
    start = next(
        (index + 1 for index, line in enumerate(lines) if line.startswith(heading_prefix)), None
    )
    if start is None:
        return []
    level = len(heading_prefix) - len(heading_prefix.lstrip("#"))
    for index in range(start, len(lines)):
        candidate = lines[index]
        if candidate.startswith("#"):
            depth = len(candidate) - len(candidate.lstrip("#"))
            if depth <= level:
                return lines[start:index]
    return lines[start:]


def check_architecture_summary() -> Check:
    """Compare the architecture document's summary table against `Base.metadata`.

    `docs/data-model.md` is the field-level reference, but `ARCHITECTURE.md` §7 is the table a
    newcomer reads first, and it is the one that listed `feedback` - a table with no model and no
    migration - next to the real ones. A summary may group tables; it may not invent them or lose
    them, so the check is the same comparison at a coarser grain.
    """
    check = Check("architecture-summary")
    import open_source_intelligence.persistence.models  # noqa: F401
    from open_source_intelligence.persistence.base import Base

    implemented = set(Base.metadata.tables)
    data_model = (PROJECT_ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")
    planned = _planned_tables(data_model)

    summary = _section_lines(
        (PROJECT_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8"), "## 7. "
    )
    listed = _backticked_names("\n".join(summary))

    for missing in sorted(implemented - listed):
        check.failures.append(
            f"implemented but absent from the ARCHITECTURE.md §7 summary: {missing}"
        )

    # A planned table has to carry the marker everywhere it is presented as a table. The
    # `data-model.md` section has its own heading and is where the marker is defined, so only the
    # documents that *refer* to that section are scanned here.
    for name in ("ARCHITECTURE.md", "INTERFACES.md"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if PLANNED_TABLE_MARKER in line:
                continue
            for table in sorted(_backticked_names(line) & planned):
                check.failures.append(
                    f"{name}:{number} lists planned table `{table}` without the marker"
                )

    check.notes.append(f"§7 lists {len(listed & implemented)} of {len(implemented)} tables")
    return check


def check_status_lines() -> Check:
    """Check that the root documents' status lines name the last completed task.

    The status line is the first thing a reader sees and the last thing anyone updates. `README.md`
    announced "T1-T12 已完成，进入 T13" through four further tasks, because nothing compared it to
    the task list that was being ticked off. Deriving the answer from `ARCHITECTURE.md` §14 and
    requiring the headers to match is the smallest comparison that catches it, and it needs no
    judgement about how stale is too stale.
    """
    check = Check("status-lines")
    architecture = (PROJECT_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    completed = [
        int(match.group(2)) for match in TASK_LINE.finditer(architecture) if match.group(1) == "x"
    ]
    if not completed:
        check.failures.append("ARCHITECTURE.md marks no task complete, so there is nothing to name")
        return check

    last = max(completed)
    for name, prefix in sorted(STATUS_DOCUMENTS.items()):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        line = next((item for item in text.splitlines() if item.startswith(prefix)), None)
        if line is None:
            check.failures.append(f"{name} has no status line beginning with {prefix!r}")
        elif f"T{last}" not in line:
            check.failures.append(
                f"{name} status line does not name T{last}, the last task marked complete"
            )

    check.notes.append(f"last completed task: T{last}")
    return check


def check_evaluation_targets() -> Check:
    """Compare the evaluation variants in `docs/evaluation.md` §1 against `TARGET_ORDER`.

    Adding a variant is a change that has to land in four places - `TARGET_ORDER`,
    `EVAL_TARGET_TYPES`, `ABLATION_PROFILES` and the migration's CHECK constraint - and the
    document is a fifth. Only the document was unguarded, which is the same asymmetry that let
    `feedback` be listed as a real table: the code cannot drift from itself, so the place nobody
    compares is the place that drifts. §1 claims its list and order are identical to
    `TARGET_ORDER`, so the comparison is a literal one.
    """
    check = Check("evaluation-targets")
    from open_source_intelligence.agent.profile import ABLATION_PROFILES
    from open_source_intelligence.evaluation.runner import PROMOTION_TARGETS
    from open_source_intelligence.evaluation.targets import TARGET_ORDER

    document = (PROJECT_ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
    variants = _section_lines(document, "## 1. ")
    documented = VARIANT_ROW.findall("\n".join(variants))

    if documented != list(TARGET_ORDER):
        check.failures.append(
            f"docs/evaluation.md §1 lists {documented}, TARGET_ORDER is {list(TARGET_ORDER)}"
        )
    for name in PROMOTION_TARGETS:
        if f"`{name}`" not in document:
            check.failures.append(f"promotion target {name} is not named in docs/evaluation.md")
    for name in sorted(ABLATION_PROFILES):
        if name not in TARGET_ORDER:
            check.failures.append(f"ablation profile {name} is not one of TARGET_ORDER")

    check.notes.append(f"variants: {len(documented)}; promotion: {','.join(PROMOTION_TARGETS)}")
    return check


def check_configuration() -> Check:
    """Compare `Settings` against `.env.example`."""
    check = Check("configuration")
    from open_source_intelligence.core.config import Settings

    env_path = PROJECT_ROOT / ".env.example"
    declared = {
        line.split("=", 1)[0].strip()
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line.strip())
    }
    expected = {name.upper() for name in Settings.model_fields}

    for missing in sorted(expected - declared):
        check.failures.append(f"setting missing from .env.example: {missing}")
    for extra in sorted(declared - expected):
        if extra in COMPOSE_ONLY_ENV:
            continue
        check.failures.append(
            f"unexpected key in .env.example: {extra} (add it to COMPOSE_ONLY_ENV if intentional)"
        )

    check.notes.append(f"settings: {len(expected)}, keys: {len(declared)}")
    return check


def check_doc_index() -> Check:
    """Check that `README.md` links every document and only existing ones."""
    check = Check("doc-index")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\((docs/[A-Za-z0-9._-]+\.md|docs/adr)\)", readme))

    for path in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        if f"docs/{path.name}" not in linked:
            check.failures.append(f"document not linked from README: docs/{path.name}")
    for link in sorted(linked):
        if not (PROJECT_ROOT / link).exists():
            check.failures.append(f"README links a missing path: {link}")

    check.notes.append(f"documents: {len(list((PROJECT_ROOT / 'docs').glob('*.md')))}")
    return check


def check_migrations() -> Check:
    """Check that `upgrade head` resolves to a single revision."""
    check = Check("migrations")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    if len(heads) != 1:
        check.failures.append(f"expected a single head, found {len(heads)}: {sorted(heads)}")
    check.notes.append(f"head: {heads[0] if heads else 'none'}")
    return check


def check_secrets() -> Check:
    """Check that the tracked environment template carries no real credential."""
    check = Check("secrets")
    env_path = PROJECT_ROOT / ".env.example"
    for number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not SECRET_NAME.search(name):
            continue
        if PLACEHOLDER.search(value.strip()):
            continue
        check.failures.append(f"credential-shaped value at .env.example:{number} ({name})")

    check.notes.append(".env.example is tracked; only placeholders may appear in it")
    return check


def check_version() -> Check:
    """Check that the package version and the project metadata agree."""
    check = Check("version")
    from open_source_intelligence import __version__

    declared = ""
    for line in (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            declared = line.split("=", 1)[1].strip().strip('"')
            break
    if __version__ != declared:
        check.failures.append(f"version mismatch: __version__={__version__} pyproject={declared}")
    check.notes.append(f"version: {__version__}")
    return check


CHECKS = (
    check_endpoints,
    check_tables,
    check_architecture_summary,
    check_evaluation_targets,
    check_configuration,
    check_doc_index,
    check_migrations,
    check_secrets,
    check_status_lines,
    check_version,
)


def main(argv: list[str] | None = None) -> int:
    """Run every check, print one JSON payload, and exit non-zero on any drift."""
    parser = argparse.ArgumentParser(description="Check documentation and code consistency.")
    parser.add_argument(
        "--only",
        action="append",
        choices=[fn.__name__.removeprefix("check_").replace("_", "-") for fn in CHECKS],
        help="run only the named check (repeatable)",
    )
    arguments = parser.parse_args(argv)

    selected = [
        fn
        for fn in CHECKS
        if not arguments.only
        or fn.__name__.removeprefix("check_").replace("_", "-") in arguments.only
    ]
    results = [fn() for fn in selected]
    payload: dict[str, Any] = {
        "checks": {
            result.name: {
                "ok": result.ok,
                "failures": result.failures,
                "notes": result.notes,
            }
            for result in results
        },
        "failures": sum(len(result.failures) for result in results),
        "passed": all(result.ok for result in results),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    for result in results:
        if not result.ok:
            for failure in result.failures:
                print(f"DRIFT [{result.name}] {failure}", file=sys.stderr)
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
