"""正文字数压缩（shrink）纯函数 —— 对齐 OpenBidKit 的 buildWordAdjustmentMessages / applyWordAdjustmentOperations

背景：本软件正文生成只有"扩写"（_continue_if_needed），超字数仅标记 word_status='over'，
无任何缩减机制。OpenBidKit 有独立的缩写修复器，核心设计（本模块逐条对齐）：

1. 操作模型：AI 只返回 ``replace`` / ``delete`` 两种局部操作（target_text 逐字唯一），
   严禁整篇重写 —— 避免压缩时破坏图表/结构/事实数据；
2. 确定性保护区间：代码围栏（```mermaid / ```chart-json / 代码块）、GFM 表格、
   图片（``![]()`` / ``<img>``）的字符区间不允许任何操作触碰（OpenBidKit 的
   collectProtectedContentRanges + rangeOverlaps 双保险中的代码级一环）；
3. 校验即拒绝：target_text 不存在 / 出现多次 / 命中保护区间 → 整轮拒绝（ValueError），
   不做部分应用，保证正文不会被半途修改；
4. 轮次封顶与无进展退出由调用方（路由）控制，本模块只负责单轮解析与应用。

字数口径与全项目一致：``content_utils.text_word_count``（剔除围栏代码块）。
"""
from __future__ import annotations

import json
import logging
import re

from app.services.content_utils import text_word_count

logger = logging.getLogger("content_shrink")

# ✅ 常量唯一来源（2026-09-16）：原先定义在 routers/sections.py，正文生成链路的
#    「生成后自动压缩」需要同一套参数，故上移到服务层，路由改为 import。
SHRINK_MAX_ROUNDS = 3          # 单次压缩最多轮数（每轮一次 AI 调用）
SHRINK_SETTLE_RATIO = 1.15     # 收敛判定：字数降到目标的 1.15 倍以内即停

# 围栏代码块（```lang ... ```）：图表（mermaid / chart-json / ai_image）与普通代码块
_FENCE_RE = re.compile(r"```[\s\S]*?```")
# Markdown 图片 ![alt](url) 与内联 <img ...>
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# GFM 表格行（行首可选空白 + |）
_TABLE_LINE_RE = re.compile(r"^\s*\|")


def collect_protected_ranges(content: str) -> list[tuple[int, int]]:
    """收集正文中的"压缩禁区"字符区间（代码围栏 / 图片 / GFM 表格）。

    对齐 OpenBidKit ``collectProtectedContentRanges``：代码围栏 + 表格 + 图片。
    表格按"连续表格行"合并为一个区间（含表头分隔行），避免逐行区间被
    跨行 target_text 的碎片命中绕过。
    """
    if not content:
        return []
    ranges: list[tuple[int, int]] = []
    for pattern in (_FENCE_RE, _IMAGE_MD_RE, _IMG_TAG_RE):
        for m in pattern.finditer(content):
            ranges.append((m.start(), m.end()))

    # GFM 表格：连续表格行合并成块区间
    lines = content.split("\n")
    offset = 0
    table_start = -1
    table_end = -1
    for line in lines:
        line_len = len(line) + 1  # +1 为换行符
        if _TABLE_LINE_RE.match(line):
            if table_start < 0:
                table_start = offset
            table_end = offset + len(line)
        else:
            if table_start >= 0:
                ranges.append((table_start, table_end + 1))
                table_start = -1
        offset += line_len
    if table_start >= 0:
        ranges.append((table_start, table_end + 1))
    return ranges


def _overlaps_protected(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < r_end and end > r_start for r_start, r_end in ranges)


def parse_shrink_operations(raw: str) -> list[dict]:
    """解析 AI 返回的压缩操作 JSON。

    兼容围栏包裹 / 前后缀噪声；只接受 ``replace``（须含 content）与 ``delete``。
    解析失败或无有效操作时抛 ``ValueError``（由调用方决定终止或重试）。
    """
    if not raw or not raw.strip():
        raise ValueError("压缩返回为空")
    text = raw.strip()
    # 剥离 ```json 围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    obj = None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # 兜底：截取第一个 { 到最后一个 } 之间的内容
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last > first:
            try:
                obj = json.loads(text[first:last + 1])
            except (json.JSONDecodeError, TypeError):
                obj = None
    if not isinstance(obj, dict):
        raise ValueError("压缩返回不是合法 JSON 对象")
    ops_raw = obj.get("operations")
    if not isinstance(ops_raw, list) or not ops_raw:
        raise ValueError("压缩返回缺少 operations")
    ops: list[dict] = []
    for item in ops_raw:
        if not isinstance(item, dict):
            continue
        operation = str(item.get("operation", "")).strip().lower()
        target = str(item.get("target_text", ""))
        if operation not in ("replace", "delete"):
            # ✅ 严禁 rewrite_full / insert 等越权操作（对齐 OpenBidKit 白名单）
            continue
        if not target.strip():
            continue
        if operation == "replace":
            content = str(item.get("content", ""))
            ops.append({"operation": "replace", "target_text": target, "content": content})
        else:
            ops.append({"operation": "delete", "target_text": target, "content": ""})
    if not ops:
        raise ValueError("压缩返回无有效操作（仅允许 replace/delete）")
    return ops


