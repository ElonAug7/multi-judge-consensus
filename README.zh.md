<p align="center">
  <img src="assets/logo-wide.svg" alt="Multi-Judge Consensus" width="560"/>
</p>

[English](README.md) · [简体中文](README.zh.md)

# Multi-Judge Consensus (MJC)

**一个跨厂商的模型委员会，在智能体（agent）生成的内容交付之前对其进行审查。**

MJC 将来自不同厂商的多个 LLM（DeepSeek、Zhipu GLM、Alibaba Qwen 等）组成一个评审委员会。每位裁判（judge）给出结构化的意见；由纯规则驱动的仲裁员（arbiter）投票，意见分歧时触发有界的交叉辩论。最终形成一种架构级的幻觉防御：一个模型的盲区很少与另一个模型的盲区重叠。

零第三方依赖（仅使用 Python 标准库）。自带 API 密钥。完全本地运行。

---

## 功能概览

- **结构化多裁判评审** —— 裁决（`pass` / `revise` / `reject` / `need_human`）、置信度，以及逐条列出的问题（`factual_error`、`logical_error`、`hallucination`、`style`），均以机器可读的 JSON 输出。
- **基于规则的仲裁** —— 没有任何 LLM 决定最终裁决。多数投票；平票或高置信度的少数异议会触发交叉辩论（≤ 2 轮，达成共识即停止）。
- **确定性校验器（verifier）** —— 日期区间、百分比基数、显式求和等错误由代码检查，而非 LLM：零成本、零延迟、设计上无误报。
- **成本分级路由** —— 初筛（screen）（1 次调用）→ 廉价双模型 → 旗舰委员会。升级是单调的；相同内容重复审查免费（缓存）。
- **管理控制台** —— 本地 Web UI，用于管理 API 密钥、模型目录、档位预设、信任分、实时审查流，以及逐条问题的处置记录。
- **适配器接口** —— MCP 服务器、带闸门退出码的子进程 CLI、HTTP API，以及可选的 OpenClaw 集成。

```
content
  ├─ ⓪ verifier      deterministic checks (dates / percentages / sums) — 0 LLM cost
  ├─ ① cache         identical content → previous verdict, 0 calls
  ├─ ② screen        cheap judge (glm-4-flash); pass with conf ≥ threshold → done
  ├─ ③ committee     3 models review independently and in parallel
  ├─ ④ arbiter       pure rule vote: ≥2/3 pass → pass; disagreement → debate
  ├─ ⑤ debate ≤2 rds each judge sees the others' opinions, may change verdict
  ├─ ⑥ factcheck     non-pass only: factual issues re-checked by independent, non-complainant judges
  └─ ⑦ verdict       result + token usage + cost estimate, fully logged
```

## 工作原理

### 零幻觉设计（v0.12.1）

检测 → 仲裁 → 修复 → 复检——如此设计，使系统绝不会把某个事实“修”成另一种错误：

- **事实仲裁（Fact arbitration）**（`mjc/factcheck.py`）—— 在尝试任何事实性修复之前，该主张会由非申诉方、跨厂商的模型独立复检（`confirmed` / `refuted` / `unknown`）。只有 `confirmed` 的替换才可以触及具体事实；`refuted` 意味着保留原文；`unknown` 只允许弱化表述。
- **证伪者（Falsifier）通道**（`mjc/falsifier.py`）—— 一个对抗式红队评审员（默认跨厂商）搜寻最薄弱的主张（前提、事实、绝对化表述）；其质疑走同一套仲裁流程，只有被确认的质疑才能把委员会的 `pass` 升级为 `revise` —— 设计上不会出现误报升级。
- **外部知识（可选启用）**（`mjc/knowledge.py`）—— 仲裁可以拉取网页证据片段（Bing/Sogou/Baidu 的 HTML 后端，或自定义命令后端；默认关闭，带缓存与限流），使证伪/确认建立在可检索的来源之上，而不只是模型记忆。
  **v0.11.0 现实核查（实测于 2026-09-13）：** 抓取类后端实际上已基本失效——Sogou 返回 HTTP 403，Baidu 返回其反爬页面，Bing 可用但对具体实体的召回很差，且从本机无法访问境外端点。现在每个后端都会记录健康状态（`ok` / `blocked` / `unparsed` / `error`），并由 `evidence_check` 呈现，因此“我们被拦截了”不再被报告为“不存在证据”；可用 `python3 -m mjc.cli knowledge-probe` 探测。若需可靠证据，请通过 `cmd` 后端接入真实的搜索 API（`MJC_KNOWLEDGE_CMD`）。
