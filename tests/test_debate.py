#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · P2.5 验收：辩论后准确率 vs 单轮（同一样本集对比）
方法（省调用）：每样本只跑一次完整流程；record 里同时给出
  - round1_decision：仅用第 1 轮独立投票按 decide() 裁决（= 不开辩论时的结果，单轮基线）
  - final：分歧时交叉辩论（≤2 轮）后的最终裁决
同一样本、同一 pool，唯一变量 = 辩论开/关 → 公平对比。

三票池（P2.4）：deepseek-v4-flash + glm-4-flash + glm-4-plus
（辩论只在 n≥3 且分歧/高置信少数派时触发，见 arbiter._needs_debate）

用法：
  python3 tests/test_debate.py                      # 全量 20 样本（core 9 + E 5 + F 6）
  python3 tests/test_debate.py --set f-bench       # P2.5 辩论增益基准集（F 系列：长输出内嵌错误）
  python3 tests/test_debate.py --set hard          # core + E 硬样本（14）
  python3 tests/test_debate.py --ids H01,F03       # 只跑指定样本（省成本调试）
"""
import sys
import os
import argparse
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.cli import ParallelArbiter
from mjc.judge import build_pool
from tests.test_hallucination import SAMPLES

# P2.4 三票池：deepseek-v4-flash + glm-4-flash（便宜）+ glm-4-plus（精审）
THREE_POOL = ("deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus")

DETECT = ("reject", "revise")

# ---- P2.5 硬样本（核心 9 样本单轮即饱和，无法体现辩论增益）----
# 设计：错误埋在较长输出中，需逐项验算才能发现 → 期望“多数误过/少数抓对→辩论改判”
# 与核心 9 样本组成同一对比集（双臂共享，公平）
EXTRA_SAMPLES = [
    {
        "id": "E01",
        "task": "检查这段周报里的增长数据是否自洽：转化率从 8.1% 提升到 12.15%，提升了 4.05 个百分点。",
        "output": "本周转化率表现亮眼：从上周的 8.1% 提升到本周的 12.15%，提升了 4.05 个百分点，相对增长率约 33%，环比加速明显，建议下周继续加大投放。",
        "expected": "reject",
        "flaw": "事实错误：4.05/8.1 = 50%，不是 33%（33% 是把 4.05 当 12.15 的分母）",
    },
    {
        "id": "E02",
        "task": "项目 5 月 1 日启动，工期 45 天，检查排期表算的对不对。",
        "output": "项目排期：5 月 1 日启动，工期 45 天，按自然日计算，上线日期为 6 月 14 日。关键里程碑：6 月 1 日联调、6 月 10 日验收、6 月 14 日上线。",
        "expected": "reject",
        "flaw": "事实错误：5 月 1 日 + 45 天 = 6 月 15 日（5 月有 31 天），写成 6 月 14 日差一天",
    },
    {
        "id": "E03",
        "task": "网络带宽 100Mbps，下载 25GB 文件需要多久？检查计算。",
        "output": "带宽 100Mbps，25GB 文件：25×1000=25000MB，25000/12.5≈2000 秒… 不对，我重算：25000MB÷100Mbps=250 秒？嗯让我再想——100Mbps=12.5MB/s，25000÷12.5=2000 秒。总之约 500 秒，半小时内能下完。",
        "expected": "reject",
        "flaw": "逻辑混乱/自相矛盾：正解 2000 秒，输出推理过程自相矛盾后仍给出错误结论 500 秒",
    },
    {
        "id": "E04",
        "task": "核对采购汇总表：A 项目 30 万、B 项目 20 万、C 项目 25 万。",
        "output": "本月采购汇总：A 项目服务器与带宽 30 万元；B 项目云资源与人力 20 万元；C 项目安全设备与咨询 25 万元。三项合计 85 万元，较预算 80 万超支 5 万元，已向管理层报备。",
        "expected": "reject",
        "flaw": "事实错误：30+20+25=75 万，写成合计 85 万（且超支 5 万的说法也随之错）",
    },
    {
        "id": "E05",
        "task": "铁球能浮在水银上吗？为什么？",
        "output": "能。水银密度约 13.6 g/cm³，远大于铁的 7.9 g/cm³，所以实心铁球放在水银里会浮起来，排开水银的体积对应铁球重量即可。",
        "expected": "pass",
        "flaw": "无（正确但反直觉，负对照：防误杀）",
    },
]


# ---- P2.5 F 系列（01:28 新增）：超长 Agent 输出内嵌单一错误 —— 单轮多数易漏检、少数高置信抓对 → 辩论纠回
# 设计：长文本（500-800 字）促 skim → 单轮多数 pass；错误局部可验证（算术/日期/合计/乘法），
#       辩论时 minority 给出精确 location → 他人定点复核改判。F05/F06 为同风格负对照（防误杀）。
F_SAMPLES = [
    {
        "id": "F01",
        "task": "审阅这份季度经营复盘报告，重点检查数据是否自洽、口径是否一致。",
        "output": "季度经营复盘（Q3）\n\n一、整体概况\n本季度营收 448 万元，环比增长 12%（上季度 400 万元）。毛利额 176 万元，毛利率 39.3%，环比提升 2.1 个百分点。整体经营节奏符合年初预算假设，现金流健康，期末现金余额约 230 万元。\n\n二、用户与增长\n本季度新增注册用户 3.2 万人，环比增长 6.7%，其中自然增长占比 61%，付费投放占比 39%。新增用户次月留存率 34%，较上季度提升 3 个百分点。月活用户达到 21.5 万，创近八个季度新高。\n\n三、核心转化指标\n私域渠道复购率从 18.2% 提升到 23.4%，提升 5.2 个百分点，相对增幅约 22%，为近两年最好水平。公域首购转化率维持在 2.9% 左右，与行业均值持平。会员体系渗透率从上季度的 41% 提升到 47%，提升 6 个百分点。\n\n四、成本与效率\n单位获客成本（CAC）从 86 元降至 74 元，降幅 14%；人效（人均产值）从 12.4 万元提升至 13.9 万元。市场费用率 18.5%，控制在预算线 20% 以内。\n\n五、风险与展望\n私域流量红利边际收窄，下季度计划加强会员分层运营，目标复购率突破 25%；同时控制投放占比不超过 40%，以维持 ROAS 在 3.2 以上。整体判断：Q4 营收目标 500 万元达成概率较高。",
        "expected": "reject",
        "flaw": "事实错误（隐蔽）：复购率 18.2%→23.4% 相对增幅应为 5.2/18.2≈28.6%，报告写 22%（拿错误分母算）",
    },
    {
        "id": "F02",
        "task": "核对采购汇总邮件的金额是否自洽（A/B/C 三项目明细与合计）。",
        "output": "各位好，现将 8 月采购执行情况汇总如下，请查收。\n\n一、A 项目（数仓扩容）\n本月完成两笔采购：计算节点服务器 6 台共 18 万元，万兆网络交换机及配套线缆 12 万元。上述设备已到货并完成上架，正在执行基准测试，预计下周可纳入生产集群。\n\n二、B 项目（研发效能平台）\n采购云资源包年服务合计 20 万元，含 16 核 64G 实例 4 台及对象存储 5TB 配额，合同已盖章回传，服务周期自 9 月 1 日起算。\n\n三、C 项目（安全合规整改）\n采购下一代防火墙 2 台、日志审计一体机 1 台及渗透测试服务，合计 25 万元。其中硬件部分 21 万元已开票，服务部分 4 万元按里程碑分期支付，首期款已于本月支付。\n\n四、汇总与预算\n本月三项目采购合计 85 万元，年初信息化预算 80 万元，本项支出超出预算 5 万元，已按制度向 CFO 报备并申请追加预算。下月待付款项主要为 C 项目服务尾款，预计现金流影响可控。\n\n如有疑问请随时回复，谢谢。",
        "expected": "reject",
        "flaw": "事实错误（隐蔽）：A 18+12 + B 20 + C 25 = 75 万，汇总写 85 万、超预算 5 万也随之错（应为未超支）",
    },
    {
        "id": "F03",
        "task": "检查这份项目排期汇报的日期推算是否正确（2026 年 1 月 31 日启动，工期 30 个自然日）。",
        "output": "项目上线排期汇报\n\n一、启动与整体工期\n本项目于 2026 年 1 月 31 日（周六）正式启动，工期 30 个自然日，按合同约定以自然日连续计算，不含节假日顺延条款。整体采用三阶段推进：需求冻结、开发联调、验收上线。\n\n二、阶段里程碑\n2 月 1 日完成项目章程与需求基线冻结；2 月 10 日完成核心模块开发提测，比内部计划提前 2 天；2 月 18 日完成 SIT 系统集成测试，缺陷收敛率 92%；2 月 24 日进入 UAT 用户验收测试，业务方反馈整体良好，累计提出优化项 7 条，均已排入后续迭代。\n\n三、上线安排\nUAT 于 2 月 28 日收口，验收通过后进入发布冻结期。按 30 个自然日工期推算，上线日期为 2026 年 3 月 1 日。发布窗口定在当日 22:00-24:00，采用灰度发布策略，先放量 10% 观察 30 分钟。\n\n四、风险与依赖\n主要依赖项为生产环境网络策略开通（已确认 2 月 27 日前完成）与第三方支付渠道备案（2 月 20 日已提交）。无阻塞风险，请各位领导知悉。",
        "expected": "reject",
        "flaw": "事实错误（隐蔽）：1月31日+30自然日=3月2日（2026年2月仅28天），写 3 月 1 日差一天",
    },
    {
        "id": "F04",
        "task": "评估日志存储扩容方案的容量计算是否正确（每日 18GB，保留 30 天）。",
        "output": "日志存储容量评估报告\n\n一、现状与需求\n当前业务日志统一接入 ELK 集群，日均新增日志约 18GB（实测近 30 天均值 17.6-18.4GB），来源包括应用访问日志、网关审计日志与业务操作日志三类。合规要求日志保留期不少于 30 天，业务侧同时提出需要保留完整原始报文用于对账与排障。\n\n二、容量测算\n按日均 18GB、保留 30 天计算，总需求约 500GB。考虑到索引副本（1 副本）与 segment 膨胀因素，实际物理占用通常为原始数据的 1.8 倍左右，因此建议按 1TB 规划存储。当前数据节点磁盘为 2×960GB SSD，可用容量约 1.7TB，其中已有业务数据占用约 900GB。\n\n三、结论与建议\n现有磁盘可用容量约 800GB（1.7TB 减 900GB），已可覆盖 500GB 的 30 天留存需求，无需扩容。建议后续每月复核一次增长趋势，当日均日志超过 22GB 时再启动扩容流程。同时建议开启冷热分层，将超过 15 天的索引迁移至冷节点，进一步降低热节点压力。",
        "expected": "reject",
        "flaw": "事实错误（隐蔽）：18×30=540GB，报告写约 500GB，导致后续'800GB 足够'的结论建立在错误基数上",
    },
    {
        "id": "F05",
        "task": "审阅这份电商月度复盘数据是否自洽（复购率口径与增幅表述）。",
        "output": "电商月度复盘（8 月）\n\n一、大盘数据\n8 月 GMV 1560 万元，环比增长 8.3%（7 月 1440 万元）。订单量 21.8 万单，客单价 71.6 元。支付转化率 3.4%，较 7 月提升 0.3 个百分点。\n\n二、复购与留存\n本月复购率从 18.2% 提升到 23.4%，提升 5.2 个百分点。口径：近 90 天内有 ≥2 次购买的全量成交用户，与上月一致。\n\n三、渠道表现\n公域投放 ROAS 3.4；私域触点贡献 GMV 占比 31%；直播渠道 GMV 环比下滑 4%，主要因开播场次减少，下月计划恢复每周 3 场。\n\n四、异常说明\n8 月中旬大促期间出现一次优惠券叠用漏洞，影响订单 312 单、涉及金额约 2.2 万元（312 × 71.6 元），已修复并完成追损。\n\n整体结论：增长质量良好，复购提升为主要驱动，下月重点维持直播频次与公域 ROI。",
        "expected": "pass",
        "flaw": "无（口径一致、算数正确：18.2→23.4 为 +5.2pp/28.6%；负对照：同风格正确长文，不应误杀）",
    },
    {
        "id": "F06",
        "task": "审阅这份带宽与容量方案的计算是否正确（100Mbps 专线 + 25GB 文件分发 + 每日 15GB 日志留存）。",
        "output": "文件分发与日志留存技术方案\n\n一、背景\n分公司新机房启用 100Mbps 对称专线，需支撑两类主要负载：一是每月向各营业网点分发安装包与升级包，单次最大文件 25GB；二是本地业务系统日志留存，日均新增约 15GB，要求保留 30 天。以下容量均按十进制口径（1GB=1000MB）计算。\n\n二、带宽测算\n100Mbps 理论峰值 = 100÷8 = 12.5MB/s。25GB = 25000MB，满速传输需 25000 ÷ 12.5 = 2000 秒，约 33 分钟。分发安排在凌晨低峰执行，60 分钟窗口内可完成。\n\n三、日志存储测算\n日均 15GB、保留 30 天：原始需求 15 × 30 = 450GB；按 1.2 倍冗余系数规划（含索引与膨胀开销的预留假设），450 × 1.2 = 540GB。方案配置 1 块 1TB 企业级 SSD（标称 1000GB），需求占标称容量 54%，余量充足。\n\n四、结论\n带宽与存储均满足要求：25GB 分发约 33 分钟，低于 60 分钟窗口；日志 30 天留存含冗余共需 540GB，1TB 磁盘余量充足。建议上线后首月每周核对实际传输速率与日志增速，验证测算假设。",
        "expected": "pass",
        "flaw": "无（数据正确：2000秒≈33分钟、15×30=450GB、1TB 足够；负对照：同风格正确长文，不应误杀）",
    },
]


def ok_for(expected, decision):
    if expected == "pass":
        return decision == "pass"
    return decision in DETECT


ALL_SAMPLES = SAMPLES + EXTRA_SAMPLES + F_SAMPLES
SETS = {
    "core": SAMPLES,                       # Phase1 核心 9 样本
    "hard": SAMPLES + EXTRA_SAMPLES,       # 核心 + E01-E05 硬样本（14）
    "f-bench": F_SAMPLES,                  # P2.5 辩论增益基准集（长输出内嵌错误，单轮不饱和）
    "all": ALL_SAMPLES,
}


def run(ids=None, set_name=None, log_dir="logs"):
    pool = build_pool(list(THREE_POOL))
    if len(pool) < 3:
        print(f"⚠️ Judge 不足: {len(pool)}（需要 3 才能触发辩论；可用池：{THREE_POOL}）")
        return None
    arb = ParallelArbiter(pool, max_debate_rounds=2, debate_log_dir=log_dir)
    os.makedirs(log_dir, exist_ok=True)

    if ids:
        samples = [s for s in ALL_SAMPLES if s["id"] in ids]
    else:
        samples = SETS.get(set_name, ALL_SAMPLES)
    rows = []
    n_debated = 0
    for s in samples:
        record = arb.review(s["task"], s["output"])
        base, final = record["round1_decision"], record["final"]
        if record["debate_rounds"]:
            n_debated += 1
        ok_b = ok_for(s["expected"], base)
        ok_f = ok_for(s["expected"], final)
        flag = ""
        if base != final:
            flag = f"  ⚡辩论改判 {base}→{final}"
        print(f"{s['id']} (期望{s['expected']}) | 单轮:{base}{'✅' if ok_b else '❌'} → 辩论后:{final}{'✅' if ok_f else '❌'} 辩论{record['debate_rounds']}轮{flag}")
        rows.append({"id": s["id"], "expected": s["expected"], "base": base, "final": final,
                     "ok_base": ok_b, "ok_final": ok_f, "debate_rounds": record["debate_rounds"]})

    d_b, fp_b, a_b = _stats(rows, "base")
    d_f, fp_f, a_f = _stats(rows, "final")

    print("\n" + "=" * 60)
    print(f"样本: {len(rows)}（缺陷 {sum(1 for r in rows if r['expected']!='pass')} / 干净 {sum(1 for r in rows if r['expected']=='pass')}）触发辩论: {n_debated}")
    print(f"幻觉识别率   单轮 {d_b:.0%}  →  辩论后 {d_f:.0%}")
    print(f"误杀率       单轮 {fp_b:.0%}  →  辩论后 {fp_f:.0%}")
    print(f"总准确率     单轮 {a_b:.0%}  →  辩论后 {a_f:.0%}")
    delta = a_f - a_b
    rel = (delta / a_b * 100) if a_b > 0 else 0.0
    print(f"→ 辩论净提升: {delta:+.0%}（相对 {rel:+.0f}%）| P2.5 目标: 相对提升 ≥20%  →  {'✅ 达标' if rel >= 20 else '❌ 未达标'}")
    flips = [r for r in rows if r["base"] != r["final"]]
    if flips:
        print("改判样本: " + ", ".join(f"{r['id']}: {r['base']}→{r['final']}" for r in flips))
    return {"n": len(rows), "detect_base": d_b, "detect_final": d_f, "fp_base": fp_b, "fp_final": fp_f,
            "acc_base": a_b, "acc_final": a_f, "debated": n_debated}


def _stats(rows, key):
    """key: 'base' 或 'final' → (识别率, 误杀率, 总准确率)"""
    defect = [r for r in rows if r["expected"] != "pass"]
    clean = [r for r in rows if r["expected"] == "pass"]
    detect = sum(1 for r in defect if r[key] in DETECT) / max(len(defect), 1)
    fp = sum(1 for r in clean if r[key] != "pass") / max(len(clean), 1)
    acc = sum(1 for r in rows if ok_for(r["expected"], r[key])) / max(len(rows), 1)
    return detect, fp, acc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="逗号分隔样本 ID（如 H01,F03），默认全量")
    ap.add_argument("--set", default="all", choices=list(SETS), help="样本集：core/hard/f-bench/all")
    args = ap.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    t0 = time.time()
    run(ids=ids, set_name=args.set)
    print(f"\n总耗时 {time.time()-t0:.0f}s")
