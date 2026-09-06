# Multi-Judge Consensus（多模型共识审查框架）· 纯国产模型版

让多个不同厂商的国产大模型组成"审查委员会"，对 Agent 输出做阶段性交叉审查：结构化辩论 + 多数投票达成共识，从架构层面压制幻觉。

## 效果验证（2026-09-06 · 红队基准集 v1 实测）

21 条对抗样本（跨文档矛盾 5 / 时序幻觉 5 / 数值陷阱 5 / 指令偏离 3 / 干净对照 3），
pro 档全流程（验证器 → 初筛 → 4 委员 → 辩论 ≤2 轮）：

| 场景 | 缺陷放行率 | 干净误杀 |
|---|---|---|
| 无审查直接交付 | 100%（18/18 原样流出） | — |
| 单模型自查（glm-4-plus） | 11.1%（漏 2/18） | — |
| **MJC 全流程** | **0%（18/18 全拦）** | **0/3** |

- 全量 Recall 1.0 · Precision 1.0 · F1 1.0；分类别 5/5 · 5/5 · 5/5 · 3/3
- 拦截分层：确定性验证器 1 条（求和，零 LLM 成本），委员会+辩论 17 条
- 复现：`python3 -m mjc.cli bench --set v1-full`（≈¥0.6/次真 API）；历史曲线 logs/bench-history.jsonl
- quick 子集 `--set quick`（9 条 ≈¥0.2）；辩论消融 `--ablation 0,1,2`

> 口径：构造缺陷集（最坏情况拦截能力），非真实生成分布。样本自洽性同样经过质检（初版 1 条样本本身可自洽被替换）。

## 为什么有效

不同厂商的模型训练数据与架构不同，"盲区"不同。DeepSeek 和 GLM 对同一段有幻觉的文本独立审查时，双双以 0.98+ 置信度识别出矛盾——单一模型可能漏掉，委员会很难集体瞎。

**实测（9 样本验收）**：幻觉识别率 **83%**、误杀率 **0%**（目标 ≥50%）。

## 快速开始

```bash
# 下载即用三步（零依赖，Python ≥3.9 即可，无需 pip install 任何包）
git clone <repo-url> && cd multi-judge-consensus
python3 -m mjc.cli setup          # ① 向导录入 API key（deepseek/智谱有免费额度；可回车跳过）
python3 -m mjc.cli webui          # ② 打开本地管理台 http://127.0.0.1:8123
# ③ 审查一段输出（初筛 → 委员会 → 辩论，全自动）
python3 -m mjc.cli judge-only --task "查明天北京的天气并写提醒" --output "明天北京有特大暴雨，请带伞"

# 其他入口
python3 -m mjc.cli doctor         # 体检（key/档位/信任分/缓存/累计用量）
python3 -m mjc.cli review --task "写一封请假邮件"   # 生成→审查→打回循环
python3 -m mjc.cli bench --set quick               # 跑红队基准（真 API，≈¥0.2）
python3 -m mjc.cli setup                           # 重跑向导可增删 key
```

> 没配 key 也能玩：界面/离线测试/文档全可用；key 配 1 家即可跑（委员会自动按可用厂商收缩），
> 配 2 家以上效果最佳（跨厂商盲区互补是核心设计）。key 只存本机 keys.local.json（600）或环境变量。

## 架构

```
用户任务 → Agent(生成模型) → 子任务完成
                    ↓ 中间结果
             审查委员会 Judge Pool
              ┌──────────┬──────────┐
              │DeepSeek  │   GLM    │   ← 不同厂商，独立审查
              └────┬─────┴────┬────┘
                   ↓          ↓
              结构化意见（verdict/confidence/issues[]）
                   ↓
              仲裁器：多数投票 → 分歧? → 交叉辩论(≤2轮) → 最终裁决
                   ↓
            pass → 进入下一阶段 | reject → 打回重写（附审查意见）
```

## 模块