- **双生产者修复（Dual-producer repair）**（`mjc/repair.py`）—— 两个厂商在同一套规则下各自独立修订。v0.7.2 的值级共识：跨模型的措辞天然不同，因此“近乎相同的文本”过于严格（在 A/B 试点 C5 中拦下了每一个真实修复）。现在的一致性判断基于**数值集合**——集合相等（双方都去掉了旧值）或真子集修订（主张更少）可以应用；纯文本改写和被掏空的“无信息”修订永不自动应用。意见不一致时保留原文（不注入单模型错误）。
- **值操作安全门（v0.8.0）** —— 在试点 C6 显示“双生产者一致”的放行仍可能在委员会误拒时掏空一个*正确*的值之后，只允许净**值→值替换**：纯删除（丢失值）和纯新增被直接阻止；替换还可能额外要求**盲重采样支持**（配置项 `repair.resample_gate`，默认关闭——见 `mjc/resample.py` 的自洽性路线）才可采纳。
- **知识证据门（Knowledge evidence gate, v0.9.0）** —— 当盲重采样无定论（或重采样门关闭）时，值替换仍可通过**外部检索证据**获得放行：一个可选启用的门（配置项 `repair.evidence_gate`，默认关闭）只搜索**问题本身**（≤80 字符），并要求 ≥ `min_snippets` 个片段包含新值且**不**包含旧值。重采样的 `conflict` 永不能被证据覆盖；检索出错时保守地阻止。两个门都关闭即完全保持此前的默认行为。
- **后端合并检索（Backend-merge retrieval, v0.9.1）** —— 证据搜索现在会跨已配置的多个后端累积并去重片段（`repair.evidence_gate` 中的 `merge_backends`，**默认开启**）：达到 ≥3 个唯一片段或已尝试 3 个后端即停止，某个后端结果为空时重试一次，并跳过失败继续——消除了 csqa-07 演示中复现的单后端不稳定（`merge_backends: false` = 旧路径）。
- **证据独立性 + 查询卫生（Query hygiene, v0.11.0）** —— 通过审计 csqa-07 证据演示发现的两个有效性缺陷，均已修复：
  （1）查询过去是 `task + proposed value`，因此搜索 “2009” 会必然返回包含 “2009” 的页面——这道门是**自我认证**的（`support` 度量的是关键词回声，而非独立佐证）。现在查询只包含问题本身，并且任何旧值/新值的字面量都会被防御性地剔除。
  （2）查询过去会包含提示词的指令外壳（“请用一句话以内回答下面的问题：”），于是抓取到的是字符“请”的词典页面（6/6 个片段）——`task_question()` 现在会先剥离该外壳。此外：生产者失败会被显式呈现（`producer_errors` + 说明），而不再降级为一句干巴巴的“少于 2 条修订”。
- **修订守则（Revision rules）**（`mjc/revision.py`）—— 修复者绝不能引入新的具体事实；存疑时应做对冲或弱化表述，而不是替换为猜测（评审员的建议是线索，不是真相）。
- **闸门默认 = 全量委员会** —— 交付闸门默认跳过廉价初筛（用 `--screen` 可重新启用）。

### 轻量化（v0.11.0j/k，生产向；实验默认不变）

实测开销（2026-09-13，C9f 单题，`api_calls`）：干净内容 **4 次**、普通非 pass **6–18 次**、
全链（委员会+仲裁+证伪+两轮修订+复审）**32 次**。重尾集中在**非 pass 路径**，可按下面几档削减：

| 杠杆 | 配置 | 实测/预期 | 代价 |
|---|---|---|---|
| **浏览器检索后端** | `knowledge.backends=["browser"]` | 证据等待 **300s → 2.5s**（无反爬、无冷却） | 需本机有 Chrome |
| **批量仲裁** | `factcheck.batch=true` | 仲裁块 **意见数×2 → 2** 次（4 条时 8→2） | 单次调用需复核多条，判断略粗 |
| **初筛** | `screen_enabled=true` + `screen_model` | 干净内容 **1 次**放行（跳过委员会 3 次） | 漏检风险由 `screen_conf` 控制 |
| **经济档委员会** | `current.tier="eco"` | 3 模型 → 2 模型 | 检出率需复测 |
| **少一轮复审** | 调用方 `--max-rev 1` | 省掉一整轮（委员会+证伪+仲裁） | 少一次收敛机会 |

