"""检查文档集与代码是否仍在描述同一个系统。

T18 的验收标准是「架构、API、数据模型和评测文档与代码一致」，而第一次靠读文档来
检查时，就翻出了契约声明过、代码从未实现的四组 API 资源。正因为「读」才让这件事
躲过十七个任务：读者看到的是一张看似合理的端点表，而不是一份差异。本脚本把同一个
检查表达成一次比较，于是它可以被「跑」，而不必靠「记得」。

十项检查，每一项失败都只会因为一个值得人看一眼的理由：

1. **端点** —— 契约声明的每个端点都能解析到一条已注册的路由，每条已注册的路由都在
   契约的某处被提到。
2. **数据表** —— `Base.metadata` 里的每张表在 `docs/data-model.md` 都有一节；文档里
   写了、代码却没实现的每张表都被标为计划内。
3. **架构摘要** —— `ARCHITECTURE.md` §7 列出 `Base.metadata` 里的每张表；任何计划内
   的表出现在那里都必须带标记。
4. **评测变体** —— `docs/evaluation.md` §1 按顺序列出 `TARGET_ORDER` 定义的变体，并
   点名晋级目标。
5. **配置** —— `Settings` 的每个字段都出现在 `.env.example`；而 `.env.example` 除了
   这些配置项与文档里那批 Compose 专用变量之外，不多带任何东西。
6. **文档索引** —— `docs/` 下的每个文件都被 `README.md` 链接到，而 `README.md` 里
   每个 `docs/` 链接都指向一个存在的文件。
7. **迁移** —— 恰好一个 Alembic head，这样 `upgrade head` 才没有歧义。
8. **密钥** —— 受版本控制的 `.env.example` 里没有任何凭据形状的值。
9. **状态行** —— `README.md` 与 `INTERFACES.md` 的状态行互相一致，都点名
   最近完成的阶段。进度事实在过程层（本地文件与 `git log`），不再由架构文档推导。
10. **版本** —— `__version__` 与 `pyproject.toml` 一致。

只有每项检查都通过*并且*每项检查都跑过时，退出码才是 0：`--only` 会收窄输入，所以
结果会报出跑了多少项比较、那是不是全部，而一次部分运行以非零退出。结果以单个 JSON
对象打印在 stdout 的最后一行，这是 `scripts/verify_*.ps1` 读取的约定。
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

#: FastAPI 为自己注册的路径。它们是真实端点，但刻意不进契约：契约描述的是产品的 API
#: 面，不是框架的面。`/` *不*在这个集合里 —— 应用把它当作浏览器预览的跳转自己占着，
#: 契约也记录过它。
FRAMEWORK_PATHS = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)

#: `.env.example` 里为 Compose 而不是为 `Settings` 存在的条目。`Settings` 用的是
#: `extra="forbid"`，所以这些恰好就是开发者把它拷成 `.env` 时必须剥掉的键；把它们列在
#: 这里，才把「大约零到五个额外键」变成一必须一直算对的数。
COMPOSE_ONLY_ENV = frozenset(
    {
        "MYSQL_DATABASE",
        "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_USER",
        "PIP_INDEX_URL",
    }
)

#: `docs/data-model.md` 里引出「已设计、尚未实现」那批表的标记。
PLANNED_TABLE_MARKER = "计划内、尚未实现"

#: 根目录文档，它们的状态行是同一份对外公告的两处副本，必须点名同一个最近完成阶段。
#: `REQUIREMENTS.md` 刻意不在其中：它的状态行记录的是用户有没有确认需求基线。
STATUS_DOCUMENTS = {
    "README.md": "> 项目状态：",
    "INTERFACES.md": "> 文档状态：",
}

#: 状态行里的阶段编号，例如 `T1-T32 已完成`。进度事实在过程层，比较器只核对
#: 两份公告是否点名同一阶段，不从架构文档推导编号。
STATUS_TASK = re.compile(r"T(\d+)")

#: `docs/evaluation.md` §1 里变体表的第一列，例如
#: `| \`agent_no_tools\` | 只使用一次候选数据… | … |`。
VARIANT_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)

BACKTICKED = re.compile(r"`([^`]+)`")
METHOD_PATH = re.compile(r"\b([A-Z]{3,7})\s+(/[A-Za-z0-9{}/_.\-,]*)")
PATH_PARAM = re.compile(r"\{[^}]*\}")
BRACE_CHOICE = re.compile(r"\{([^{}]*,[^{}]*)\}")

#: 以这些之一*结尾*的名字是凭据；只是包含它们的不算。`AGENT_MAX_TOKENS` 是预算，
#: `GITHUB_TOKEN` 是密钥，区别就在后缀。
SECRET_NAME = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY)$")
PLACEHOLDER = re.compile(r"(replace-with|change-me|example|placeholder|xxx|^$)")


@dataclass
class Check:
    """一项具名比较，外加它查到的所有东西。"""

    name: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """返回这项检查有没有查出要报的东西。"""
        return not self.failures


def _prose(document: str) -> str:
    """返回剔掉围栏代码块之后的文档。

    围栏内的反引号并不会开启行内代码，所以直接扫原文会把它们配错对，并把第一个围栏
    之后的每个 token 都挪了位：JSON 示例里的说明文字于是读起来像被记录的端点，而真正的
    端点标题反而一个也找不到了。先把围栏剥掉，配对才是诚实的。
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
    """替换掉路径参数，好让契约里的参数名可以与路由里的不同。

    它们本来就允许不同：契约写的是 `/agent/runs/{agent_run_id}/steps`，而路由声明的是
    `/agent/runs/{run_id}/steps`。参数名不是客户端发出的 URL 的一部分，所以按字面比较
    会报出一处调用方根本观察不到的差异。
    """
    trimmed = path.rstrip("/") or "/"
    return PATH_PARAM.sub("{}", trimmed)


