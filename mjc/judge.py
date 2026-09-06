#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · judge.py — 单个 Judge 的结构化审查
每 Judge 独立阅读 Agent 输出 → 输出 JSON：
  { verdict: pass|reject|revise, confidence: 0-1,
    issues: [{type, location, description, suggestion}],
    final_reasoning: str }
"""
import json
import re

from . import providers

JUDGE_PROMPT = """你是一个独立审查员，属于多模型审查委员会的一员。你的任务是对 Agent 生成的内容进行严格审查，识别事实错误、逻辑错误、幻觉和风格问题。

【原始任务】
{user_task}

【Agent 输出】
{agent_output}

{memory_section}{other_opinions_section}
请输出你的审查意见，必须是合法 JSON（不要用 markdown 代码块包裹），格式：
{{
  "verdict": "pass 或 reject 或 revise",
  "confidence": 0.0 到 1.0,
  "issues": [
    {{
      "type": "factual_error 或 logical_error 或 hallucination 或 style",
      "location": "定位（如：第2段/第3句/第2条建议）",
      "description": "具体问题描述",
      "suggestion": "修改建议（可选）"
    }}
  ],
  "final_reasoning": "简要说明你的最终判断理由"
}}

判断标准：
- pass：内容正确、逻辑自洽、无虚构（可有个别无伤大雅的风格瑕疵）
- revise：内容基本正确，但存在需要修改的具体问题——影响正确性或可用性的细节错误，或会让读者误解的表述。
  注意：仅"可以写得更好/更专业/换种说法"的偏好、语气或排版风格差异，不属于 revise 的理由。
- reject：存在事实错误、幻觉（编造不存在的 API/数据/事件）或严重逻辑错误

注意：涉及任何数值（百分比、相对增幅、日期推算、单位换算、求和、乘法）时，请先独立验算确认错误属实再下结论；把正确计算误判为错误属于严重失误。
规划/设计类内容中明确标注的假设、口径与系数（如"按 1.2 倍冗余规划"、"办公流量约占 30%"、"按十进制 1GB=1000MB"）属于作者的工程选择，只要与正文自洽、内部无矛盾，就不得仅因"假设缺乏依据"或"我会选别的参数/写法"而 revise/reject。revise 只用于客观错误或确实会让读者误解的内容。
即使你是唯一发现问题的人，只要问题真实存在就请坚持判断并说明理由。

