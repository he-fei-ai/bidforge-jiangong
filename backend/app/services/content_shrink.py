"""正文字数压缩（shrink）纯函数。

背景：本软件正文生成只有"扩写"（_continue_if_needed），超字数仅标记
word_status='over'，无任何缩减机制。本模块提供可复用的缩写修复能力：

1. 操作模型：AI 只返回 ``replace`` / ``delete`` 两种局部操作（target_text 逐字唯一），
   严禁整篇重写 —— 避免压缩时破坏图表/结构/事实数据；
2. 确定性保护区间：代码围栏（```mermaid / ```chart-json / 代码块）、GFM 表格、
   图片（``![]()`` / ``<img>``）、Markdown 列表块不允许任何操作触碰；
3. 校验即拒绝：target_text 不存在 / 出现多次 / 命中保护区间 → 整轮拒绝（ValueError），
   不做部分应用，保证正文不会被半途修改；
4. 轮次封顶与无进展退出由调用方（路由）控制，本模块只负责单轮解析与应用。

字数口径与全项目一致：``content_utils.text_word_count``（剔除围栏代码块）。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata

from app.services.content_utils import fence_spans, text_word_count

logger = logging.getLogger("content_shrink")

# ✅ 常量唯一来源（2026-09-16）：原先定义在 routers/sections.py，正文生成链路的
#    「生成后自动压缩」需要同一套参数，故上移到服务层，路由改为 import。
SHRINK_MAX_ROUNDS = 3  # 单次压缩最多轮数（每轮一次 AI 调用）
SHRINK_SETTLE_RATIO = 1.15  # 收敛判定：字数降到目标的 1.15 倍以内即停

# Markdown 图片 ![alt](url) 与内联 <img ...>
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# GFM 表格行（行首可选空白 + |）
_TABLE_LINE_RE = re.compile(r"^\s*\|")
# Markdown 列表项行首（无序 -/*/+、有序 1./1、/1）、中文括号编号（1）/(1)）。
# ✅ R179（2026-11-05 · 列表样式被拍平）：旧保护区间只含围栏/表格/图片，
#    AI 的 replace/delete 可整段命中列表行 → 列表被压成一段散文（层级丢失、
#    导出成稿出现「1. xxx 2. xxx」连排）。列表是结构元素，与表格同属
#    "压缩禁区"，只允许压缩其周边段落，不允许改写/删除列表行本身。
_LIST_LINE_RE = re.compile(
    r"^\s*(?:[-*+•·]\s+|\d+(?:\.\d+)*[.)．]\s+|\d+(?:\.\d+)*、|[（(]\s*[\d一二三四五六七八九十]+\s*[）)])"
)


# ---------------------------------------------------------------------------
# 保护区间
# ---------------------------------------------------------------------------


def _merge_line_blocks(content: str, is_block_line, is_continuation) -> list[tuple[int, int]]:
    """把满足条件的连续行合并为块区间（行尾换行并入区间）。

    ``is_block_line`` 判定块起始行，``is_continuation`` 判定缩进续行；
    块区间收尾时统一 ``+1`` 吞掉行尾换行，避免跨行 target 从换行处绕过。
    """
    if not content:
        return []
    ranges: list[tuple[int, int]] = []
    offset = 0
    start = -1
    end = -1
    for line in content.split("\n"):
        if is_block_line(line):
            if start < 0:
                start = offset
            end = offset + len(line)
        elif start >= 0 and is_continuation(line):
            end = offset + len(line)
        elif start >= 0:
            ranges.append((start, end + 1))
            start = -1
        offset += len(line) + 1  # +1 为换行符
    if start >= 0:
        ranges.append((start, end + 1))
    return ranges


def _collect_list_ranges(content: str) -> list[tuple[int, int]]:
    """收集 Markdown 列表块的字符区间（连续列表项 + 其缩进续行合并为一块）。

    ✅ R179：列表是不可压缩的**结构**证据 —— 沿用表格的处理方式（连续行合并
    为一个区间），保证跨多行的 target_text 无法以碎片方式绕过保护。列表项之间
    的空行不并入区间，但任何跨块的操作仍会与区间重叠而被拒绝。
    """

    def _is_continuation(line: str) -> bool:
        # 列表项的缩进续行（换行折行）：同样属于列表块，不能被单独改写
        return bool(line.strip()) and (line.startswith(" ") or line.startswith("\t"))

    return _merge_line_blocks(content, lambda line: bool(_LIST_LINE_RE.match(line)), _is_continuation)


def _collect_table_ranges(content: str) -> list[tuple[int, int]]:
    """收集 GFM 表格块区间（连续表格行合并，含表头分隔行）。"""
    return _merge_line_blocks(content, lambda line: bool(_TABLE_LINE_RE.match(line)), lambda line: False)


def _pattern_spans(content: str, pattern) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in pattern.finditer(content)]


def collect_protected_ranges(content: str) -> list[tuple[int, int]]:
    """收集正文中的"压缩禁区"字符区间（代码围栏 / 图片 / GFM 表格 / 列表）。

    表格按"连续表格行"合并为一个区间（含表头分隔行），避免逐行区间被
    跨行 target_text 的碎片命中绕过。列表块按同一方式合并（R179）——
    列表被压成段落是"结构损坏"，比超字数更需要防范。

    ✅ 2026-09-28（围栏口径收敛）：代码围栏改用 ``content_utils.fence_spans``
      （与 ``strip_fenced_code_blocks`` / ``find_unclosed_fences`` 同一套
       CommonMark 语义）—— 旧实现用正则只认反引号，波浪线围栏不保护、
      4 反引号围栏保护区间残缺。
    """
    if not content:
        return []
    ranges: list[tuple[int, int]] = []
    # 代码围栏：CommonMark 语义的已闭合围栏区间
    ranges.extend(fence_spans(content))
    ranges.extend(_pattern_spans(content, _IMAGE_MD_RE))
    ranges.extend(_pattern_spans(content, _IMG_TAG_RE))
    ranges.extend(_collect_list_ranges(content))
    ranges.extend(_collect_table_ranges(content))
    return ranges


def _overlaps_protected(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < r_end and end > r_start for r_start, r_end in ranges)


# ---------------------------------------------------------------------------
# 操作解析
# ---------------------------------------------------------------------------


def _strip_json_fence(text: str) -> str:
    """剥离 ```json / ``` 围栏包裹。"""
    if not text.startswith("```"):
        return text
    body = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    return re.sub(r"\s*```$", "", body).strip()


def _load_json_object(text: str):
    """解析 JSON 对象；失败时截取首个 { 到末个 } 再试一次。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        return None
    try:
        return json.loads(text[first : last + 1])
    except (json.JSONDecodeError, TypeError):
        return None


