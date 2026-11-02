"""目录结构合规修复（R162 · 层级硬性标准）——AI 补齐/精简服务。

判据单一事实源 = app.services.outline_rules（层级 L1~L4、L1 下 3~5 个 L2、
L2 下 3~5 个 L3、L3 为最小叶子、严禁 L5）；本模块只负责「把违规清单 + 现有目录交给
AI，拿回修复后的完整目录」，不自带判据。

消费方：
  - 目录生成主链路（sse_handlers.generate_outline）：生成后不合规时的
    补齐/精简轮（R151 用户指令 二.2/二.3，R162 起仅 L1/L2 数量违规会触发）；
  - 目录标准化修复端点（sections.standardize-outline）：存量树的一次性
    AI 标准化（用户指令 五「旧数据清洗方案」的 AI 面）。

AI 调用范式与 outline_title_fix 同款：render + collect_json_response +
OutlineResponse schema + 超时/异常 fail-soft（返回 None 由调用方决策）。

⚠️ R162 加固（2026-11-01 取证：生产任务 0ed46f6d 合规修复第 1 轮 120s 超时
即终局 failed，且 182 节点树 JSON 约 3~4 万字符被截断到 12000 后 AI 根本
看不到完整树）：① 超时不再立即终局——进入下一轮（仍以
MAX_COMPLIANCE_FIX_ATTEMPTS 封顶）；② 现有目录注入上限 12000 → 48000。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import settings
from app.services.ai.json_response import OUTLINE_REPAIR_KEY, collect_json_response
from app.services.ai.outline_schema import OutlineResponse
from app.services.ai.prompts._limits import (
    PROJECT_FACTS_LIMIT_OUTLINE,
)
from app.services.ai.prompts._registry import render
from app.services.outline_rules import (
    violations_to_payload,
)

logger = logging.getLogger("outline_compliance")

OUTLINE_FIX_TIMEOUT = max(10, int(settings.outline_fix_timeout))
OUTLINE_FIX_MAX_TOKENS = int(settings.outline_fix_max_tokens) or None

#: 单次合规修复的 AI 轮数上限（每轮内部还有 collect_json_response 的 JSON 修复重试）
MAX_COMPLIANCE_FIX_ATTEMPTS = 2

#: 现有目录注入提示词的字符上限（R162：12000 → 48000。182 节点树 JSON
#: 约 3~4 万字符，旧上限下修复 AI 看到的是截断残树，大树修复结构性失效）
OUTLINE_FIX_CONTEXT_CHARS = 48000


def build_violations_brief(violations) -> str:
    """违规清单 → 提示词用的逐条文本（带序号，AI 逐条消除）。"""
    lines = []
    for i, v in enumerate(violations_to_payload(violations), start=1):
        loc = " > ".join(p for p in (*v.get("path", [])[-2:], v.get("title", "")) if p)
        lines.append(f"{i}. [{v.get('code')}] {loc}：{v.get('detail')}（要求 {v.get('expected')}，实际 {v.get('actual')}）")
    return "\n".join(lines) if lines else "（无）"


def build_compliance_fix_messages(outline, violations, *, scheme_name: str = "",
                                  scheme_type: str = "",
                                  construction_scope: str = "",
                                  project_brief: str = "",
                                  project_facts: str = "") -> list[dict]:
    """构造合规修复调用消息（与 outline_title_fix 同款：system 单条）。"""
    prompt = render(
        "outline_compliance_system",
        scheme_name=scheme_name,
        scheme_type=scheme_type,
        construction_scope=construction_scope,
        project_brief=(project_brief or "")[:1500],
        project_facts=(project_facts or "")[:PROJECT_FACTS_LIMIT_OUTLINE],
        violations_text=build_violations_brief(violations),
        current_outline=json.dumps(outline, ensure_ascii=False)[:OUTLINE_FIX_CONTEXT_CHARS],
    )
    return [{"role": "system", "content": prompt}]


def _structure_validate_fn(obj) -> list[str]:
    """修复结果的结构校验（不判合规——合规由调用方用 outline_rules 复检）。"""
    ol = obj.get("outline") if isinstance(obj, dict) else None
    if not isinstance(ol, list) or not ol:
        return ["outline 为空或格式错误"]
    issues: list[str] = []

    def _check(nodes, depth=0):
        if depth > 12:
            return
        for node in nodes:
            if not isinstance(node, dict):
                issues.append(f"L{depth + 1} 节点不是字典对象")
                continue
            if not str(node.get("title") or "").strip():
                issues.append(f"L{depth + 1} 节点缺少 title 或 title 为空")
            children = node.get("children")
            if children is None:
                continue  # 缺失 children 是合法叶子
            if not isinstance(children, list):
                issues.append(f"L{depth + 1} 节点 {node.get('title', '?')} children 不是列表")
            elif children:
                _check(children, depth + 1)

    _check(ol)
    return issues


async def fix_outline_compliance(
    outline,
    violations,
    *,
    scheme_name: str = "",
    scheme_type: str = "",
    construction_scope: str = "",
    project_brief: str = "",
    project_facts: str = "",
    compliance_check=None,
    collect=None,
):
    """AI 合规修复主入口：返回 (fixed_outline|None, info dict)。

    - 最多 ``MAX_COMPLIANCE_FIX_ATTEMPTS`` 轮；每轮结果先过结构校验，
      再用 ``compliance_check``（默认 outline_rules.validate_outline_nodes）
      复检；合规即返回。
    - 超时进入下一轮（R162 加固，用尽 MAX 才终止）；AI 异常/空产出：终止并
      返回当前最优结果（None 或上一轮产物），
      info.reason 记录终止原因（ai-timeout / ai-error / ai-empty / ai-still-invalid）。
    - fail-soft：绝不抛异常打断调用方主流程，由调用方决定 failed / 降级。

    info 键：attempts / reason / remaining（残留违规数） / ok。
    """
    from app.services.outline_rules import validate_outline_nodes

    _collect = collect or collect_json_response
    _check = compliance_check or validate_outline_nodes
    info = {"attempts": 0, "reason": "", "remaining": -1, "ok": False}
    current = outline
    remaining = list(violations)

    for attempt in range(1, MAX_COMPLIANCE_FIX_ATTEMPTS + 1):
        try:
            messages = build_compliance_fix_messages(
                current, remaining,
                scheme_name=scheme_name, scheme_type=scheme_type,
                construction_scope=construction_scope,
                project_brief=project_brief, project_facts=project_facts)
            info["attempts"] = attempt
            obj, _raw = await asyncio.wait_for(
                _collect(
                    messages,
                    _structure_validate_fn,
                    json_mode=True, temperature=0.2,
                    timeout=OUTLINE_FIX_TIMEOUT,
                    repair_key=OUTLINE_REPAIR_KEY,
                    schema=OutlineResponse, target_desc="合规修复后的完整目录"),
                timeout=OUTLINE_FIX_TIMEOUT)
            fixed = obj.get("outline") if isinstance(obj, dict) else None
        except asyncio.TimeoutError:
            # R162：超时不再立即终局（生产取证 0ed46f6d：第 1 轮 120s 超时即
            # failed，轮数上限形同虚设）——进入下一轮，用尽 MAX 后仍超时才终止
            logger.warning("目录合规修复超时（%ds，第 %d 轮）", OUTLINE_FIX_TIMEOUT, attempt)
            if attempt >= MAX_COMPLIANCE_FIX_ATTEMPTS:
                info["reason"] = "ai-timeout"
                return None, info
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("目录合规修复失败（第 %d 轮）: %s", attempt, e)
            info["reason"] = "ai-error"
            return None, info

        if not isinstance(fixed, list) or not fixed:
            info["reason"] = "ai-empty"
            return None, info

        current = fixed
        remaining = _check(fixed)
        info["remaining"] = len(remaining)
        if not remaining:
            info["ok"] = True
            info["reason"] = "ok"
            return current, info
        if attempt >= MAX_COMPLIANCE_FIX_ATTEMPTS:
            info["reason"] = "ai-still-invalid"
            return current, info

    info["reason"] = info["reason"] or "ai-still-invalid"
    return (current if info["remaining"] >= 0 else None), info