默认全部保持不变（`batch=false`、C 臂显式 `no_screen=True`、证据门 opt-in），
以免污染 A/B 口径；上面是**生产部署**时的推荐配方。


**档位预设**（管理控制台一键切换）：

| 档位 | 委员会 | 策略 |
|---|---|---|
| 经济（Economy） | 2 个白菜模型 | 最省，初筛优先 |
| 标准（Standard） | 2 白菜 + 1 旗舰 | 默认（经基准验证） |
| 严格（Strict） | 仅旗舰，不用初筛 | 最高严谨度 |

## 实测结果

对抗性基准 v1 —— 21 个样本（跨文档矛盾、时间性幻觉、数值陷阱、指令偏离，外加干净对照组），使用真实 API 调用运行，2026-09-06：

| 流水线 | 缺陷漏过率 | 对干净内容的误杀 |
|---|---|---|
| 不审查（原样交付） | 100%（18/18） | — |
| 单模型自查（glm-4-plus） | 11.1%（漏 2/18） | — |
| **MJC 全流水线** | **0%（18/18 全拦截）** | **0/3** |

全集召回率 1.0 · 精确率 1.0 · F1 1.0。分类别：5/5、5/5、5/5、3/3。
分层：1 个由校验器确定性捕获（0 LLM 成本），17 个由委员会 + 辩论捕获。

复现：`python3 -m mjc.cli bench --set v1-full`（约 ¥0.6 的 API 开销）。
历史记录追加到 `logs/bench-history.jsonl`，用于回归追踪。

### 代码审查实测（2026-09-13）

一个 **518 行 / 14,044 字符**的 Python 模块（订单结算引擎，可正常编译）中，**刻意植入 12 个落在 MJC 声明能力边界内的缺陷**——描述与实现不符（4）、数字/日期错误（4）、逻辑矛盾如死分支/不可达分支（4）——另有 **4 处"看起来可疑但其实正确"的诱饵**（含等号的 `>=` 边界、有文档说明的折扣叠加顺序、`ROUND_HALF_UP` 量化、负值收敛），用于测误报。编译与运行时错误**刻意排除**：那属于测试套件的职责，不属于 MJC。

| 臂 | API 调用 | 延迟 | 成本 | 找到缺陷 | 误报 | 输出可用性 |
|---|---|---|---|---|---|---|
| A 确定性验证器（零 API） | 0 | 0.0 秒 | 0 | 0/12 | 0 | 可用 |
| B 单模型自查（glm-4-plus） | 1 | 88 秒 | ~0.21 | 4/12 | 不适用 | **不可用** |
| **C MJC 全流水线** | **7** | **346 秒** | **0.29192** | **6/12** | **0** | **可用** |

- **零误报。** MJC 提出的 6 条意见逐条对照封存的真值清单核验，全部为真；4 处诱饵一处未报。
- **单模型臂没有产出可用结果。** 其响应撞上输出 token 上限，JSON 数组没有闭合、无法解析；且从第 11 条起进入重复循环——114 个可抢救条目中只有 22 条是不同内容，其余 92 条在重复已有抱怨（同一句最多重复 13 次，总条数为不同内容的 5.2 倍）。
- **分类召回**（MJC / 单模型）：描述与实现不符 2/4 对 1/4，数字 2/4 对 2/4，逻辑 2/4 对 1/4。
- **每检出 1 个缺陷的成本**：MJC 0.049，单模型约 0.053（但其输出不可用）。本次共 59,678 tokens（输入 51,545 / 输出 8,133）。
- **MJC 的弱项（如实说明）**：漏掉的 6 条都**没有直接的文字矛盾**，需要理解业务语义才能发现（计税基数用了折前小计、退款路径可超过实付金额、优惠券阈值前后不一致、top-N 排序方向反了、缺少"未拆封"校验、预计送达的注释与常量不一致）。MJC 是**交付前校对员**，不是单元测试的替代品。12 个缺陷属构造样本而非生产分布，样本量为单个模块。

## 快速上手

> 面向首次使用者的分步演练（密钥配置、首次审查、Web 控制台、智能体集成、成本表、故障排查）：**[QUICKSTART.md](QUICKSTART.md)**