{memory_rules}"""


MEMORY_RULES = """
【背景记忆的使用规则】
- 背景记忆是外部检索到的历史记录（可能是与本任务相关的旧事实），只作参考线索，**不是金标准**：它可能过时、被推翻或与本次任务无关。
- 若 Agent 输出与背景记忆冲突：先判断输出内部是否自洽、有无独立证据；输出本身准确而记忆过时 → 不算错误。
- 若背景记忆能佐证输出中的具体事实（人名/日期/数据/结论），且记忆来源可信（如用户本人之前的明确陈述）→ 可降低对相应内容的怀疑。
- 输出中编造了与记忆相悖且无法自证的细节 → 按 hallucination/factual_error 处理。
"""


def _memory_section(memory):
    """背景记忆 → prompt 段落。memory: [{text, source, date, relevance}]"""
    if not memory:
        return "", ""
    lines = []
    for i, m in enumerate(memory, 1):
        src = m.get("source", "?")
        date = (m.get("date") or "")[:10]
        lines.append(f"{i}. 【{src}·{date}】{m.get('text', '')[:300]}")
    section = "【背景记忆（外部检索，仅参考，可能过时）】\n" + "\n".join(lines) + "\n\n"
    return section, MEMORY_RULES


def extract_json(text):
    """从模型输出里稳健提取 JSON（容忍代码块/前后杂文本）"""
    if not text:
        return None
    # 去 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return None


def _vendor_models(provider):
    """settings 注册表里该厂商的模型列表（懒加载避免循环 import）；异常回退空列表"""
    try:
        from mjc import settings
        meta = (settings.load().get("providers") or {}).get(provider) or {}
        return list(meta.get("models") or [])
    except Exception:
        return []


def _backup_model(provider, model):
    """同厂商替补模型（P1.1）：注册表列表中紧随当前模型的下一个。
    设计：deepseek-v4-flash → deepseek-chat，glm-4-flash → glm-4-plus……
    仅在本模型故障/不可解析时才动用；不在注册表/已是末尾 → None（无替补）。"""
    if not model:
        return None
    models = _vendor_models(provider)
    if model not in models:
        return None
    idx = models.index(model)
    return models[idx + 1] if idx + 1 < len(models) else None


class Judge:
    def __init__(self, provider, model=None, name=None):
        self.provider = provider
        self.model = model
        self.name = name or f"{provider}:{model or providers.MODELS.get(provider, '')}"
        self.display = providers.display_name(provider, model)

    def review(self, user_task, agent_output, other_opinions=None, timeout=90, memory=None):
        """执行一轮审查，返回结构化结果 + 原始响应。memory: [{text,source,date}] 背景记忆
        健壮性（P1.1）：首选模型失败（空响应/5xx 重试耗尽后 raise 或响应不可解析）→
        自动换同厂商替补模型（注册表下一个）再试一次；无替补则同模型重试（保持原行为）。
        预算至多 2 次 chat 调用；认证类（401/403）换模型无意义 → 直接抛给上层记 error 票。"""
        memory = memory if memory is not None else getattr(self, "memory", None)
        opinions_section = ""
        if other_opinions:
            opinions_section = "【其他审查员的意见】（请参考，但独立判断，不盲从）：\n" + json.dumps(
                other_opinions, ensure_ascii=False, indent=1
            )
        mem_section, mem_rules = _memory_section(memory)
        prompt = JUDGE_PROMPT.format(
            user_task=user_task,
            agent_output=agent_output,
            memory_section=mem_section,
            other_opinions_section=opinions_section,
            memory_rules=mem_rules,
        )
        primary = self.model or providers.MODELS.get(self.provider)
        backup = _backup_model(self.provider, primary)
        candidates = [primary] + ([backup] if backup and backup != primary else [primary])
        last_response, call_err, parsed, used = None, None, None, None
        for m in candidates:
            try:
                last_response = providers.chat(
                    self.provider,
                    [{"role": "user", "content": prompt}],
                    model=m, temperature=0.2, max_tokens=2000, timeout=timeout,
                )
            except Exception as e:
                call_err = e
                s = str(e)
                if "HTTP 401" in s or "HTTP 403" in s:
                    raise  # 认证/权限类：换模型无意义，直接失败
                continue  # 瞬时故障/空响应 → 换替补模型或同模型重试
            used = m
            parsed = extract_json(last_response)
            if parsed:
                break
            # 有响应但不可解析 → 记录后尝试下一个候选（与原逻辑一致：error dict 而非 raise）
        if parsed:
            parsed["judge_id"] = self.name
            parsed["judge_display"] = self.display
            if backup and used == backup:
                parsed["model_used"] = used  # 替补成功标记（日志/报告可查兜底是否发生）
            return parsed
        if call_err is not None and last_response is None:
            raise call_err  # 从未拿到响应（全部候选调用失败）→ 抛给上层记 error 票/跳初筛
        detail = (last_response or str(call_err) or "")[:200]
        parsed = {"verdict": "error", "confidence": 0, "issues": [], "final_reasoning": f"解析失败，原始响应: {detail}", "_parse_fail": True}
        parsed["judge_id"] = self.name
        parsed["judge_display"] = self.display
        return parsed

    def __repr__(self):
        return f"<Judge {self.display}>"


def build_pool(providers_list):
    """providers_list: ['qwen','deepseek',...] 或 ['deepseek:deepseek-v4-flash','glm:glm-4-flash',...] → [Judge]
    'provider:model' 语法支持同厂商多模型（P2.4 三票池）；厂商以 settings 注册表 + 内置 BASE 为准"""
    known = set(providers.BASE) | set(providers.registry_providers())
    pool = []
    for spec in providers_list:
        p, m = (spec.split(":", 1) if ":" in spec else (spec, None))
        if p in known and providers.has_key(p) and providers.endpoint_of(p):
            pool.append(Judge(p, model=m or None))
    return pool
