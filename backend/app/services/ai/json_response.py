"""统一 JSON 生成 / 校验 / 修复通道。

流程：请求模型 → 优先 JSON 模式 → 提取 JSON → 校验 → 失败定向修复 → 有界重试 → 返回。

范围说明：本模块只做「单次生成 + 定向修复 + 有界重试」，**不包含分步生成**。
超大 JSON 的分步生成本身是领域问题，由调用方拆分实现：
- 目录生成：一级 → 逐章二三级（routers/sse_handlers.py，word_budget > 50000 时启用）；
- 事实提取：文档分块逐块提取（services/facts_extractor.py，FACTS_CHUNK_RETRIES）。
"""

import json
import logging
import re

from app.services.ai.prompts._metrics import (
    record_ai_failure,
    record_repair_triggered,
)
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import chat_with_fallback

logger = logging.getLogger("json_response")

#: 修复轮上限。
#: 推导：本地字符串级修复已先过滤掉「非法反斜杠」这一大类（约占可挽救候选的
#: 多数）；剩下的失败多为结构性错误，通常 1 轮定向修复即可纠正，第 2 轮作为
#: 保险。继续增加轮次收益急剧下降，而每轮真实 AI 调用耗时数十秒，因此取 2
#: 作为「成功率 / 时延」的平衡点。
MAX_REPAIR_RETRIES = 2

#: 非目录任务的默认修复提示词。
#: 修复提示词必须描述**通用 JSON 结构**：多数调用点（符合性检查、一致性审计、
#: 冲突仲裁、事实确认、事实补全、上传章节识别等）返回的都不是目录结构，用
#: 目录提示词会诱导模型把结果改成目录形状。
#: 目录族调用点必须显式传 ``OUTLINE_REPAIR_KEY``；该约定由 AST 护栏锁死。
GENERIC_REPAIR_KEY = "json_schema_fix_system"

#: 目录族专用修复提示词（唯一需要「目录」语义的场景）。
OUTLINE_REPAIR_KEY = "outline_json_fix_system"

#: 事实提取族专用修复提示词（facts_extractor 分块提取消费）。
#: 具名常量存在的目的是避免调用点直接拼字符串，导致与注册条目漂移。
FACTS_REPAIR_KEY = "facts_json_fix_system"


# ---------------------------------------------------------------------------
# 提示词渲染
# ---------------------------------------------------------------------------


def _build_repair_user_prompt(issues: list, raw: str, repair_key: str = GENERIC_REPAIR_KEY) -> str:
    """构造 JSON 修复 user 提示词。

    提示词正文只保留注册表一份（DB 优先，可在前端编辑生效）。渲染异常时
    退化为「注册表原文 + 朴素占位符替换」，保证修复链路不中断，且不引入
    第二份内联文案。
    """
    try:
        return render(
            repair_key,
            target_description="JSON 结果",
            issues=_format_json_issues(issues or ["JSON 语法错误，无法解析"]),
            invalid_content=raw or "",
        )
    except Exception:
        try:
            from app.services.ai.prompts._registry import get_prompt as _get_generic

            _tpl = _get_generic(GENERIC_REPAIR_KEY)
            return (
                _tpl.replace("{target_description}", "JSON 结果")
                .replace("{issues}", _format_json_issues(issues or ["JSON 语法错误，无法解析"]))
                .replace("{invalid_content}", raw or "")
            )
        except Exception:  # noqa: BLE001
            logger.error("JSON 修复提示词不可用（注册表缺失）", exc_info=True)
            raise ValueError("JSON 修复提示词不可用：注册表缺少 %s" % GENERIC_REPAIR_KEY) from None


def _format_json_issues(issues: list) -> str:
    return "\n".join(f"- {i}" for i in issues)


# ---------------------------------------------------------------------------
# JSON 提取：括号配对扫描器（显式状态机）
# ---------------------------------------------------------------------------


