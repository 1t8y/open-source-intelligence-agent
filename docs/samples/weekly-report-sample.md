# 报告样例：LLM Agent 开源技术情报周报（真实数据）
> 层级：证据报告｜职责：数字可复现的报告成品样例｜不构成契约，不被比较器当事实读

> **这是一份「数字可复现」的真实报告样例**，由系统对真实采集快照自动生成。
> 报告正文里的每一个净增数字，都可以用 `repo_snapshots` 表里相邻快照的 stars 首末差**逐一复现**，见文末「数字可复现性核对」。

## 数字可复现性核对

本期窗口 `2026-10-22 .. 2026-10-26`，净增 = 窗口内最后一个快照的 stars − 第一个快照的 stars：

| 仓库 | 窗口首快照 (10-22) | 窗口末快照 (10-26) | 净增（末 − 首） | 报告数字 |
|---|---:|---:|---:|---:|
| morluto/rea | 359,166 | 389,021 | +29,855 | +29,855 ✓ |
| DietrichGebert/ponytail | 171,957 | 176,721 | +4,764 | +4,764 ✓ |
| Graphify-Labs/graphify | 131,857 | 135,805 | +3,948 | +3,948 ✓ |
| affaan-m/ECC | 285,837 | 289,326 | +3,489 | +3,489 ✓ |
| TauricResearch/TradingAgents | 112,615 | 115,420 | +2,805 | +2,805 ✓ |

> 净增由 `reports/composition.py:_movement_rows` 对快照序列首末差计算，**模型不参与数字计算**，只负责趋势判断与写作。
> 每条引用锚点（如 `repo_snapshots:morluto/rea:2026-10-26`）都能定位到 `repo_snapshots` 表里的具体快照行。
>
> **关于表格比正文多 5 行的说明**：关键数字表按**主题当前全部入榜候选**（10 个）计算，而本轮 Agent 只深读了前 5 个（`AGENT_MAX_CANDIDATES=5`），所以摘要与趋势章节只讨论这 5 个。表里的 10 个净增都是真实快照首末差，可以逐个核对；这不是数据错误，而是「表算全榜、正文读前 N」的口径差异。

---

# LLM Agent 开源技术情报周报（真实数据样例 2026-10-26）

> 本轮候选仓库：morluto/rea、DietrichGebert/ponytail、affaan-m/ECC、Graphify-Labs/graphify、TauricResearch/TradingAgents、NousResearch/hermes-agent、paperclipai/paperclip、harry0703/MoneyPrinterTurbo、firecrawl/firecrawl、JuliusBrussee/caveman
> 快照窗口：2026-10-21 至 2026-10-26；引用快照 14 个

**关键数字（系统统计）**

| 仓库 | 净增 stars | 占合计 |
|---|---:|---:|
| morluto/rea | +29,855 | 57.5% |
| DietrichGebert/ponytail | +4,764 | 9.2% |
| affaan-m/ECC | +3,489 | 6.7% |
| Graphify-Labs/graphify | +3,948 | 7.6% |
| TauricResearch/TradingAgents | +2,805 | 5.4% |
| NousResearch/hermes-agent | +1,265 | 2.4% |
| paperclipai/paperclip | +1,756 | 3.4% |
| harry0703/MoneyPrinterTurbo | +1,461 | 2.8% |
| firecrawl/firecrawl | +1,554 | 3.0% |
| JuliusBrussee/caveman | +1,024 | 2.0% |
| **合计** | **+51,921** | 100% |

## 本周摘要

本期（2026-10-22 至 2026-10-26）覆盖 5 个候选仓库，主题聚焦「AI Agent 技能/工具链」方向，语言分布为 TypeScript 1 个、JavaScript 2 个、Python 2 个，许可证均为 MIT 或 Apache-2.0。

规模量级（最新快照 2026-10-26）：
- morluto/rea（#100）：389,021 stars / 97,446 forks，主题内首位。
- affaan-m/ECC（#99）：289,326 stars / 42,871 forks。
- DietrichGebert/ponytail（#107）：176,721 stars / 9,662 forks。
- Graphify-Labs/graphify（#112）：135,805 stars / 12,951 forks。
- TauricResearch/TradingAgents（#115）：115,420 stars / 22,197 forks，主题内向金融垂直场景分化。

