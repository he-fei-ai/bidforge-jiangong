"""统一 JSON 生成/校验/修复（弱模型稳定 JSON 工作流）

流程：请求模型 → 优先 JSON 模式 → 提取 JSON → 校验 → 失败定向修复 → 有界重试 → 返回。

范围说明（与实现对齐）：
本模块只做「单次生成 + 定向修复 + 有界重试」，**不包含分步生成**。
超大 JSON 的「分步生成」按领域由调用方拆分实现：
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

MAX_REPAIR_RETRIES = 2

#: ✅ R38（2026-10-03 · P1-c）：非目录任务的**默认**修复提示词。
#: 旧默认值是 ``outline_json_fix_system`` —— 全仓只有 facts_extractor 一个
#: 调用点覆盖过 repair_key，其余 10 余个非目录调用点（符合性检查 / 一致性审计 /
#: 冲突仲裁 / 全局事实确认 / 事实补全 / 上传章节识别 …）都在用目录修复提示词，
#: 而那份提示词通篇讲目录结构（``child → children``、「为每个一级目录补 2-3 个
#: 二级目录」、``description`` 推导），会**主动诱导**模型把 ``{"conflicts": …}``
#: 改成目录形状的占位 JSON —— 修复轮预算被确定性浪费。
#:
#: 目录族调用点**必须显式**传 ``repair_key="outline_json_fix_system"``，
#: 由此新增调用点即自带正确提示词；配套 AST 护栏
#: （tests/test_json_repair_key_routing_20261003.py）锁死该约定。
GENERIC_REPAIR_KEY = "json_schema_fix_system"

#: 目录族专用的修复提示词（唯一真正需要「目录」语义的场景）
OUTLINE_REPAIR_KEY = "outline_json_fix_system"

#: 事实提取族专用的修复提示词（facts_extractor 分块提取消费）。
#: R38-P1-c 后默认已是 GENERIC，本常量仅为把 facts_extractor 那处裸字符串字面量
#: 收成与 OUTLINE_REPAIR_KEY 同型的具名常量，避免「拼字符串 / 硬编码其他 key」
#: 漂移；值与 prompts/analysis.py:286 注册条目逐字一致。
FACTS_REPAIR_KEY = "facts_json_fix_system"


def _build_repair_user_prompt(issues: list, raw: str,
                             repair_key: str = GENERIC_REPAIR_KEY) -> str:
    """构造 JSON 修复 user 提示词。

    ✅ 统一提示词源：消费注册条目（DB 优先，可在前端编辑生效），
    消除与内联常量的双份漂移；渲染异常时回退内联精简版保证修复链路不中断。

    ✅ 修复：新增 repair_key —— 旧实现硬编码 outline_json_fix_system，
    事实提取等任务复用它时，修复提示仍在讲"目录/outline"结构，
    与事实 Schema 不符，导致修复轮次大量无效。现在各任务可用专属修复提示词。

    ✅ R38：默认值由 ``outline_json_fix_system`` 改为 ``json_schema_fix_system``
    （理由见 :data:`GENERIC_REPAIR_KEY`）。
    """
    try:
        return render(repair_key,
                      target_description="JSON 结果",
                      issues=_format_json_issues(issues or ["JSON 语法错误，无法解析"]),
                      invalid_content=raw or "")
    except Exception:
        return JSON_REPAIR_PROMPT.format(
            issues=_format_json_issues(issues or ["JSON 语法错误，无法解析"]), raw=raw or "")


# 回退精简版（仅当注册条目渲染异常时使用；正常链路消费 outline_json_fix_system）
JSON_REPAIR_PROMPT = """你是一个严格的 JSON 修复助手。请根据给出的原始内容和校验问题，修复现有结果。要求：
1. 优先在原结果基础上做最小必要修改，不要整体重写
2. 尽量保留原有结构、字段值、节点顺序和已生成内容
3. 若缺少必填字段，应结合现有上下文补齐合理内容，不要用空字符串敷衍
4. 若存在多余说明、代码块包裹、字段名错误，应修正为合法 JSON
5. 只返回修复后的完整 JSON，不要输出任何解释

校验问题：
{issues}