class _BalancedJsonScanner:
    """在任意文本中定位第一个完整的 JSON 结构。

    状态机只维护三个量：``depth``（嵌套深度）、``in_string``（是否在字符串内）、
    ``escaped``（上一个字符是否开启转义）。这样字符串内的括号不会计入深度，
    嵌套结构能正确闭合，多个片段只取第一个完整结构。

    注意：起点按「第一个出现的 { 或 [」确定，与是否位于字符串中无关 —— 这是
    调用方依赖的既有语义（模型常在 JSON 前后附带解释性文字）。
    """

    _OPENERS = {"{": "}", "[": "]"}
    _QUOTE = '"'
    _BACKSLASH = "\\"

    def __init__(self, text: str) -> None:
        self._text = text

    def _find_start(self) -> int:
        for position, char in enumerate(self._text):
            if char in self._OPENERS:
                return position
        return -1

    def scan(self) -> str | None:
        start = self._find_start()
        if start < 0:
            return None
        opener = self._text[start]
        closer = self._OPENERS[opener]
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(self._text)):
            char = self._text[position]
            if escaped:
                escaped = False
                continue
            if in_string and char == self._BACKSLASH:
                escaped = True
                continue
            if char == self._QUOTE:
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return self._text[start : position + 1]
        return None


def _extract_balanced(text: str) -> str | None:
    """从文本中提取最外层的 ``{...}`` 或 ``[...]``（括号配对算法）。"""
    return _BalancedJsonScanner(text).scan()


#: 代码围栏匹配：允许 \`\`\`json 或裸 \`\`\` 开头，内容非贪婪直到下一个闭合围栏。
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _json_from_fenced_blocks(text: str) -> str | None:
    """优先从代码围栏内提取 JSON（多个围栏逐个尝试）。"""
    for block in _FENCE_PATTERN.findall(text):
        candidate = _extract_balanced(block.strip())
        if candidate:
            return candidate
    return None


def _json_from_raw_text(text: str) -> str | None:
    """无围栏时在原文中依次提取候选结构，返回第一个可解析的。"""
    candidates: list[str] = []
    remaining = text
    while True:
        candidate = _extract_balanced(remaining)
        if not candidate:
            break
        candidates.append(candidate)
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            index = remaining.find(candidate)
            remaining = remaining[index + len(candidate) :]
    return candidates[0] if candidates else None


def extract_json(text: str) -> str | None:
    """从模型输出中提取 JSON 字符串（兼容代码块包裹）。

    优先从代码围栏提取；若多个围栏都拿不到结构，再在原文中用括号配对扫描。
    """
    text = text.strip()
    return _json_from_fenced_blocks(text) or _json_from_raw_text(text)


# ---------------------------------------------------------------------------
# 字符串内非法转义的本地修复
# ---------------------------------------------------------------------------

#: JSON 规范允许的反斜杠转义目标字符（``\uXXXX`` 另行按四位十六进制判定）。
_LEGAL_ESCAPE_TARGETS = frozenset('"\\/bfnrt')

#: 弱模型常见的 markdown 风格转义目标：出现在 JSON 字符串里属于非法转义，
#: 修复方式是直接去掉反斜杠、保留字符本身。
_MARKDOWN_ESCAPE_TARGETS = frozenset(".()[]{}#*+-_!<>|`")

#: 十六进制字符集（用于识别合法的 ``\uXXXX``）。
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _escape_replacement(char: str) -> str:
    """决定一个非法转义序列的去向（返回替换文本，反斜杠已按规则保留/删除）。"""
    if char in _LEGAL_ESCAPE_TARGETS:
        return "\\" + char
    if char in _MARKDOWN_ESCAPE_TARGETS:
        return char
    # 兜底：其余情况一律变成「双反斜杠 + 字符」，保证结果仍是合法 JSON
    return "\\\\" + char


def _repair_json_string_escapes(content: str) -> str:
    """修复 JSON 字符串内部的非法反斜杠转义。

    弱模型常在字符串里写入 markdown 风格转义（如 ``1\\.5m``、``\\#基坑``），
    这些在 JSON 规范里只有 ``" \\\\ / b f n r t u`` 合法，其余反斜杠会让
    ``json.loads`` 直接失败并进入耗时的 AI 修复轮。本地先修一遍，零 AI 成本。

    关键：``\\uXXXX``（四位十六进制）必须整体保留，不能被逐字符拆开处理。
    """
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    total = len(content)
    while index < total:
        char = content[index]
        if not in_string:
            if char == '"':
                in_string = True
            output.append(char)
            index += 1
            continue
        if not escaped:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                output.append(char)
            else:
                output.append(char)
            index += 1
            continue
        escaped = False
        following = content[index + 1 : index + 5]
        if char == "u" and index + 4 < total and all(d in _HEX_DIGITS for d in following):
            # 合法 unicode 转义：连同反斜杠整体保留
            output.append(content[index - 1 : index + 5])
            index += 5
            continue
        output.append(_escape_replacement(char))
        index += 1
    if escaped:
        output.append("\\\\")
    return "".join(output)


