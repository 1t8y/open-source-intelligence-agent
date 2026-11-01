"""Guards for values that are written down in more than one place.

Each test here pins one duplication that nothing else compares. They are all pure-Python: no
database, no network, no filesystem beyond the repository itself, so they can run in the same
breath as the packaging checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DEFAULT = re.compile(r"^\s{2,}([A-Z][A-Z0-9_]*):\s*\$\{[A-Z0-9_]+:-([^}]*)\}", re.MULTILINE)


def _render(value: Any) -> str:
    """Render a Settings default the way an env file would spell it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


#: Settings whose container value is deliberately different from the process default.
#: `API_HOST` must be 0.0.0.0 inside a container or the published port maps to nothing, and
#: containers emit JSON logs so the driver can parse them. Everything else must agree: a fallback
#: is the same decision as a default, and the two drifted twice (`AGENT_MAX_TOOL_CALLS` stayed at
#: 10 after the default became 20; `AGENT_MAX_TOKENS` stayed at 12000 after T24 raised it to
#: 30000), each time only visible to a deployment that left the variable unset.
DEPLOYMENT_OVERRIDES = {
    "API_HOST": "a container must bind 0.0.0.0 to be reachable through its published port",
    "LOG_FORMAT": "containers emit JSON so the log driver can parse it",
}


def test_compose_defaults_match_the_settings_defaults() -> None:
    """A settings field and its Compose fallback are the same decision written twice.

    They did drift: `AGENT_MAX_TOOL_CALLS` was raised to 20 in `Settings` and `.env.example` but
    the Compose fallback still said 10, so a deployment that did not supply the variable got the
    old budget - the "exactly enough" value the evidence-floor work exists to avoid. Nothing
    compared them, because the fallback only matters when the variable is absent.
    """
    from open_source_intelligence.core.config import Settings

    compose = (PROJECT_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    fields = {
        name.upper(): field
        for name, field in Settings.model_fields.items()
        if not field.is_required() and hasattr(field, "default")
    }

    mismatches: list[str] = []
    compared = 0
    overrides_seen: set[str] = set()
    for variable, raw in COMPOSE_DEFAULT.findall(compose):
        field = fields.get(variable)
        if field is None:
            continue
        expected: Any = field.default
        if expected is None or hasattr(expected, "get_secret_value"):
            continue
        if variable in DEPLOYMENT_OVERRIDES:
            overrides_seen.add(variable)
            continue
        compared += 1
        actual: Any = raw
        if isinstance(expected, bool):
            actual = raw.strip().lower() in {"true", "1", "yes", "on"}
        elif isinstance(expected, int) and not isinstance(expected, bool):
            actual = int(raw)
        elif isinstance(expected, float):
            actual = float(raw)
        elif not isinstance(expected, str):
            actual, expected = str(raw), str(expected)
        if actual != expected:
            mismatches.append(f"{variable}: compose {raw!r} != Settings {expected!r}")

    assert compared > 20, f"only {compared} settings were compared - the parse likely broke"
    # A stale allowlist entry silently widens the exemption to nothing, so require each one to
    # still name a variable the compose file actually defines.
    assert overrides_seen == set(DEPLOYMENT_OVERRIDES), (
        "deployment overrides no longer present in compose: "
        f"{sorted(set(DEPLOYMENT_OVERRIDES) - overrides_seen)}"
    )
    assert not mismatches, "compose fallbacks disagree with Settings:\n" + "\n".join(mismatches)


def test_the_env_example_values_match_the_settings_defaults() -> None:
    """`.env.example` is the third copy of the same defaults, and nothing compared its values.

    `check_docs_drift.py` only asserts that every `Settings` field *appears* in `.env.example`;
    a key whose value had been left behind would still pass that check while handing a new
    deployment a different budget than the documented default.
    """
    from open_source_intelligence.core.config import Settings

    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    written = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.MULTILINE))
    fields = {
        name.upper(): field
        for name, field in Settings.model_fields.items()
        if not field.is_required()
    }

    mismatches: list[str] = []
    compared = 0
    for variable, raw in written.items():
        field = fields.get(variable)
        if field is None:
            continue
        expected: Any = field.default
        if expected is None or hasattr(expected, "get_secret_value"):
            continue
        if variable in DEPLOYMENT_OVERRIDES:
            continue
        compared += 1
        rendered = _render(expected)
        if raw.strip() != rendered:
            mismatches.append(f"{variable}: .env.example {raw.strip()!r} != Settings {rendered!r}")

    assert compared > 20, f"only {compared} settings were compared - the parse likely broke"
    assert not mismatches, ".env.example disagrees with Settings:\n" + "\n".join(mismatches)


def test_the_audit_result_vocabulary_is_written_down_once() -> None:
    """`AUDIT_RESULTS` and the route's `Literal` enumerate the same three values.

    The table's CHECK is generated from `AUDIT_RESULTS`, so it cannot drift. The route builds its
    OpenAPI enum from its own `Literal`, which is a second hand-maintained copy: adding a result
    to one and not the other either rejects a storable value at the API boundary or documents a
    value the table refuses.
    """
    from typing import get_args

    from open_source_intelligence.api.routes.audit import AuditResult
    from open_source_intelligence.persistence.models.audit import AUDIT_RESULTS

    assert set(get_args(AuditResult)) == set(AUDIT_RESULTS)


def test_the_run_type_vocabulary_matches_its_check_constraint() -> None:
    """`RUN_TYPES` and the CHECK on `pipeline_runs.run_type` must list the same values.

    Both are hand-written: the model spells the list inside the constraint, the pipeline module
    spells it again as a tuple. A new run type added to only one of them is rejected by the
    database at the moment the new stage first runs - the failure lands in production, not in CI.
    """
    from open_source_intelligence.persistence.models.intelligence import PipelineRun
    from open_source_intelligence.pipeline.runs import RUN_TYPES

    constraint = next(
        item
        for item in PipelineRun.__table__.constraints
        if (getattr(item, "name", "") or "").endswith("run_type_valid")
    )
    stored = set(re.findall(r"'([^']*)'", str(constraint.sqltext)))

    assert stored == set(RUN_TYPES), (
        f"only in the constraint: {sorted(stored - set(RUN_TYPES))}; "
        f"only in RUN_TYPES: {sorted(set(RUN_TYPES) - stored)}"
    )
