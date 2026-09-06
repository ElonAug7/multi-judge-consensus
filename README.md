<p align="center">
  <img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="License"/>
  <img src="https://img.shields.io/badge/python-3.9%2B-3776AB.svg" alt="Python"/>
  <img src="https://img.shields.io/badge/dependencies-zero-4CAF50.svg" alt="Zero dependencies"/>
  <img src="https://img.shields.io/badge/offline%20tests-10%20suites-green.svg" alt="Tests"/>
  <img src="https://img.shields.io/badge/recall-1.0%20on%20red--team%20bench-yellow.svg" alt="Bench recall"/>
</p>

<h1 align="center">🦉 Multi-Judge Consensus（MJC）</h1>
<p align="center"><b>让多个国产大模型组成"审查委员会"，交叉审查你的 Agent 输出 —— 从架构层面压制幻觉</b></p>
<p align="center"><i>纯 Python 标准库 · 零第三方依赖 · key 自备 · 本地可控 · 大厂白菜价模型可用</i></p>

---

## 为什么需要它

你的 Agent 输出越来越长、越来越"像真的"，但幻觉防不胜防。**单一模型的自查存在盲区**——它看不见自己的错误。

MJC 的思路：**不同厂商模型的训练数据与架构不同，盲区也不同**。DeepSeek、GLM 对同一段含幻觉文本独立审查时，可能双双以 0.98+ 置信度识别出矛盾——**单一模型会漏的幻觉，委员会很难集体瞎**。

## ✨ 核心亮点

| | |
|---|---|
| 🗳️ **三模型委员会 + 纯规则仲裁** | 不用 LLM 做最终裁决（避免引入新幻觉源），结构化 JSON 意见 → 多数投票 → 分歧触发交叉辩论（≤2 轮，共识即停） |
| 🔍 **确定性验证器** | 日期跨度 / 百分比基数 / 显式求和 100% 可验算错误由规则层**零成本秒拦**——LLM 心算不靠谱的活交给代码 |
| 💰 **白菜价路由** | 初筛（1 次调用 ≈¥0.002）→ 双白菜 → 旗舰委员会，三级只升不降；同内容重跑 0 成本（缓存） |
| 🖥️ **Apple 风 Web 管理台** | 审查台 / 实时任务流（每票落定实时动画）/ 后台管理（key、模型目录、三档位一键切换） |
| 🔌 **适配任何 Agent** | MCP 服务器（Claude/Cursor 即插即用）+ 子进程 CLI（退出码闸门语义）+ HTTP API + OpenClaw 原生链路 |
| 🧪 **红队基准集** | 21 条对抗样本（跨文档矛盾/时序幻觉/数值陷阱/指令偏离）+ Recall/Precision/F1 历史曲线 |

## 📊 效果验证（2026-09-06 红队基准 v1 实测，真 API）

| 场景 | 缺陷放行率 | 干净内容误杀 |
|---|---|---|
| 无审查直接交付 | **100%**（18/18 原样流出） | — |
| 单模型自查（glm-4-plus） | **11.1%**（漏 2/18） | — |
| **🦉 MJC 全流程** | **0%（18/18 全拦）** | **0/3** |

全量 Recall **1.0** · Precision **1.0** · F1 **1.0**；跨文档矛盾 5/5、时序幻觉 5/5、数值陷阱 5/5、指令偏离 3/3。
拦截分层：确定性验证器 1 条（零 LLM），委员会+辩论 17 条。复现：`python3 -m mjc.cli bench --set v1-full`。

## 🚀 下载即用（三步）

```bash
git clone <repo-url> && cd multi-judge-consensus   # 零依赖：不用 pip install 任何东西
python3 -m mjc.cli setup          # ① 向导录入 API key（deepseek/智谱有免费额度；可跳过）
python3 -m mjc.cli webui          # ② 打开 http://127.0.0.1:8123 管理台
python3 -m mjc.cli judge-only --task "查北京的天气" --output "北京明天有特大暴雨"   # ③ 开审
```

> 没 key 也能玩：确定性验证器、全部界面、离线测试可用；1 家 key 即可跑（委员会自动收缩），2 家以上效果最佳。

## 🧠 一次审查的旅程