# ---------------------------------------------------------------------------
# 解析 + 校验
# ---------------------------------------------------------------------------


def parse_and_validate(raw: str, validate_fn=None):
    """解析并校验；``validate_fn(obj)`` 返回错误列表（空列表 = 通过）。

    校验器通常假设 obj 是 dict（如 ``lambda o: o.get("outline")``），而弱模型
    可能返回顶层数组或裸值。这里把校验器异常转成 issues 交给修复轮，而不是
    让 AttributeError 直接炸掉整个任务（否则连修复机会都没有）。

    ``json.loads`` 失败时先做一次本地字符串级修复再重试：合法 JSON 零成本，
    非法 JSON 可挽救一批本应进入完整 AI 修复轮的候选。
    """
    js = extract_json(raw)
    if not js:
        return None, ["输出中未找到 JSON 结构"]
    try:
        obj = json.loads(js)
    except json.JSONDecodeError as e:
        repaired = _repair_json_string_escapes(js)
        if repaired == js:
            return None, [f"JSON 语法错误: {e}"]
        try:
            obj = json.loads(repaired)
            logger.info("JSON 本地修复成功（非法反斜杠）：原始错误=%s，已跳过 AI 修复轮", e)
        except json.JSONDecodeError as e2:
            return None, [f"JSON 语法错误（本地修复后仍失败）: {e2}"]
    if validate_fn is not None:
        try:
            issues = validate_fn(obj)
        except Exception as e:
            return obj, [f"输出顶层结构非预期（应为对象而非 {type(obj).__name__}）: {e}"]
        if issues:
            return obj, issues
    return obj, []


#: 只有 system 消息时补的最小 user 指令。
JSON_USER_NUDGE = "请严格按上述要求输出 JSON 结果，不要输出任何解释。"


def _ensure_user_message(messages: list, nudge: str = JSON_USER_NUDGE) -> list:
    """保证消息列表中存在 user 消息（缺失时追加一条最小指令）。

    背景：本项目大量 JSON 任务把指令全写在 system 里。OpenAI 兼容协议允许
    只有 system，但部分 provider 会直接返回 ``400 Bad Request: No user query
    found in messages.`` —— 这类任务在该 provider 上必然失败。修在共享入口，
    新增调用点与复制粘贴的写法都不会再退化；已有 user 消息的任务不受影响。
    user 内容不能为空串（另有 provider 显式拒绝空 content）。
    """
    if not isinstance(messages, list):
        return messages
    has_user = any(isinstance(m, dict) and str(m.get("role") or "") == "user" for m in messages)
    if has_user:
        return messages
    return [*messages, {"role": "user", "content": nudge}]


async def _invoke_with_kwarg_fallback(call, messages: list, trials: tuple[dict, ...], mismatch_message: str):
    """按 trials 由全到简依次调用；只在 TypeError 时降级，最后一次仍失败则抛出。

    只对 TypeError 降级一次（本仓既有约定），不无限吞异常：签名不兼容是唯一的
    预期场景，其余异常必须原样上抛。
    """
    for position, trial in enumerate(trials):
        try:
            return await call(messages, **trial)
        except TypeError:
            if position == len(trials) - 1:
                raise
    raise TypeError(mismatch_message)


async def _call_chat_fn(chat_fn, messages: list, **kwargs) -> str:
    """调用注入式 ``chat_fn``，兼容「只认 messages」的最小签名实现。"""
    return await _invoke_with_kwarg_fallback(
        chat_fn,
        messages,
        (kwargs, {}),
        "chat_fn 签名不兼容",
    )


async def _provider_chat_safe(
    provider, messages: list, temperature: float = 0.7, max_tokens: int | None = None, extra_body: dict | None = None
) -> str:
    """调用 ``provider.chat``，兼容不支持 temperature / max_tokens / extra_body 的旧实现。

    按「逐个降级」透传：先全参数，TypeError 则逐步去掉可选参数再试，保证旧实现
    （只认 messages、或只认 temperature）仍可工作。
    """
    full: dict = {"temperature": temperature}
    if max_tokens is not None:
        full["max_tokens"] = max_tokens
    if extra_body is not None:
        full["extra_body"] = extra_body
    return await _invoke_with_kwarg_fallback(
        provider.chat,
        messages,
        (full, {"temperature": temperature}, {}),
        "provider.chat 签名不兼容",
    )