def _expand_brace_choices(path: str) -> list[str]:
    """展开契约里的简写，例如 `/reports/{id}/{verify,review}`。

    几处表格都用了这种简写，值得照它读懂而不是绕开：把每种组合都写开会让表格没法读，
    而一个读不懂它们的检查器，无非是在忽略契约的一部分。

    展开时会删掉花括号，而不是留着：`{verify,review}` 的意思是那两条路径 `/verify` 与
    `/review`；留着它们会让两者都归一化成 `{}`，塌成一个谁也匹配不上的端点。
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
    """返回契约点名过的每一组 `(方法, 归一化路径)`，位置不限。

    两种形态都算：标题（`### `GET /reports``）与表格里的行内提及
    （`| `POST /reports/{id}/push` |`）。像 `/{id}/verify` 这样的相对片段按它写的样子
    留下，之后再按后缀匹配，因为契约在一处命名了资源，随后就用短形式指代它。
    """
    found: set[tuple[str, str]] = set()
    for token in BACKTICKED.findall(_prose(contract)):
        for method, path in METHOD_PATH.findall(token):
            for expanded in _expand_brace_choices(path):
                found.add((method, _normalize(expanded)))
    return found


def _declared_endpoints(contract: str) -> set[tuple[str, str]]:
    """返回契约*断言其存在*的那些端点。

    只有两个位置构成断言：标题与表格行。正文不算。这个区别在这里是承重的，因为契约
    也会点名端点，只是为了说明它们已被移除 —— `POST /topics/{id}/run` 与
    `POST /agent/runs` 都写在「为什么不再提供它们」的那一段里。把一次否认当成一句
    承诺，就会让唯一一份写对了的文档，变成检查器拒绝接受的那一份。
    """
    declared = "\n".join(
        line for line in _prose(contract).splitlines() if line.lstrip().startswith(("#", "|"))
    )
    return _documented_endpoints(declared)


def _documented_paths(contract: str) -> set[str]:
    """返回契约点名过的每一个归一化路径，位置不限。"""
    paths: set[str] = set()
    for _, path in _documented_endpoints(contract):
        paths.add(path)
    for token in BACKTICKED.findall(_prose(contract)):
        for candidate in token.split():
            if candidate.startswith("/"):
                for expanded in _expand_brace_choices(candidate):
                    paths.add(_normalize(expanded))
    return paths


#: 一个 `app.routes` 节点所委派到的路由，挂在这些属性上。`include_router` 在不同
#: FastAPI 版本里行为不同：有些版本把子路由拷进父节点，而从 0.143 起，它把子路由留在
#: 一个 `_IncludedRouter` 节点后面，并把收到的路由暴露成 `original_router`。两种形态
#: 都必须走，否则同一份源码在一个解释器上给出 41 条路由，在另一个上一条也没有。
ROUTE_CHILD_ATTRIBUTES = ("routes", "original_router")


def _registered_routes(routes: object) -> list[tuple[str, set[str]]]:
    """走一遍应用的路由树，为每条真实路由返回 `(路径, 方法集)`。

    这里递归地走，而不是对 `app.routes` 平铺一遍，因为 `include_router` 并不总会拍平。
    宿主解释器与容器跑着不同的 FastAPI 版本，而且彼此不一致：一个平铺的循环在本地能
    找到全部 41 条路由，在镜像里却一条都找不到。一个悄悄什么也没发现、随后把每个被
    记录的端点都报成缺失的检查器，比没有检查器更糟，所以调用方也不肯相信空结果。
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
    """把契约里的端点标题与已注册的路由比一遍。"""
    check = Check("endpoints")
    from open_source_intelligence.api.app import create_app
    from open_source_intelligence.core.config import Settings

    # 枚举路由不能依赖一个填好的 `.env`。`create_app()` 会回落到 `get_settings()`，后者
    # 从被 gitignore 的 `.env` 构造 `Settings`，并要求三个没有默认值的字段
    # （`database_url`、`redis_url`、`secret_key`）。干净的 CI 检出里没有 `.env`，于是
    # 这项检查还没来得及比较任何东西，就会抛 `ValidationError`。改为注入一份显式的测试
    # 配置：引擎只会被构造，从不连接，所以那些连接目标只是占位。
    app = create_app(
        settings=Settings(
            database_url="mysql+pymysql://app:secret@127.0.0.1:3306/osint",
            redis_url="redis://127.0.0.1:6379/0",
            secret_key="drift-check-secret-key-0123456789abcdef",
            app_env="test",
        )
    )
    registered: dict[str, set[str]] = {}
    for path, methods in _registered_routes(app.routes):
        if path in FRAMEWORK_PATHS or path.startswith("/ui/static"):
            continue
        registered.setdefault(_normalize(path), set()).update(methods - {"HEAD", "OPTIONS"})

    # 空结果意味着这趟遍历失败了，而不是应用什么都不提供。把这句话说出来，才不会让
    # 一趟坏掉的遍历看起来像一份干净的契约。
    if not registered:
        check.failures.append("no routes were discovered - the route walk found nothing to compare")
        return check

    contract = (PROJECT_ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")
    documented = _documented_paths(contract)

    # 方向一：文档承诺的必须存在。正是这项检查抓到了契约声明过、代码却从未实现的那
    # 四组资源。
    #
    # 每一个声明过的端点都会被报出来，不先套一层长度启发式。早先的版本只抱怨斜杠数
    # 大于等于二的路径，理由是更短的路径只是片段而不是承诺 —— 而这个理论恰好悄悄放过
    # 了正在追查的那种漂移形态：`GET /sources` 与 `GET /topics` 各自只有一个斜杠。
    # 把输入收窄到标题与表格行，已经足够把承诺与引用分开，路径长度不必再承载这层含义。
    for method, path in sorted(_declared_endpoints(contract)):
        candidates = [route for route in registered if route == path or route.endswith(path)]
        if not candidates:
            check.failures.append(f"documented but not registered: {method} {path}")
            continue
        if not any(method in registered[route] for route in candidates):
            check.failures.append(f"documented method not registered: {method} {path}")

    # 方向二：每条路由都被某处提到过。没人写下来的路由，就是没人评审的路由。
    for route, methods in sorted(registered.items()):
        if any(route == path or route.endswith(path) for path in documented):
            continue
        check.failures.append(f"registered but not documented: {','.join(sorted(methods))} {route}")

    check.notes.append(f"registered paths: {len(registered)}")
    return check


def check_tables() -> Check:
    """把 `Base.metadata` 与 `docs/data-model.md` 里的表小节比一遍。"""
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
    """返回那些看起来像表名的反引号 token。

    `[a-z_]+` 正是把表与文档放进反引号的其他东西区分开的东西：列名（`next_page`）、
    模块路径（`core/logging.py`）与需求编号（`FR-D-05`）都过不了这一关，而表名既不
    大写，也不带点。
    """
    return {
        token for token in BACKTICKED.findall(text) if re.fullmatch(r"[a-z_]+", token) is not None
    }


def _planned_tables(data_model: str) -> set[str]:
    """返回 `docs/data-model.md` 声明为「已设计、尚未实现」的那些表。

    要求文档把这件事明说出来，正是重点：`feedback` 曾像其他表一样被列了三个任务，而
    那份清单里没有任何东西能让读者分辨出来。
    """
    planned: set[str] = set()
    for line in data_model.splitlines():
        if PLANNED_TABLE_MARKER in line:
            planned.update(re.findall(r"`([a-z_]+)`", line))
    return planned


def _section_lines(document: str, heading_prefix: str) -> list[str]:
    """返回标题以 `heading_prefix` 开头的那一节的正文行。

    这一节在下一个同级或更高级的标题处结束。没有这条边界，某个子表的行就会漏进比较里，
    检查就会因为一个它从没打算包含的标题而通过。
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
    """把架构文档的摘要表与 `Base.metadata` 比一遍。

    `docs/data-model.md` 是字段级的参考，但 `ARCHITECTURE.md` §7 是新人最先读的那张
    表，而它正是把 `feedback` —— 一张既没有模型、也没有迁移的表 —— 列在真表旁边的那
    张。摘要可以对表分组，但不能凭空造表或漏表，所以这项检查就是同一次比较，只是粒度
    更粗。
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

    # 计划内的表在每一处以表格形式呈现的地方，都必须带着标记。`data-model.md` 的那一节
    # 有自己的标题，也是标记被定义的地方，所以这里只扫那些*引用*该节的文档。
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
    """检查两份状态公告是否点名同一个最近完成的阶段。

    状态行是读者看到的第一样东西，也是最后才有人去更新的东西：两处不同步，读者就
    会拿到两个答案。开发进度流水已移到本地过程层（`开发进度流水.md` 与 `git log`），
    架构文档不再承载任务清单，因此这里比较的是两份公告彼此一致，而不是从
    `ARCHITECTURE.md` 推导编号 —— 进度事实本来就不该由基线文档提供。
    """
    check = Check("status-lines")
    named: dict[str, set[int]] = {}
    for name, prefix in sorted(STATUS_DOCUMENTS.items()):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        line = next((item for item in text.splitlines() if item.startswith(prefix)), None)
        if line is None:
            check.failures.append(f"{name} has no status line beginning with {prefix!r}")
            continue
        stages = {int(value) for value in STATUS_TASK.findall(line)}
        if not stages:
            check.failures.append(f"{name} status line names no stage (expected T<n>)")
            continue
        named[name] = stages

    if len(named) == len(STATUS_DOCUMENTS):
        stages = [value for _, value in sorted(named.items())]
        if any(value != stages[0] for value in stages[1:]):
            detail = ", ".join(f"{key}=T{max(value)}" for key, value in sorted(named.items()))
            check.failures.append(f"status lines disagree on the last stage: {detail}")
        else:
            check.notes.append(f"last completed task: T{max(stages[0])}")
    return check


def check_evaluation_targets() -> Check:
    """把 `docs/evaluation.md` §1 里的评测变体与 `TARGET_ORDER` 比一遍。

    增加一个变体，这项改动必须落在四个地方 —— `TARGET_ORDER`、`EVAL_TARGET_TYPES`、
    `ABLATION_PROFILES`，以及迁移里的 CHECK 约束 —— 文档是第五处。只有文档没有守卫，
    而正是同一种不对称，让 `feedback` 被列成了一张真表：代码不会与自己漂移，所以没人
    去比的那个地方，就是漂移发生的地方。§1 声称它的清单与顺序与 `TARGET_ORDER` 完全
    一致，所以比较是逐字的。
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
    """把 `Settings` 与 `.env.example` 比一遍。"""
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
    """检查 `README.md` 链接到了每一份文档，且只链接存在的文档。"""
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
    """检查 `upgrade head` 是否只解析到一个修订。"""
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
    """检查受版本控制的环境变量模板里没有真实凭据。"""
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
    """检查包版本与项目元数据是否一致。"""
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
    """跑完每一项检查，打印一份 JSON 结果，任何漂移都以非零退出。"""
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
        # `passed` 只是对跑过的那几项检查的陈述。说清跑了多少项、那是不是全部，才不至于
        # 让 `--only endpoints` 被读成「整份文档集都一致」—— 一个能靠收窄输入把结论放大
        # 的检查器，身上就有它本来要补的那个洞。
        "run": len(results),
        "total": len(CHECKS),
        "complete": len(results) == len(CHECKS),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    for result in results:
        if not result.ok:
            for failure in result.failures:
                print(f"DRIFT [{result.name}] {failure}", file=sys.stderr)
    return 0 if payload["passed"] and payload["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