| 模块 | 文件 | 说明 |
|---|---|---|
| providers | `mjc/providers.py` | 国产模型统一调用（urllib 零依赖）：deepseek / glm（+qwen 预留） |
| judge | `mjc/judge.py` | 单 Judge 审查：结构化 JSON（verdict/confidence/issues/final_reasoning），支持交叉辩论输入 |
| arbiter | `mjc/arbiter.py` | 投票 + 分歧触发辩论 + 仲裁（串行/并发 ParallelArbiter） |
| pipeline | `mjc/pipeline.py` | Phase 3 统一审查入口：初筛→降级→委员会，缓存/信任分/日志内聚 |
| cache | `mjc/cache.py` | 审查结果缓存（P3.2）：同内容重跑 0 API |
| trust | `mjc/trust.py` | 信任分/降级（P3.3）：连续一致 Judge 降频 |
| webui | `mjc/webui.py` | Web 界面（审查台+后台管理+纠正过程）：127.0.0.1:8123，页面资产 mjc/assets/ |
| autocheck | `mjc/autocheck.py` | 自动审查核心 auto_review + cmd_auto/scan（P4 从 cli 拆出） |
| scan | `mjc/scan.py` | 转录扫描触发源（webchat 无 message:sent 的替代通道） |
| dispositions | `mjc/dispositions.py` | 审查意见处置记录（纠正过程可视化数据源） |
| paths | `mjc/paths.py` | 公共路径常量（消除循环依赖） |
| cli | `mjc/cli.py` | CLI 薄壳（review/judge-only/auto/scan/dispose/webui/doctor） |

## Phase 3：成本优化（初筛 / 缓存 / 信任降级）

```bash
# 初筛：glm-4-flash 先审，pass+置信≥0.75 直接放行免委员会（review 默认开，judge-only 需 --screen）
python3 -m mjc.cli review --task "..."                      # 默认走初筛
python3 -m mjc.cli judge-only --task "..." --output "..." --screen
python3 -m mjc.cli review --task "..." --no-screen          # 关初筛
MJC_SCREEN_MODEL=qwen:qwen3-turbo python3 -m mjc.cli review --task "..."  # 换初筛模型

# 缓存（默认开，--no-cache 关）：同 task+output 重跑直接命中，0 API
# 降级（--degrade）：trust.json 中连续一致≥5 次的 Judge 本轮跳过；双 Judge 分歧自动升级回满委员会
python3 -m mjc.cli review --task "..." --degrade
python3 -m mjc.cli doctor   # 可看信任分与缓存条目
```

## 辩论规则（仲裁器）

- ≥2/3 pass → 通过
- ≥2/3 reject → 打回重写
- 1:1:1 或高置信(≥0.7)少数派反对 → 触发第 2 轮交叉辩论（每个 Judge 看到其他 Judge 意见后改判）
- 辩论 ≤2 轮；打回 ≤3 次；超限强制输出标记低置信度

## 测试

```bash
python3 tests/test_hallucination.py   # 9 样本幻觉验收（识别率/误杀率）
python3 tests/test_debate.py          # P2.5 辩论增益（单轮 vs 辩论，--set f-bench）
python3 tests/test_settings.py        # settings/probe/apply 离线（零 API）
python3 tests/test_webui_api.py       # Web HTTP API 离线（monkeypatch，零 API）
python3 tests/test_scan.py            # 转录扫描器离线（零 API）
python3 tests/test_autocheck.py       # 自动审查核心离线（零 API）
python3 tests/test_dispositions.py    # 处置记录离线（零 API）
python3 tests/test_phase3.py --api-screen   # P3.1 初筛基准（真 API，成本受控）
python3 tests/test_phase3.py --api-degrade # P3.3 信任降级（真 API）
```

## 可用模型（2026-09-06 实测）

| 厂商 | 模型 | 状态 |
|---|---|---|
| DeepSeek | deepseek-v4-flash / deepseek-chat | ✅ |
| 智谱 GLM | glm-4-plus / glm-4-flash / glm-4.5 | ✅ |
| 阿里 qwen3.8-max | token-plan API 直连 | ❌ 403 Unpurchased（OpenClaw 专属通道可用） |
| 阿里 qwen-plus/max | dashscope 标准 key | ❌ 欠费 |