# ---------------------------------------------------------------------------
# 生成 → 解析 → 修复 的共享编排
# ---------------------------------------------------------------------------


async def _run_json_generation(
    chat_call,
    messages: list,
    evaluate,
    max_retries: int,
    repair_key: str,
    on_call_failure=None,
    on_repair=None,
):
    """共享编排：首轮生成 → 评估 → 定向修复（最多 max_retries 轮）。

    Args:
        chat_call: ``await chat_call(messages) -> str`` 的异步可调用对象。
        evaluate: ``evaluate(raw) -> (obj, issues)``，issues 为空表示通过。
        on_call_failure: 首轮调用失败的旁路回调（仅观测，不改变抛错语义）。
        on_repair: 进入修复轮时的旁路回调（仅观测）。

    Returns:
        ``(obj, raw)`` —— 通过校验的对象与对应原文。

    Raises:
        ValueError: 首轮与全部修复轮都未通过。
    """
    messages = _ensure_user_message(messages)
    try:
        raw = await chat_call(messages)
    except Exception:
        if on_call_failure is not None:
            on_call_failure()
        raise
    first_raw = raw
    obj, issues = evaluate(raw)
    if obj is not None and not issues:
        return obj, raw

    if on_repair is not None:
        on_repair()

    repair_errors: list[str] = []
    for attempt in range(max_retries):
        # 修复提示词的 invalid_content 必须携带**最初**的问题输出，而不是上一轮
        # 修复的半成品（否则第 2 轮是在「修已经改坏的结果」）。
        repair_messages = [
            *messages,
            {"role": "assistant", "content": raw or ""},
            {"role": "user", "content": _build_repair_user_prompt(issues, first_raw or "", repair_key)},
        ]
        try:
            raw = await chat_call(repair_messages)
        except Exception as e:  # noqa: BLE001
            repair_errors.append("第%d次修复调用异常: %s" % (attempt + 1, e))
            logger.warning("JSON 修复第 %d 次调用异常（继续使用剩余修复预算）：%s", attempt + 1, e)
            continue
        obj, issues = evaluate(raw)
        if obj is not None and not issues:
            return obj, raw
        logger.warning("JSON 修复第 %d 次仍未通过：%s", attempt + 1, issues[:3])

    if repair_errors:
        raise ValueError("JSON 生成/修复失败：%s；修复轮异常：%s" % (issues[:5], " | ".join(repair_errors)))
    raise ValueError(f"JSON 生成/修复失败：{issues[:5]}")


async def collect_json_response_with_chat(
    chat_fn,
    messages: list,
    validate_fn=None,
    max_retries: int = MAX_REPAIR_RETRIES,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    repair_key: str = GENERIC_REPAIR_KEY,
    scene: str = "",
    max_tokens: int | None = None,
):
    """统一 JSON 收集编排：生成 → 解析 → 校验 → 定向修复 → 有界重试。

    AI 调用来源作为参数注入，使需要注入 chat 实现的调用方（含测试）可以复用
    完整修复链路，而不必各自再写一份「解析即弃」的劣化版。

    Args:
        chat_fn: 与 ``chat_with_fallback`` 同签名的异步可调用对象。
        其余参数、语义、返回值与 :func:`collect_json_response` 完全一致。
    """

    async def _call(repair_messages: list) -> str:
        return await _call_chat_fn(
            chat_fn,
            repair_messages,
            temperature=temperature,
            json_mode=json_mode,
            timeout=timeout,
            scene=scene,
            max_tokens=max_tokens,
        )

    return await _run_json_generation(
        _call,
        messages,
        lambda text: parse_and_validate(text, validate_fn),
        max_retries,
        repair_key,
        on_call_failure=lambda: record_ai_failure(scene),
        on_repair=lambda: record_repair_triggered(repair_key),
    )


