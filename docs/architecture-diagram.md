# 系统架构图
> 层级：详细规格（展开基线）｜职责：逐组件对应代码模块的架构图｜冲突时以三份根文档为准

> 一张图看完整条流水线：数据源 → 采集 → 快照 → 排名 → 研究 Agent → 报告组合 → 质量闸门 → 发布/推送。
> 旁路标注 MySQL、Redis、Celery worker/beat 与模型计费。
> 图中的每个组件都能在 `src/open_source_intelligence/` 里找到对应模块。

```mermaid
flowchart TB
    subgraph SRC["数据源（外部，只读）"]
        GH["GitHub API<br/>collectors/"]
        HF["Hugging Face API<br/>collectors/"]
    end

    subgraph INGEST["确定性采集管线（pipeline/）"]
        C1["collect<br/>拉取仓库元数据与指标"]
        C2["snapshot<br/>每日快照<br/>（同仓库同日唯一）"]
        C3["rank<br/>候选筛选与排序"]
        C4["contents<br/>预热候选 README 缓存"]
    end

    subgraph AGENT["研究 Agent（agent/，LangGraph）"]
        P["plan<br/>规划要查什么"]
        T["tools<br/>查画像 / 查指标 / 查 README / 查历史报告"]
        O["observe<br/>吸收工具结果"]
        V["verify<br/>验证器：证据是否充分"]
        W["write<br/>写草稿（六章节，带引用）"]
        P --> T --> O --> V -->|证据不足则回到 plan| P
        V -->|充分| W
    end

    subgraph REP["报告链路（reports/）"]
        R1["compose<br/>组合报告<br/>数字由代码算，不由模型写"]
        R2["verify<br/>引用覆盖 + 事实一致性"]
        R3{"publish policy<br/>质量闸门"}
        R1 --> R2 --> R3
    end

    subgraph OUT["出口"]
        PUB["发布<br/>AUTO / AUTO_WITH_DELAY / MANUAL"]
        PSH["推送<br/>notifications/（HMAC 签名）"]
        R3 -->|通过| PUB
        R3 -->|证据不足 / 敏感| MANUAL["转人工审核"]
        PUB --> PSH
    end

    subgraph BUDGET["预算与审计（贯穿全链路）"]
        COST["模型计费<br/>token / cost 预算闸"]
        AUDIT["审计日志<br/>RBAC / ACL / 脱敏"]
    end

    subgraph INFRA["基础设施"]
        MYSQL[("MySQL 8<br/>持久化 / 唯一约束 / 运行时不变量")]
        REDIS[("Redis<br/>Celery broker")]
        API["api<br/>FastAPI + Jinja2/HTMX 界面"]
        WKR["worker<br/>Celery 异步任务"]
        BEAT["beat<br/>每日 07:00 定时触发"]
    end

    GH --> C1
    HF --> C1
    C1 --> C2 --> C3 --> C4
    C4 --> T
    W --> R1
    COST -.预算限制.-> AGENT
    AUDIT -.记录每一次写操作.-> AGENT
    AUDIT -.记录每一次写操作.-> REP
    MYSQL --- API
    MYSQL --- WKR
    REDIS --- WKR
    REDIS --- BEAT
    BEAT -->|"daily pipeline 任务"| WKR
    WKR --> C1
    API --> WKR
    API --> MYSQL

    classDef src fill:#e3f2fd,stroke:#1565c0,stroke-width:1px,color:#0d47a1;
    classDef pipe fill:#e8f5e9,stroke:#2e7d32,stroke-width:1px,color:#1b5e20;
    classDef agent fill:#fff3e0,stroke:#ef6c00,stroke-width:1px,color:#e65100;
    classDef rep fill:#f3e5f5,stroke:#7b1fa2,stroke-width:1px,color:#4a148c;
    classDef out fill:#e0f7fa,stroke:#00838f,stroke-width:1px,color:#006064;
    classDef inf fill:#eceff1,stroke:#37474f,stroke-width:1px,color:#263238;
    class GH,HF src;
    class C1,C2,C3,C4 pipe;
    class P,T,O,V,W agent;
    class R1,R2,R3 rep;
    class PUB,PSH,MANUAL out;
    class COST,AUDIT inf;
    class MYSQL,REDIS,API,WKR,BEAT inf;
```

## 关键工程环节（不只是「大模型」）

| 环节 | 模块 | 一句话 |
|---|---|---|
| 计费 | `agent/` 预算闸 | 每次模型调用累计 token/cost，超 `AGENT_MAX_COST`/`AGENT_MAX_TOKENS` 即停 |
| 验证 | `agent/graph.py` 验证器 | 证据不充分就回到规划、拒绝写草稿，而不是带着猜测往下走 |
| 数字可信 | `reports/composition.py` | 净增/百分比由代码对快照首末差计算，模型只写判断、不写数字 |
| 闸门 | `reports/` 发布策略 | `AUTO_WITH_DELAY`/`MANUAL` 决定「直接发布」还是「转人工」 |
| 幂等 | `pipeline/` | 同仓库同日唯一快照、采集可重复执行、重复任务无副作用 |
| 注入防护 | `agent/` 守卫 | 命中即停、转人工，注入文本不达模型 |
