#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · revision.py — 修订守则（零幻觉架构 · 修复环节规范）

背景（2026-09-12 pilot 实测教训）：
  - csqa-02/07：审查检出错误后，生产者按“建议”替换成另一个错误值 → 幻觉在“修复”环节被放大
  - csqa-03：审查误报时，生产者顺从改写正确答案 → 误杀被放大
原则：审查意见只是线索，不是事实；宁可存疑，不许编造。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REVISION_SYSTEM_PROMPT = (
    "你是严谨的修订者。请依据审查意见修订答案，并严格遵守《修订守则》：\n"
    "1) 审查意见只是线索，不是事实——只修改你独立确认为错误的内容；\n"
    "2) 不得引入原答案与审查意见之外的新具体事实（数字、年份、日期、人名、地名、作品名、机构名）；\n"
    "3) 需要替换具体事实时，仅当你确信正确值时才替换；审查员的建议值未经确认（或你无法确信）时，不得采用，也不得另猜一个；\n"
    "4) 无法确认时，删去或弱化该断言（如“据记载/存在不同说法”），宁可存疑，绝不编造；\n"
    "5) 只输出修订后的完整最终答案，不要解释。"
)


def build_messages(task, prev, feedback, extra_rules=""):
    """构造修订消息（OpenAI 兼容格式）。extra_rules: 追加的临时守则（如仲裁结果提示）。"""
    sys_p = REVISION_SYSTEM_PROMPT + (("\n" + extra_rules) if extra_rules else "")
    usr = f"【题目】\n{task}\n\n【上一版答案】\n{prev}\n\n【审查意见】\n{feedback}"
    return [{"role": "system", "content": sys_p}, {"role": "user", "content": usr}]


def arbitration_note(items):
    """把仲裁结果转成给修订者的提示文本：只强调“未确认的不得替换”，并展示仲裁员独立答案。"""
    if not items:
        return ""
    lines = ["【事实仲裁结果】（独立复核，供参考）"]
    for it in items:
        out = it.get("outcome")
        desc = (it.get("desc") or "")[:100]
        if out == "confirmed":
            tag = "已确认"
        elif out == "refuted":
            tag = "已否证（该审查意见不成立，务必保留原内容）"
        else:
            tag = "未确认（不得据此替换具体事实，仅可弱化表述）"
        extra = ""
        answers = [v.get("my_answer") for v in (it.get("votes") or [])
                   if v.get("my_answer") and v.get("my_answer") not in ("不确定", "unknown", "?")]
        if answers:
            uniq = []
            for a in answers:
                if a not in uniq:
                    uniq.append(a)
            extra = "（仲裁员独立答案：" + "；".join(uniq[:2]) + "）"
        lines.append(f"- [{tag}] {desc}{extra}")
    return "\n".join(lines)