def _coerce_operation(item) -> dict | None:
    """把单个 JSON 操作收敛为 replace/delete；非法项返回 None。"""
    if not isinstance(item, dict):
        return None
    operation = str(item.get("operation", "")).strip().lower()
    target = str(item.get("target_text", ""))
    # 严禁 rewrite_full / insert 等越权操作（白名单之外一律丢弃）
    if operation not in ("replace", "delete") or not target.strip():
        return None
    if operation == "replace":
        return {"operation": "replace", "target_text": target, "content": str(item.get("content", ""))}
    return {"operation": "delete", "target_text": target, "content": ""}


def parse_shrink_operations(raw: str) -> list[dict]:
    """解析 AI 返回的压缩操作 JSON。

    兼容围栏包裹 / 前后缀噪声；只接受 ``replace``（须含 content）与 ``delete``。
    解析失败或无有效操作时抛 ``ValueError``（由调用方决定终止或重试）。
    """
    if not raw or not raw.strip():
        raise ValueError("压缩返回为空")

    obj = _load_json_object(_strip_json_fence(raw.strip()))
    if not isinstance(obj, dict):
        raise ValueError("压缩返回不是合法 JSON 对象")
    ops_raw = obj.get("operations")
    if not isinstance(ops_raw, list) or not ops_raw:
        raise ValueError("压缩返回缺少 operations")

    ops = [op for op in (_coerce_operation(item) for item in ops_raw) if op is not None]
    if not ops:
        raise ValueError("压缩返回无有效操作（仅允许 replace/delete）")
    return ops


# ---------------------------------------------------------------------------
# 目标定位
# ---------------------------------------------------------------------------


def _normalize_for_match(text: str) -> tuple[str, list[int]]:
    """空白折叠 + NFKC 标点归一，并保留「规范化字符 → 原文下标」映射。

    ✅ R180（2026-11-05 · 误报治理）：生产日志每日约 4 次
    ``content_shrink: target_text 未逐字匹配，跳过本次修补``。根因是模型复述
    target 时空白/换行与正文不同（偶尔还有全/半角标点差异），并非压缩本身失败。
    本函数只用于**首次逐字查找失败后的回退**：逐字命中优先，因此成功路径行为
    逐字不变；回退仍要求规范化后**唯一命中**，并照常做保护区重叠校验（偏移已
    映射回原文），不放松任何红线。

    实现上先展开每个原字符的 NFKC 结果（可能一拆多），再把连续空白压成
    单个半角空格；首尾空白不占位，与"折叠空白"的常规口径一致。
    """
    expanded: list[tuple[str, int]] = [
        (normalized, i) for i, ch in enumerate(text) for normalized in unicodedata.normalize("NFKC", ch)
    ]
    chars: list[str] = []
    pos: list[int] = []
    idx = 0
    size = len(expanded)
    while idx < size:
        ch, source_index = expanded[idx]
        if ch.isspace():
            run_end = idx
            while run_end < size and expanded[run_end][0].isspace():
                run_end += 1
            # 中间空白折叠为一个空格（锚在空白段首字符），尾部空白直接丢弃
            if chars and run_end < size:
                chars.append(" ")
                pos.append(source_index)
            idx = run_end
            continue
        chars.append(ch)
        pos.append(source_index)
        idx += 1
    return "".join(chars), pos


