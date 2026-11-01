FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml README.md ./
COPY requirements.txt ./
COPY src ./src
COPY prompts ./prompts
COPY datasets ./datasets
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

# 一次装齐依赖与本包。`--retries` / `--timeout` 是给镜像源抖动留的余量：默认 5 次 15 秒在
# 国内源上会装出一个缺包的半成品镜像（曾出现 `No module named 'urllib3'`，构建却是成功的）。
#
# 依赖分两步：先按 `requirements.txt` 装精确 pin（与 CI 测试环境同源，`requirements-ci.txt`
# 就是 `-r requirements.txt` 加三个测试工具），再用 `--no-deps` 装本包。只让 `pip install .`
# 自己解析会走到 `pyproject.toml` 的宽范围（如 `fastapi>=0.115,<1`）拉到最新（曾到 fastapi
# 0.162 / pydantic 2.14.2），与 CI 锁的 0.136.3 / 2.10.6 两套漂移；`--no-deps` 防止装包时把已
# 锁版本再升级。
#
# 已知代价：`COPY src` 在安装之前，改一行源码就会让这一层失效、重下一次全部依赖（约 1–3 分钟）。
# 试过"先装空包再覆盖源码"的两段式分层，但第二次 `pip install` 会留下上一次的空 `__init__.py`，
# 镜像能构建成功却启动即崩（`cannot import name '__version__'`）—— 宁慢勿脆，保持单段。
RUN pip install --no-cache-dir --retries 5 --timeout 60 --index-url "$PIP_INDEX_URL" \
        -r requirements.txt && \
    pip install --no-cache-dir --no-deps .

USER app

EXPOSE 8000