def apply_shrink_operations(content: str, ops: list[dict]) -> str:
    """把压缩操作应用到正文（全有或全无：任一操作非法即抛 ValueError）。

    校验规则（对齐 OpenBidKit applyWordAdjustmentOperations）：
    - target_text 必须在当前正文中**逐字出现且仅出现一次**；
    - 操作区间不得与保护区间（代码围栏/表格/图片）重叠 —— 命中即拒绝，
      错误信息与 OpenBidKit 同口径："字数调整不能修改图片、Mermaid、代码块或表格"。
    """
    if not ops:
        raise ValueError("无操作可应用")
    ranges = collect_protected_ranges(content)
    # 先全部定位校验（对原始 content 的偏移），再按区间起点倒序应用，
    # 保证前面的替换不影响后面操作的偏移量。
    located: list[tuple[int, int, dict]] = []
    for op in ops:
        target = op["target_text"]
        first = content.find(target)
        if first == -1:
            raise ValueError("操作目标在正文中不存在（target_text 未逐字匹配）")
        second = content.find(target, first + 1)
        if second != -1:
            raise ValueError("操作目标在正文中出现多次（target_text 不唯一）")
        end = first + len(target)
        if _overlaps_protected(first, end, ranges):
            raise ValueError("字数调整不能修改图片、Mermaid、代码块或表格")
        located.append((first, end, op))
    located.sort(key=lambda x: x[0], reverse=True)
    result = content
    for start, end, op in located:
        result = result[:start] + op.get("content", "") + result[end:]
    return result


async def shrink_content_rounds(
    content: str,
    *,
    word_budget: int,
    prompt_factory,
    call_ai,
    max_rounds: int = SHRINK_MAX_ROUNDS,
    settle_ratio: float = SHRINK_SETTLE_RATIO,
    on_round=None,
) -> dict:
    """多轮字数压缩主循环（手动「压缩本章」端点与正文生成后的自动压缩共用）。

    ✅ 设计（2026-09-16）：把「提示词渲染」与「AI 调用」作为参数注入，本模块因此
    不依赖 provider / prompts 层 —— 既可被两个调用方复用（避免把 50 行轮次逻辑
    抄两遍），也能用假实现做纯单元测试。

    Args:
        content: 待压缩正文。
        word_budget: 目标字数。
        prompt_factory: ``(round_no, current_wc) -> system prompt``；抛异常即终止压缩。
        call_ai: ``async (system_prompt, user_payload) -> str``（AI 原始返回）。
        max_rounds: 轮次上限（每轮一次 AI 调用）。
        settle_ratio: 收敛判定倍数（字数 ≤ budget × ratio 即停）。
        on_round: 可选 ``async (round_no, current_wc)`` 回调，用于进度上报；
            回调异常不影响压缩。

    Returns:
        ``{"content","before","word_count","rounds_used","stop_reason","applied"}``
        ``applied=False`` 表示正文未发生任何变更（调用方自行决定报错或忽略）。
    """
    current = content
    before = text_word_count(current)
    out = {
        "content": current, "before": before, "word_count": before,
        "rounds_used": 0, "stop_reason": "已达轮次上限", "applied": False,
    }
    if before <= 0 or not (content or "").strip():
        # ✅ 纯空白内容同样视为无可压缩（text_word_count 按字符计数，会把空格算进去）
        out["stop_reason"] = "章节正文为空"
        return out

    current_wc = before
    for round_no in range(1, max_rounds + 1):
        if on_round is not None:
            try:
                await on_round(round_no, current_wc)
            except Exception:      # 进度上报失败绝不影响压缩
                logger.debug("压缩进度回调异常（已忽略）", exc_info=True)
        try:
            sys_prompt = prompt_factory(round_no, current_wc)
        except Exception as e:
            logger.warning("压缩提示词渲染失败: %s", e)
            out["stop_reason"] = f"提示词渲染失败: {e}"
            break
        user_payload = (f"当前字数 {current_wc}，目标 {word_budget} 字。"
                        "请输出压缩操作 JSON（不要输出正文本身）：\n\n" + current)
        try:
            raw = await call_ai(sys_prompt, user_payload)
        except Exception as e:
            logger.warning("压缩第 %d 轮 AI 调用失败: %s", round_no, e)
            out["stop_reason"] = f"AI 调用失败: {e}"
            break
        out["rounds_used"] = round_no
        try:
            ops = parse_shrink_operations(raw)
            new_content = apply_shrink_operations(current, ops)
        except ValueError as e:
            logger.warning("压缩第 %d 轮操作被拒绝: %s", round_no, e)
            out["stop_reason"] = f"第 {round_no} 轮操作校验未通过：{e}"
            break
        new_wc = text_word_count(new_content)
        if new_wc >= current_wc:
            out["stop_reason"] = "无进展（压缩未减字）"
            break
        current, current_wc = new_content, new_wc
        out["content"] = current
        out["word_count"] = current_wc
        if current_wc <= word_budget * settle_ratio:
            out["stop_reason"] = "已收敛至目标区间"
            break
    else:
        out["stop_reason"] = f"已达 {max_rounds} 轮上限"

    out["applied"] = current != content and out["word_count"] < before
    return out