async def collect_json_response(
    messages: list,
    validate_fn=None,
    max_retries: int = MAX_REPAIR_RETRIES,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    repair_key: str = GENERIC_REPAIR_KEY,
    scene: str = "",
    max_tokens: int | None = None,
):
    """统一 JSON 收集入口：生成 → 解析 → 校验 → 定向修复 → 重试。

    透传参数（供结构化提取使用）：
    - ``temperature`` / ``json_mode`` / ``timeout``：低温 + JSON 模式 + 更长超时，降低非法 JSON 与超时；
    - ``repair_key``：修复轮使用的提示词。目录族调用点必须显式传
      ``OUTLINE_REPAIR_KEY``（默认值是 Schema 自适应的通用提示词）。
    - ``max_tokens``：显式输出上限。目录合并修复需要回吐完整三级目录，长方案
      可能触发 provider 默认上限（8192）被截断；None 表示沿用 provider 默认。
    - ``scene``：业务场景标记，透传至 ``chat_with_fallback`` 写入
      ``ai_audit_logs.scene``，``/ai/stats`` 可按场景聚合调用次数。

    消息列表只有 system 时自动补一条最小 user 指令（见 :func:`_ensure_user_message`）。

    本函数只做一件事：把 provider 降级链作为 ``chat_fn`` 注入共享编排，
    保证全仓只有一份修复逻辑。
    """

    async def _chat(_msgs: list, **kwargs) -> str:
        return await chat_with_fallback(_msgs, **kwargs)

    return await collect_json_response_with_chat(
        _chat,
        messages,
        validate_fn,
        max_retries,
        temperature=temperature,
        json_mode=json_mode,
        timeout=timeout,
        repair_key=repair_key,
        scene=scene,
        max_tokens=max_tokens,
    )


def _run_validator(validator, obj) -> list:
    """兼容两种 validator 约定：``(is_valid, error_msg)`` 与 issues 列表。"""
    result = validator(obj)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_valid, error_msg = result
        return [] if is_valid else [str(error_msg)]
    if isinstance(result, list):
        return result
    if result:
        return [str(result)]
    return []


def _evaluate_provider_output(raw: str, validator, schema) -> tuple[object, list]:
    """解析 → 业务校验 → Schema 校验，任何一步失败都转成 issues 进入修复轮。"""
    obj, issues = parse_and_validate(raw, None)
    if obj is None or issues:
        return obj, issues
    if validator is not None:
        issues = _run_validator(validator, obj)
        if issues:
            return obj, issues
    if schema is not None:
        try:
            return schema.model_validate(obj), []
        except Exception as e:  # noqa: BLE001
            # Schema 校验失败不应直接崩溃：转为 issues 进入修复轮
            return None, [f"Schema 校验失败：{e}"]
    return obj, issues


async def collect_json_response_with_provider(
    provider,
    messages: list,
    temperature: float = 0.7,
    schema=None,
    validator=None,
    max_retries: int = 1,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
    repair_key: str = GENERIC_REPAIR_KEY,
):
    """provider 直连版 JSON 收集。

    与 :func:`collect_json_response` 的区别：不经过 provider_factory 的降级链，
    直接使用调用方传入的 provider 实例（插图编排流水线使用）。

    返回解析后的对象本身（非二元组）。validator 兼容两种约定：
    - ``validator(obj) -> (is_valid: bool, error_msg: str)``
    - ``validate_fn(obj) -> issues: list[str]``（空列表 = 通过）

    消息列表只有 system 时同样自动补一条最小 user 指令。
    """

    async def _call(repair_messages: list) -> str:
        return await _provider_chat_safe(provider, repair_messages, temperature, max_tokens, extra_body)

    obj, _raw = await _run_json_generation(
        _call,
        messages,
        lambda text: _evaluate_provider_output(text, validator, schema),
        max_retries,
        repair_key,
    )
    return obj


# ------------------------------------------------------------
# 标题内嵌编号清洗 + 目录树编号重排
#
# 编号统一：核心实现收敛到 services/numbering.py 唯一事实源，本模块保留同名
# 薄包装 / 再导出（调用方众多：sse_handlers / outline_library / sections / tests
# 均按 json_response.renumber_outline 引用，签名与语义不变）。
# ------------------------------------------------------------
import re as _re  # noqa: F401  — 兼容再导出（历史引用点按 json_response._re 使用）

from app.services.numbering import (  # noqa: F401  — 兼容再导出
    _PURE_NUMBER_TITLE_RE,
    _STRIP_NUMBER_RE,
    renumber_outline_nodes,
    strip_outline_numbering,
)


def renumber_outline(nodes: list, prefix: str = "", _depth: int = 0) -> list:
    """程序统一重排编号（薄包装；唯一实现在 services/numbering.renumber_outline_nodes）

    兼容说明：保持与旧实现完全相同的签名与语义（就地修改并返回；
    非列表原样返回；递归深度防护 20）。
    """
    return renumber_outline_nodes(nodes, strip_titles=True, prefix=prefix, _depth=_depth)