```bash
git clone https://github.com/ElonAug7/multi-judge-consensus.git
cd multi-judge-consensus

python3 -m mjc.cli setup      # ① enter API keys interactively (skippable; stored locally, 0600)
python3 -m mjc.cli webui      # ② admin console at http://127.0.0.1:8123

# ③ review a piece of content
python3 -m mjc.cli judge-only \
  --task "Summarize tomorrow's weather in Beijing" \
  --output "Beijing will have a heavy storm tomorrow"
```

要求：Python ≥ 3.9。无需 `pip install`。没有任何密钥时，你仍可运行校验器、UI 和离线测试套件；只有一家厂商的密钥时，委员会会自动缩小（推荐：两家或更多厂商）。

密钥从环境变量（`MJC_DEEPSEEK_KEY`、`MJC_GLM_KEY`、`MJC_<PROVIDER>_KEY`）或 `keys.local.json`（已加入 gitignore，chmod 600）读取。配置模板见 `settings.example.json`。

## CLI

| 命令 | 用途 |
|---|---|
| `setup` | 交互式首次配置 API key + 连通性探测 |
| `doctor` | 健康检查：key、档位、信任分、缓存、累计用量 |
| `judge-only --task --output` | 用委员会审查给定文本 |
| `review --task` | 生成 → 审查 → 重写循环（≤3 次 reject） |
| `auto --task --content [--kind code] [--channel ID]` | 单次审查（带记忆上下文），输出 JSON；`--channel` 标识调用方 |
| `gate --stage design\|code\|deliver --task --content` | 阶段闸门；退出码 pass=0 / revise=2 / reject=3 |
| `bench [--set quick\|v1-full] [--ablation 0,1,2]` | 红队对抗基准（带历史记录） |
| `dispose --in <json>` | 记录逐条处置（采纳／不改并附理由） |
| `switch on\|off\|status` | 暂停／恢复自动审查钩子（0 花费、完全静音；手动命令不受影响） |
| `webui` | 本地管理台（127.0.0.1:8123） |
| `savings [--days N] [--json]` | 节省账本：各机制省下的 tokens／费用／时间 |
| `knowledge-probe` | 逐后端探活并报告状态（ok / blocked / unparsed / error） |
| `mcp` | MCP stdio server |

## 与其他智能体集成

**MCP**（Claude Desktop/Code、Cursor、Windsurf、Cline 等）：

```bash
python3 -m mjc.mcp
# Claude Code:  claude mcp add mjc -- python3 -m mjc.mcp
```

`review(task, output)` 工具返回 `{verdict, votes, issues, api_calls, tokens, cost_yuan}`。

**子进程 CLI** —— 单行 JSON 加上退出码闸门语义；可用于任何语言或 CI。

**HTTP** —— `POST /api/review`，可选 token 鉴权（Dify/Coze/n8n 自定义工具、跨机器调用）。

**OpenClaw** —— 可选的原生集成：转录扫描、编码阶段闸门、实时审查流。

## 架构

```
mjc/
├── providers.py     vendor registry (deepseek/glm/qwen/dashscope/doubao/kimi; add keys to enable)
├── judge.py         single reviewer: structured JSON, memory injection, same-vendor fallback
├── arbiter.py       vote/debate arbitration; per-opinion live events
├── pipeline.py      verifier → screen → committee; fault-tolerant degradation
├── verifier.py      deterministic checks (dates/percentages/sums), zero false positives by design
├── bench.py         adversarial benchmark runner (recall/precision/F1 + history)
├── webui.py         admin console (review / live tasks / settings)
├── mcp_server.py    MCP stdio server
└── settings.py      runtime config (provider registry, model catalog, tier presets)
tests/               18 offline suites (0 API calls; key-dependent cases skip gracefully)
samples/bench-v1.json   adversarial benchmark corpus
```

## 开发

```bash
python3 tests/test_phase3.py     # pipeline/cache/degradation
python3 tests/test_verifier.py   # deterministic verifier
python3 tests/test_mcp.py        # MCP protocol
# …18 suites total, all offline. GitHub Actions runs them on Python 3.9/3.11/3.12.
```

## 安全与说明

- 密钥只存放在环境变量或本地的 `keys.local.json`（0600，已加入 gitignore）中。
- 运行时配置、日志和审查记录都留在本地，绝不提交。
- 成本数字是根据各厂商价格表（¥/1K tokens）估算的近似值，已标注为近似；
  token 数来自 API 的 usage 字段。
- 基准衡量的是在构造语料上的最坏情况拦截率，而不是自然生产分布。

## 许可证

GPL-3.0。仅限本地调用；密钥由用户自行提供。