## v0.3 代码交付工作流（方案 B + 方案 2 + 纠正可视化）

- **强制工作流**（AGENTS.md）：每个编码任务收尾 → `auto --kind code` 初审 → **逐条处置**（客观错误当场修+重跑测试；纯风格不改但写明理由）→ reject/need_human 修后复审 ≤1 轮 → `dispose` 留痕 → 交付附【MJC 审查】报告行 + 【本批 MJC】自检字段
- **纠正过程可视化**：后台管理新增「🧭 纠正过程」页——动画回放审查节点逐个提意见 → 主 agent 逐条答复（✅采纳·已修 / ❌未采纳+理由），数据源 `logs/auto/dispositions.jsonl`
- 记忆链：Mnemosyne（优先）→ OpenClaw 官方（无 search CLI 占位跳过）→ 第三方（未配置）；每条结果记 memory.primary

## 文献对照（2026-09-06 网核版，Crossref+DBLP 验证，arxiv 被墙部分标注）

| 方向 | 论文 | 出处（网核） | 与 MJC 的关系 |
|---|---|---|---|
| 多 Agent 辩论 | Du et al., Multiagent Debate | ICML 2024 | 辩论轮机制源头 |
| 多 Agent 辩论 | Liang et al., Divergent Thinking | EMNLP 2024 | 跨厂商=天然视角多样性 |
| 多 Agent 辩论 | Chan et al., ChatEval | ICLR 2024 | 最接近的评审团工作（我们=结构化+纯规则仲裁） |
| LLM 裁判 | Zheng et al., MT-Bench | NeurIPS 2023 | 裁判偏见警示（位置/冗长） |
| LLM 裁判 | Wang et al., Not Fair Evaluators | ACL 2024 | 规则仲裁器（不用 LLM 仲裁）的背书 |
| 幻觉检测 | Manakul et al., SelfCheckGPT | EMNLP 2023 | 独立审查理念（我们升级为跨厂商） |
| 幻觉检测 | Farquhar et al., Semantic Entropy | Nature 2024 | 共识即停的学术版 |
| 幻觉检测 | Kadavath et al., Know What They Know | NeurIPS 2022 ⚠️ | 置信度校准（我们=经验桶统计） |
| 省钱路由 | Chen et al., FrugalGPT | arXiv 2023 ⚠️ | 初筛+三档位=级联路由 |
| 选择性预测 | Geifman & El-Yaniv | NeurIPS 2017 | 初筛 conf 阈值=拒绝升级 |
| RAG 忠实 | Es et al., ARES | NAACL 2024 | 少量标签校准思路（PPI） |
| RAG 忠实 | Dziri et al., FaithDial | TACL 2022 | 证据锚定方向（暂缓项） |

> 注：外部推荐单里 5 处出处有误已修正（Liang=EMNLP2024 / FaithDial=TACL2022 / Wang=ACL2024 / LayerSkip=ACL2024 / Selective Prediction 应为 Geifman&El-Yaniv 2017）。完整核验记录见 ADVICE-REVIEW.md。

## OpenClaw 集成（可选，非核心）

`autocheck/scan/memctx/live` 与 WebUI 实时任务流同时支持作为 OpenClaw 网关的自动审查链路：
回复转录扫描（`scan`）、Mnemosyne 记忆上下文（`memctx`，优先级 Mnemosyne→官方→第三方，缺失自动降级为空）、
编码任务阶段闸门（`gate`，退出码阻断 + WebUI 实时动画）。不使用 OpenClaw 也能独立运行 CLI 与 WebUI。

## 开源说明

- 密钥只走环境变量（`MJC_DEEPSEEK_KEY` / `MJC_GLM_KEY` / `MJC_<厂商>_KEY`）或本地的 `keys.local.json`（已 gitignore）
- 配置模板见 `settings.example.json`；运行时配置 `settings.json` 不入库
- 全部零第三方依赖（仅 Python 标准库），key 自备，纯本地调用

## License

GPL-3.0 · 纯本地调用 · key 自备