def _unique_span(text: str, needle: str, duplicate_msg: str) -> tuple[int, int] | None:
    """在 text 中查找 needle 的唯一区间；缺失返回 None，多次出现抛错。"""
    first = text.find(needle)
    if first < 0:
        return None
    if text.find(needle, first + 1) >= 0:
        raise ValueError(duplicate_msg)
    return first, first + len(needle)


def _locate_target(content: str, target: str) -> tuple[int, int]:
    """定位 target 在 content 的唯一区间（逐字优先，规范化回退）。

    不存在 / 出现多次 → 抛 ValueError（错误文案与历史逐字一致，调用方口径不变）。
    """
    exact = _unique_span(content, target, "操作目标在正文中出现多次（target_text 不唯一）")
    if exact is not None:
        return exact

    normalized_content, source_map = _normalize_for_match(content)
    normalized_target, _ = _normalize_for_match(target)
    normalized_target = normalized_target.strip()
    if not normalized_target:
        raise ValueError("操作目标在正文中不存在（target_text 未逐字匹配）")

    first = normalized_content.find(normalized_target)
    if first < 0:
        raise ValueError("操作目标在正文中不存在（target_text 未逐字匹配）")
    if normalized_content.find(normalized_target, first + 1) >= 0:
        raise ValueError("操作目标在正文中出现多次（target_text 不唯一）")
    return source_map[first], source_map[first + len(normalized_target) - 1] + 1


# ---------------------------------------------------------------------------
# 操作应用
# ---------------------------------------------------------------------------


def _apply_edits(content: str, edits: list[tuple[int, int, str]]) -> str:
    """按区间起点倒序写入，保证前面的替换不影响后面操作的偏移量。"""
    result = content
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def apply_shrink_operations(content: str, ops: list[dict]) -> str:
    """把压缩操作应用到正文（全有或全无：任一操作非法即抛 ValueError）。

    校验规则：
    - target_text 必须在当前正文中**逐字出现且仅出现一次**；
    - 操作区间不得与保护区间（代码围栏/表格/图片/列表）重叠 —— 命中即拒绝。
    """
    if not ops:
        raise ValueError("无操作可应用")
    ranges = collect_protected_ranges(content)
    edits: list[tuple[int, int, str]] = []
    for op in ops:
        start, end = _locate_target(content, op["target_text"])
        if _overlaps_protected(start, end, ranges):
            raise ValueError("字数调整不能修改图片、Mermaid、代码块或表格、列表")
        edits.append((start, end, op.get("content", "")))
    return _apply_edits(content, edits)


# ---------------------------------------------------------------------------
# 轮次主循环
# ---------------------------------------------------------------------------


def _empty_outcome(content: str, before: int) -> dict:
    return {
        "content": content,
        "before": before,
        "word_count": before,
        "rounds_used": 0,
        "stop_reason": "章节正文为空",
        "applied": False,
    }


def _request_payload(current: str, current_wc: int, word_budget: int) -> str:
    return f"当前字数 {current_wc}，目标 {word_budget} 字。" "请输出压缩操作 JSON（不要输出正文本身）：\n\n" + current


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
    不依赖 provider / prompts 层 —— 既可被两个调用方复用，也便于用假实现做
    纯单元测试。

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
    if before <= 0 or not (content or "").strip():
        # ✅ 纯空白内容同样视为无可压缩（text_word_count 按字符计数，会把空格算进去）
        return _empty_outcome(current, before)

    out = {
        "content": current,
        "before": before,
        "word_count": before,
        "rounds_used": 0,
        "stop_reason": f"已达 {max_rounds} 轮上限",
        "applied": False,
    }
    current_wc = before
    round_no = 0
    while round_no < max_rounds:
        round_no += 1
        if on_round is not None:
            try:
                await on_round(round_no, current_wc)
            except Exception:  # 进度上报失败绝不影响压缩
                logger.debug("压缩进度回调异常（已忽略）", exc_info=True)

        try:
            sys_prompt = prompt_factory(round_no, current_wc)
        except Exception as e:
            logger.warning("压缩提示词渲染失败: %s", e)
            out["stop_reason"] = f"提示词渲染失败: {e}"
            break

        try:
            raw = await call_ai(sys_prompt, _request_payload(current, current_wc, word_budget))
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

    out["applied"] = current != content and out["word_count"] < before
    return out