```
内容进来
 ├─ ⓪ 确定性验证器    （日期/数值/求和，零 LLM，命中直接打回）
 ├─ ① 缓存查重        （同内容重跑 = 0 成本）
 ├─ ② 初筛            （glm-4-flash，pass 且 conf≥阈值 → 1 次调用放行）
 ├─ ③ 委员会          （3 模型并行独立审查：verdict + confidence + issues[]）
 ├─ ④ 仲裁器          （纯规则投票：≥2/3 pass 过；分歧触发辩论）
 ├─ ⑤ 辩论 ≤2 轮      （互相看意见 → 改判，共识即停）
 └─ ⑥ 终局            （裁决 + tokens + 费用估算 + 全量日志）
```

三档位策略（后台一键切）：🥬 省钱（2×白菜）· ⚖️ 标准（2 白菜+1 精审旗舰）· 💎 精审（旗舰全量，无初筛）

## 🔌 适配其他 Agent

**① MCP（推荐）** —— Claude Desktop/Code、Cursor、Windsurf、Cline：
```bash
python3 -m mjc.mcp
# Claude Code:  claude mcp add mjc -- python3 -m mjc.mcp
# Cursor:       Settings → MCP → Add → Command: python3 -m mjc.mcp
```
**② 子进程 CLI**（任何语言）—— 单行 JSON + 退出码闸门语义：
```bash
python3 -m mjc.cli auto --task "<任务>" --content "<输出>"    # pass 退出码 0
python3 -m mjc.cli gate --stage deliver --task "<任务>" --content "<产物>"  # revise=2 / reject=3
```
**③ HTTP API**（Dify/Coze/n8n 等自定义工具）—— `POST /api/review`，token 可选鉴权。
**④ OpenClaw** —— 自动转录扫描 + 编码任务阶段闸门 + 实时任务流（可选集成）。

## 🏗️ 架构

```
mjc/
├── providers.py      厂商统一调用（deepseek/glm/qwen/dashscope/doubao/kimi 注册表，加 key 即用）
├── judge.py          单审查员（结构化 JSON + 背景记忆注入 + 同厂商替补）
├── arbiter.py        投票/辩论仲裁（error 票中性、共识即停、每票实时事件）
├── pipeline.py       流水线：验证器→初筛→降级→委员会（故障自动降级）
├── verifier.py       确定性验证器（零 LLM：日期/百分比/求和，宁缺毋滥零误报）
├── bench.py          红队基准运行器（Recall/Precision/F1 + 历史曲线 + 辩论消融）
├── webui.py          服务端（审查台 + 实时任务流 + 后台管理，Apple 风毛玻璃 UI）
├── mcp_server.py     MCP stdio 服务器（任意 MCP 客户端接入）
├── autocheck/scan/gate  自动审查与阶段闸门（OpenClaw 可选集成）
└── settings.py       后台配置（厂商/模型目录 🥬💰💎/三档位/探测）
tests/                10 套离线测试（零 API，CI 友好）
samples/bench-v1.json 红队基准样本
```

## 🧪 测试与质量

```bash
python3 tests/test_phase3.py         # 流水线/缓存/降级（离线）
python3 tests/test_verifier.py       # 确定性验证器 15 断言
python3 tests/test_mcp.py            # MCP 协议
# …共 10 套，全部离线零 API；无 key 环境自动跳过 key 依赖用例（CI 友好）
```

## 🗺️ 路线图

- [x] 委员会 + 辩论（ICML 2024 Multiagent Debate 工程化，共识即停）
- [x] 验证器 / 缓存 / 三档位路由（FrugalGPT 级联思想）
- [x] 后台管理 + 实时任务流 + 纠正过程可视化（dispositions 处置留痕）
- [x] MCP / CLI / HTTP 三路 Agent 适配 + 红队基准
- [ ] 置信度校准桶驱动的自适应阈值（数据积累中）
- [ ] 更多厂商白菜模型接入（qwen3-turbo 等，槽位已备）

## 📜 开源说明

- **密钥安全**：key 只走环境变量（`MJC_<厂商>_KEY`）或本地 `keys.local.json`（600 权限，已 gitignore）；配置模板 `settings.example.json`
- 审查记录/日志/运行时配置全部本地、不入库
- 文献对照与设计取舍见 README 各节；效果数据全部真 API 实测可复现
- License: **GPL-3.0** · 纯本地调用 · key 自备 · 兴趣驱动，不为任何厂商背书

<p align="center"><i>如果 MJC 帮你拦住了一次线上事故级别的幻觉 —— 点个 ⭐ 就是最好的支持</i> 🦉</p>
