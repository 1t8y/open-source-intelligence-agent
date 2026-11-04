"""守护「同一份取值被写在多处」的那些副本。

这里的每个用例钉住一处别处不会去比对的重复。它们都是纯 Python：
不碰数据库、不联网，除了仓库本身也不读文件系统，
因此可以和打包检查放在同一轮里跑。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DEFAULT = re.compile(r"^\s{2,}([A-Z][A-Z0-9_]*):\s*\$\{[A-Z0-9_]+:-([^}]*)\}", re.MULTILINE)


def _render(value: Any) -> str:
    """按环境变量文件的写法渲染一个 Settings 默认值。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


#: 容器里的取值刻意与进程默认值不同的配置项。
#: `API_HOST` 在容器里必须绑 `0.0.0.0`，否则映射出去的端口指向空处；
#: 容器又输出 JSON 日志，好让日志驱动解析。其余项必须一致：
#: 兜底值就是一个默认值，同一个决定；这两处已经漂移过两次
#: （`AGENT_MAX_TOOL_CALLS` 的默认值改成 20 后仍停在 10；
#: `AGENT_MAX_TOKENS` 在 T24 提到 30000 后仍停在 12000），
#: 而每次都是只有「没设该变量的部署」才会显形。
DEPLOYMENT_OVERRIDES = {
    "API_HOST": "a container must bind 0.0.0.0 to be reachable through its published port",
    "LOG_FORMAT": "containers emit JSON so the log driver can parse it",
}


def test_compose_defaults_match_the_settings_defaults() -> None:
    """证明配置字段与 Compose 兜底值是同一个决定的两次书写。

    它们确实漂移过：`AGENT_MAX_TOOL_CALLS` 在 `Settings` 与 `.env.example` 里
    都提到了 20，Compose 兜底值却仍写着 10，于是没有提供该变量的部署
    拿到了旧预算 —— 正是「证据下限」那项工作要避免的「刚好够用」值。
    此前没人比对这两处，因为兜底值只在变量缺席时才起作用。
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
    # 过期的白名单条目会让豁免范围悄悄缩成空，所以要求每一条
    # 都仍然指向 compose 文件里真实定义的变量。
    assert overrides_seen == set(DEPLOYMENT_OVERRIDES), (
        "deployment overrides no longer present in compose: "
        f"{sorted(set(DEPLOYMENT_OVERRIDES) - overrides_seen)}"
    )
    assert not mismatches, "compose fallbacks disagree with Settings:\n" + "\n".join(mismatches)


def test_the_env_example_values_match_the_settings_defaults() -> None:
    """证明 `.env.example` 这第三份默认值副本的取值也与配置一致。

    `check_docs_drift.py` 只断言每个 `Settings` 字段在 `.env.example` 里
    *出现过*；
    一个值被落下的键照样能通过那项检查，同时却把一份与文档默认值
    不同的预算交给新的部署。
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
    """证明 `AUDIT_RESULTS` 与路由的 `Literal` 枚举的是同样的三个值。

    表的 CHECK 由 `AUDIT_RESULTS` 生成，因此不会漂移。路由却从自己的
    `Literal` 生成 OpenAPI 枚举，那是第二份手写副本：只往一边加新结果，
    要么在 API 边界拒掉一个本可入库的值，要么对外声明一个表会拒绝的值。
    """
    from typing import get_args

    from open_source_intelligence.api.routes.audit import AuditResult
    from open_source_intelligence.persistence.models.audit import AUDIT_RESULTS

    assert set(get_args(AuditResult)) == set(AUDIT_RESULTS)


def test_the_run_type_vocabulary_matches_its_check_constraint() -> None:
    """证明 `RUN_TYPES` 与 `pipeline_runs.run_type`
    上的 CHECK 列出的是同一批值。

    两处都是手写的：模型在约束里写一遍列表，流水线模块再以元组写一遍。
    只往其中一边加新运行类型，新阶段第一次跑起来时就会被数据库拒绝 ——
    故障落在生产，而不是 CI。
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