窗口内五个仓库 stars 全部为正增长，无一个出现回落，主题整体为上行态势。

## 趋势观察

**主题级结论：整体升温，但内部呈现「一强领跑、四家温和跟进」的分层格局。** 判断依据是五个仓库在窗口内 stars 增量均为正，且无一仓库出现负增长或停滞，属于全线上行而非分化。

逐仓库方向与节奏（依据逐日 stars_delta，非首尾端点）：

- **morluto/rea（#100）— 升温，增速加快。** 10-23 单日增量 10,942，10-26 检查点增量 18,913（覆盖 3 天），后段日均明显高于前段，是主题上行的主要拉动者。
- **affaan-m/ECC（#99）— 升温，增速加快。** 10-23 增量 1,087，10-26 检查点增量 2,402，后段日均抬升，节奏由平缓转为提速。
- **DietrichGebert/ponytail（#107）— 平稳偏强，增速放缓。** 10-22 至 10-23 两日增量分别为 7,193 与 2,387，之后 10-26 检查点增量仅 2,377，增量几乎持平，属于高位走平。
- **Graphify-Labs/graphify（#112）— 平稳，小幅波动。** 10-22 增量 4,804、10-23 增量 1,691、10-26 增量 2,257，日增量呈锯齿状，无明确加速或衰减。
- **TauricResearch/TradingAgents（#115）— 升温，增速加快。** 10-23 增量 594 为窗口低点，10-26 检查点增量 2,211，后段显著回升，是节奏反转最明显的一个。

分化点说明：本期不存在方向相反的仓库，因此分化不体现在「涨/跌」上，而体现在**贡献权重**上——#100 单日增量量级远超其余四家，是拉动主题上行的核心；#107 虽增速放缓但基数高，仍构成上行的重要底盘；#112 与 #115 属于跟随型上行。

数据覆盖说明：#100 与 #99 的首个快照 stars_delta 为 null（无前序基线），其趋势判断基于 3 个快照中的后两次增量，观测点数量为 2 个、勉强达到判断门槛，节奏判断的置信度低于其余三个仓库，建议下期补足基线后复核。

## 重点项目

**本期主题上行由 morluto/rea（#100）单点拉动，其余四家提供次要增量。**

按窗口内净增排序（stars 首尾差）：

1. **morluto/rea（#100）+29,855**：359,166 → 389,021。绝对增量约为第二位仓库的 6.6 倍，是本期唯一具备「结构性拉动」能力的仓库，其定位（agent 驱动逆向工程，覆盖 Ghidra/Hopper/MCP 等工具链）在主题内差异化最明显。
2. **DietrichGebert/ponytail（#107）+4,764**：171,957 → 176,721。净增排第二，但增量主要由窗口首日贡献，后段已走平。
3. **Graphify-Labs/graphify（#112）+3,948**：131,857 → 135,805。稳定跟随增长。
4. **affaan-m/ECC（#99）+3,489**：285,837 → 289,326。基数最大但增量排第四，增长效率相对偏低。
5. **TauricResearch/TradingAgents（#115）+2,805**：112,615 → 115,420。净增最小，但后段动能回升，是本期唯一「低开高走」的仓库。

另可注意：#107 的 fork 数仅 9,662，与 #99 的 42,871、#100 的 97,446 相差一个量级，说明其增长形态更偏「个人工具被收藏」而非「被二次开发」，与 #99/#100 的生态型增长路径不同。

## 风险与不确定性

**1. 主题集中度风险。** 本期上行高度依赖 morluto/rea（#100）单一仓库，其窗口净增 29,855，而其余四家合计约 15,000。一旦 #100 增速回落，主题级「升温」结论将立即失去支撑，判断脆弱性较高。

**2. 快照间隔不均，日均口径不可靠。** 10-23 与 10-26 之间间隔 3 天，但 stars_delta 字段为区间累计值而非日均值。直接比较 10-23 与 10-26 两个增量会产生严重误导（例如 #100 的 18,913 实为 3 天累计），任何人均化推算均需显式说明口径，本报告未做日均换算。