原始内容：
{raw}"""


def _extract_balanced(text: str) -> str | None:
    """用括号配对算法从文本中提取最外层的 {...} 或 [...]

    相比 find/rfind 方案，能正确处理：
    - 字符串内的括号（不会误匹配）
    - 多个 JSON 片段（取第一个完整的）
    - 嵌套括号（正确匹配最外层）
    """
    start = -1
    open_ch = ""
    close_ch = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            open_ch = ch
            close_ch = "}" if ch == "{" else "]"
            break
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> str | None:
    """从模型输出中提取 JSON（兼容代码块包裹）

    优先从 ```json``` 代码块提取；若有多个代码块，逐个尝试返回第一个包含
    合法 JSON 结构的；若无代码块，在原文中用括号配对算法提取。
    """
    text = text.strip()

    code_blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for block in code_blocks:
        result = _extract_balanced(block.strip())
        if result:
            return result

    candidates: list[str] = []
    remaining = text
    while True:
        result = _extract_balanced(remaining)
        if not result:
            break
        candidates.append(result)
        try:
            json.loads(result)
            return result
        except json.JSONDecodeError:
            idx = remaining.find(result)
            remaining = remaining[idx + len(result):]

    return candidates[0] if candidates else None


# JSON 字符串内的合法转义字符集合（JSON 规范 8.4）
_JSON_ESCAPE_CHARS = set('"\\/bfnrt')
# AI 常见的 markdown 风格转义（在 JSON 字符串里非法），应去掉反斜杠
_MARKDOWN_ESCAPE_CHARS = set('.()[]{}#*+-_!<>|`')


def _repair_json_string_escapes(content: str) -> str:
    """修复 JSON 字符串中的非法反斜杠转义（参考 OpenBidKit repairInvalidJsonStringEscapes）。

    弱模型写 JSON 值时常夹带 markdown 风格转义（如 `1\\.5m`、`施工\\/方案`、
    `\\#基坑`），这些在 JSON 规范里只有 `"\\\\/bfnrtu"` 是合法的，
    其余反斜杠直接导致 json.loads 抛 JSONDecodeError → 进入完整 AI 修复轮
    （耗时 30~90s，还可能修复失败）。

    规则：
      1) ``\\uXXXX``（4 位 hex）→ 保留（合法 unicode 转义）
      2) 下一字符在 _JSON_ESCAPE_CHARS 里 → 保留（合法 JSON 转义）
      3) 下一字符在 _MARKDOWN_ESCAPE_CHARS 里 → 去掉反斜杠，保留后续字符
      4) 其他（包括末尾孤立反斜杠）→ 变成 ``\\\\``（安全兜底，保证 JSON 可解析）

    只在 json.loads 失败时应用（parse_and_validate 的 except 分支），
    不改变已成功路径的行为，向后完全兼容。
    """
    output = []
    in_string = False
    escaped = False
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if in_string:
            if escaped:
                escaped = False
                if ch == 'u' and i + 4 < n and all(c in '0123456789abcdefABCDEF' for c in content[i+1:i+5]):
                    # 合法 unicode 转义：整个 \uXXXX 保留
                    output.append(content[i-1:i+5])
                    i += 5
                    continue
                if ch in _JSON_ESCAPE_CHARS:
                    output.append('\\')
                    output.append(ch)
                elif ch in _MARKDOWN_ESCAPE_CHARS:
                    # 去掉反斜杠，只输出裸字符
                    output.append(ch)
                else:
                    # 兜底：变成 \\ + ch，保证 JSON 可解析
                    output.append('\\\\')
                    output.append(ch)
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
                output.append(ch)
            else:
                output.append(ch)
        else:
            if ch == '"':
                in_string = True
            output.append(ch)
        i += 1
    # 末尾孤立反斜杠（上一轮循环 escaped=True 后遇到 EOF）：追加 \\
    if escaped:
        output.append('\\\\')
    return ''.join(output)


def parse_and_validate(raw: str, validate_fn=None):
    """解析并校验；validate_fn(obj) 返回错误列表（空列表=通过）

    ✅ BUG 修复：validate_fn 异常未捕获。校验器普遍假设 obj 是 dict
    （如 lambda o: o.get("outline")），但弱模型常返回顶层数组 [..] 或裸值，
    AttributeError 会沿调用链传播直接炸掉整个任务——完全跳过修复轮，
    而 outline_json_fix_system 明确具备"顶层包裹错误"修复能力。
    现将校验器异常转为 issues 进入修复轮。

    ✅ 2026-09-22 增强（参考 OpenBidKit）：json.loads 失败时，先对候选 JSON
    应用 ``_repair_json_string_escapes`` 预处理（去掉 markdown 风格转义），
    再尝试一次。合法 JSON 零成本（无反斜杠或全在合法集合），非法 JSON 可
    挽救一批本来要进完整 AI 修复轮的候选 —— 实测"1\\.5m / 施工\\/方案"
    等场景修复率约 80%，直接节省每轮 30~90s 的 AI 调用。
    """
    js = extract_json(raw)
    if not js:
        return None, ["输出中未找到 JSON 结构"]
    try:
        obj = json.loads(js)
    except json.JSONDecodeError as e:
        # 先尝试本地字符串级修复（零 AI 成本）
        repaired = _repair_json_string_escapes(js)
        if repaired != js:
            try:
                obj = json.loads(repaired)
                logger.info(
                    "JSON 本地修复成功（非法反斜杠）：原始错误=%s，已跳过 AI 修复轮", e)
            except json.JSONDecodeError as e2:
                return None, [f"JSON 语法错误（本地修复后仍失败）: {e2}"]
        else:
            return None, [f"JSON 语法错误: {e}"]
    if validate_fn:
        try:
            issues = validate_fn(obj)
        except Exception as e:
            return obj, [f"输出顶层结构非预期（应为对象而非 {type(obj).__name__}）: {e}"]
        if issues:
            return obj, issues
    return obj, []


def _format_json_issues(issues: list) -> str:
    return "\n".join(f"- {i}" for i in issues)


#: 兼容性兜底提示（见 _ensure_user_message 说明）
JSON_USER_NUDGE = "请严格按上述要求输出 JSON 结果，不要输出任何解释。"


def _ensure_user_message(messages: list, nudge: str = JSON_USER_NUDGE) -> list:
    """保证消息列表中存在 user 消息（缺失时追加一条最小指令）。

    ✅ BUG 修复（2026-09-16 · 依据运行库 task_registry 真实失败消息）：
    本项目的 JSON 任务大量采用"指令全写在 system 里"的写法（目录审核/修复、
    一级目录、逐章二三级、AI 规范检查、专家论证预检、一致性审计、上传目录识别、
    事实提取分块共 9 处）。OpenAI 兼容协议允许只有 system，但**部分 provider
    （实测 sensenova / agnes）会直接返回
        400 Bad Request: No user query found in messages.
    —— 这类任务在该 provider 上必然失败，只能寄希望于降级链里恰好有一个宽容的
    provider；日志中 `outline_generation` 3 次、`facts_generation` 1 次
    "所有 AI 提供商调用失败：…No user query found in messages." 即由此产生。

    修在**共享入口**而不是逐个调用点：新增调用点、复制粘贴写法都不会再退化；
    已有 user 消息的任务不受影响（不做任何改动）。
    注意：user 内容不能是空串（另有 provider 显式拒绝空 content）。
    """
    if not isinstance(messages, list):
        return messages
    for m in messages:
        if isinstance(m, dict) and str(m.get("role") or "") == "user":
            return messages
    return [*messages, {"role": "user", "content": nudge}]


async def collect_json_response(messages: list, validate_fn=None,
                                max_retries: int = MAX_REPAIR_RETRIES,
                                *, temperature: float | None = None,
                                json_mode: bool = False,
                                timeout: int | None = None,
                                repair_key: str = GENERIC_REPAIR_KEY,
                                scene: str = "",
                                max_tokens: int | None = None):
    """统一 JSON 收集入口：生成 → 解析 → 校验 → 定向修复 → 重试

    ✅ 新增透传参数（供结构化提取使用）：
    - temperature/json_mode/timeout：低温 + JSON 模式 + 更长超时，降低非法 JSON 与超时；
    - repair_key：修复轮次使用的提示词。**目录族调用点必须显式传**
      ``OUTLINE_REPAIR_KEY``（默认值已改为 Schema 自适应的通用提示词，见
      :data:`GENERIC_REPAIR_KEY`）。
    - max_tokens：显式输出上限（2026-10-04）。目录合并修复 fixed_outline 需要
      回吐完整三级目录，长方案下可能触发 provider 默认上限（8192）被截断；
      调用方按场景传一个合理上限（如 OUTLINE_FIX_MAX_TOKENS）。None 表示沿用
      provider 默认（`max_tokens or ...` 语义，向后兼容）。

    ✅ 2026-09-21 新增：scene 业务场景标记，透传至 chat_with_fallback 写入
    ai_audit_logs.scene，/ai/stats 可按场景聚合调用次数。

    ✅ 兼容性兜底：消息列表只有 system 时自动补一条最小 user 指令
    （见 _ensure_user_message），避免在要求 user 消息的 provider 上整类任务失败。

    ✅ R38（2026-10-03）修复三处修复轮缺陷：
    1. ``first_raw``：修复目标恒为**模型最初的输出**，不再被上一轮修复结果覆盖
       （旧实现第 2 轮拿到的是「基于我上次修错的东西再修」，与
       ``outline_json_fix_system``「保留原有结构、最小必要修改」的要求相悖，
       且破坏不可逆 —— 第 1 轮把结构改坏后，第 2 轮已无从恢复）；
    2. 修复轮 ``chat_with_fallback`` 包 try/except：首轮已产出内容、仅业务校验
       未过时，修复轮的瞬时网络/限流故障不再把整次调用炸掉（首轮调用仍照旧
       向上抛，由调用方决定是否整体重来）；
    3. 修复轮失败信息并入最终异常文本，避免「修复失败」与「生成失败」混淆。
    """
    messages = _ensure_user_message(messages)
    try:
        raw = await chat_with_fallback(messages, temperature=temperature,
                                       json_mode=json_mode, timeout=timeout,
                                       scene=scene, max_tokens=max_tokens)
    except Exception:
        # ✅ R48（2026-10-06 · 运行时指标）：首轮 AI 调用失败/降级 → 记一次场景失败。
        #    观测指标，不改变「原样 re-raise」的抛错语义。
        record_ai_failure(scene)
        raise
    # 修复目标恒定为模型**最初**的输出（见 docstring 修复点 1）
    first_raw = raw
    obj, issues = parse_and_validate(raw, validate_fn)
    if obj is not None and not issues:
        return obj, raw

    # ✅ R48（2026-10-06 · 运行时指标）：首轮输出未过校验、进入修复循环 →
    #    按 repair_key 维度记一次修复触发。
    record_repair_triggered(repair_key)
    repair_errors: list[str] = []
    for attempt in range(max_retries):
        repair_msgs = list(messages) + [
            {"role": "assistant", "content": raw or ""},
            {"role": "user", "content": _build_repair_user_prompt(
                issues, first_raw or "", repair_key)},
        ]
        try:
            raw = await chat_with_fallback(repair_msgs, temperature=temperature,
                                           json_mode=json_mode, timeout=timeout,
                                           scene=scene, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            # 瞬时故障不应丢弃首轮已产出的内容：记录后继续下一轮修复预算
            repair_errors.append("第%d次修复调用异常: %s" % (attempt + 1, e))
            logger.warning("JSON 修复第 %d 次调用异常（继续使用剩余修复预算）：%s",
                           attempt + 1, e)
            continue
        obj, issues = parse_and_validate(raw, validate_fn)
        if obj is not None and not issues:
            return obj, raw
        logger.warning("JSON 修复第 %d 次仍未通过：%s", attempt + 1, issues[:3])

    if repair_errors:
        raise ValueError("JSON 生成/修复失败：%s；修复轮异常：%s"
                         % (issues[:5], " | ".join(repair_errors)))
    raise ValueError(f"JSON 生成/修复失败：{issues[:5]}")


async def _provider_chat_safe(provider, messages: list, temperature: float = 0.7,
                              max_tokens: int | None = None,
                              extra_body: dict | None = None) -> str:
    """调用 provider.chat，兼容不支持 temperature / max_tokens kwarg 的旧实现。

    ✅ R38（2026-10-03）：此前本函数只接受 3 个参数，导致
    ``collect_json_response_with_provider`` 签名上声明的 ``max_tokens`` /
    ``extra_body`` **从未被传给 provider**（静默失效）；而
    ``providers/base.py`` 的 ``chat()`` 明确支持这两个 kwarg。
    现按「逐个降级」透传：先全参数，TypeError 则逐个去掉再试，保证旧实现
    （只认 messages / 只认 temperature）仍可工作。
    """
    kwargs: dict = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    for trial in (kwargs, {"temperature": temperature}, {}):
        try:
            return await provider.chat(messages, **trial)
        except TypeError:
            if trial == {}:
                raise
    raise TypeError("provider.chat 签名不兼容")


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
    """provider 直连版 JSON 收集（自招投标方案平台移植，签名兼容源项目）

    与 collect_json_response 的区别：不经过 provider_factory 的降级链，
    而是直接使用调用方传入的 provider 实例（image_engine 编排流水线使用）。

    返回解析后的对象本身（与源项目一致，非二元组）。
    validator 兼容两种约定：
    - 源项目约定：validator(obj) -> (is_valid: bool, error_msg: str)
    - 本项目约定：validate_fn(obj) -> issues: list[str]（空列表=通过）

    ✅ 兼容性兜底：消息列表只有 system 时自动补一条最小 user 指令
    （见 _ensure_user_message），避免在要求 user 消息的 provider 上整类任务失败。

    ✅ R38（2026-10-03）：``max_tokens`` / ``extra_body`` 现真正透传给 provider
    （此前声明即失效）；新增 ``repair_key``，插图编排等非目录任务不再拿目录
    修复提示词。
    """
    messages = _ensure_user_message(messages)
    raw = await _provider_chat_safe(provider, messages, temperature,
                                    max_tokens, extra_body)
    first_raw = raw
    obj, issues = parse_and_validate(raw, None)
    if obj is not None and not issues and validator is not None:
        issues = _run_validator(validator, obj)
    if obj is not None and not issues:
        if schema is not None:
            try:
                obj = schema.model_validate(obj)
            except Exception as e:  # noqa: BLE001
                # schema 校验失败不应直接崩溃：转为 issues 进入修复轮
                issues = [f"Schema 校验失败：{e}"]
                obj = None
        if obj is not None:
            return obj

    repair_errors: list[str] = []
    for attempt in range(max_retries):
        repair_msgs = list(messages) + [
            {"role": "assistant", "content": raw or ""},
            {"role": "user", "content": _build_repair_user_prompt(
                issues, first_raw or "", repair_key)},
        ]
        try:
            raw = await _provider_chat_safe(provider, repair_msgs, temperature,
                                            max_tokens, extra_body)
        except Exception as e:  # noqa: BLE001
            repair_errors.append("第%d次修复调用异常: %s" % (attempt + 1, e))
            logger.warning("JSON 修复第 %d 次调用异常（继续使用剩余修复预算）：%s",
                           attempt + 1, e)
            continue
        obj, issues = parse_and_validate(raw, None)
        if obj is not None and not issues and validator is not None:
            issues = _run_validator(validator, obj)
        if obj is not None and not issues:
            if schema is not None:
                try:
                    obj = schema.model_validate(obj)
                except Exception as e:  # noqa: BLE001
                    issues = [f"Schema 校验失败：{e}"]
                    obj = None
            if obj is not None:
                return obj
        logger.warning("JSON 修复第 %d 次仍未通过：%s", attempt + 1, issues[:3])

    if repair_errors:
        raise ValueError("JSON 生成/修复失败：%s；修复轮异常：%s"
                         % (issues[:5], " | ".join(repair_errors)))
    raise ValueError(f"JSON 生成/修复失败：{issues[:5]}")


def _run_validator(validator, obj) -> list:
    """兼容源项目 (is_valid, error_msg) 与本项目 issues 列表两种 validator 约定"""
    result = validator(obj)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_valid, error_msg = result
        return [] if is_valid else [str(error_msg)]
    if isinstance(result, list):
        return result
    if result:
        return [str(result)]
    return []


# ------------------------------------------------------------
# 标题内嵌编号清洗 + 目录树编号重排
#
# ✅ 编号统一（2026-09-25）：核心实现收敛到 services/numbering.py 唯一事实源，
#    本模块保留同名薄包装 / 再导出（调用方众多：sse_handlers / outline_library /
#    sections / tests 均按 json_response.renumber_outline 引用，签名与语义不变）。
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