**3. 基线缺失。** #100 与 #99 的首个快照（2026-10-22）stars_delta 为 null，说明窗口起点无前序观测，全窗口净增只能用首尾 stars 差值倒推，与系统累计口径可能存在偏差，待人工复核。

**4. 元数据不完整。** 全部 5 个仓库的 open_issues 在窗口内所有快照中均为 null，无法评估社区维护压力与积压问题，相关判断本期缺失。

**5. README 未纳入本期核对。** 本期未读取各仓库 README，上述所有结论仅基于指标数据，仓库定位描述来自平台简介字段，未经内容层交叉验证，待人工复核。

## 行动建议

**1. 优先深挖 morluto/rea（#100）。** 它是本期唯一具备结构性拉动能力的仓库，建议下期补充 README 与内容层证据，重点核实其「agent 驱动逆向工程 + MCP」定位是否有实质交付物支撑，避免把平台简介当作事实。

**2. 补足观测基线后再下趋势结论。** 针对 #100 与 #99 缺失首个 stars_delta 的问题，建议将观测窗口向前延伸至少 1 个快照，并对 10-23→10-26 的 3 天间隔统一按「区间累计」口径归档，避免后续报告误做日均比较。

**3. 将 #107 单列观察。** 其 fork 数与 stars 严重不成比例（9,662 vs 176,721），且后段增量已走平，建议下期判断它是「工具型收藏热」还是「社区采用」——前者意味着增长天花板临近。

**4. 关注 #115 的反弹是否可持续。** 它是本期唯一低开高走的仓库（10-23 增量 594 → 10-26 累计 2,211），且在主题内属垂直场景（金融多智能体），建议下期单独追踪，验证是否为偶发波动。

**5. 补齐 open_issues 数据源。** 5 个仓库该字段全为空，建议在数据侧排查采集链路，否则风险章节将持续缺失社区健康度维度。

## 附录

**证据规模**

- 主题编号：44；候选仓库：5 个（repo_id 100、107、99、112、115）。
- 指标序列：5 组，每个仓库 1 组，覆盖窗口 2026-10-22 至 2026-10-26。
- 覆盖快照数：每组 3 个快照（2026-10-22、2026-10-23、2026-10-26），引用快照 15 个（去重后为上述 3 个日期 × 5 仓库）。
- README：本轮未读取，计 0 份。
- 历史报告：本轮未引用，计 0 篇。

**数据口径说明**

- stars_delta 为相邻快照间的区间累计增量，非日均值；2026-10-23 与 2026-10-26 间隔 3 天。
- open_issues 字段在全部快照中为 null。
- 仓库定位描述取自平台简介与 topics 字段，未经 README 交叉核对。

## 引用与证据

### 指标快照

- [repo_snapshots:morluto/rea:2026-10-26](https://github.com/morluto/rea)
- [repo_snapshots:affaan-m/ECC:2026-10-26](https://github.com/affaan-m/ECC)
- [repo_snapshots:DietrichGebert/ponytail:2026-10-26](https://github.com/DietrichGebert/ponytail)
- [repo_snapshots:Graphify-Labs/graphify:2026-10-26](https://github.com/Graphify-Labs/graphify)
- [repo_snapshots:TauricResearch/TradingAgents:2026-10-26](https://github.com/TauricResearch/TradingAgents)
- [repo_snapshots:morluto/rea:2026-10-23](https://github.com/morluto/rea)
- [repo_snapshots:affaan-m/ECC:2026-10-23](https://github.com/affaan-m/ECC)
- [repo_snapshots:DietrichGebert/ponytail:2026-10-22](https://github.com/DietrichGebert/ponytail)
- [repo_snapshots:Graphify-Labs/graphify:2026-10-22](https://github.com/Graphify-Labs/graphify)
- [repo_snapshots:TauricResearch/TradingAgents:2026-10-23](https://github.com/TauricResearch/TradingAgents)
- [repo_snapshots:morluto/rea:2026-10-22](https://github.com/morluto/rea)
- [repo_snapshots:affaan-m/ECC:2026-10-22](https://github.com/affaan-m/ECC)
- [repo_snapshots:DietrichGebert/ponytail:2026-10-23](https://github.com/DietrichGebert/ponytail)
- [repo_snapshots:Graphify-Labs/graphify:2026-10-23](https://github.com/Graphify-Labs/graphify)
