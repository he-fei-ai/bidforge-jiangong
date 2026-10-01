"""JSON 模式兼容性判定（G9）回归护栏 —— 2026-10-01。

背景（参考软件对齐缺口 G9）
--------------------------
参考软件用 ``err.includes('response_format')`` 判定「厂商不支持 JSON 模式」，
匹配不上**中文报错**。本仓原实现同样只认英文关键词
（``response_format`` / ``json_object`` / ``json mode``），而国内厂商拒绝
JSON 模式时返回的多是中文（「该模型暂不支持 JSON 输出」等），后果有三：

  1. 不回退普通模式 —— ``_attempt_candidate`` 的「JSON 优先 + 普通兜底」
     链条只跑 ``use_json=True`` 这一支；
  2. 该错误仍被计入**熔断失败**与**配额冷却**，一次参数不兼容被当成
     provider 故障，污染降级链健康度；
  3. 整条候选链用同一原因逐个失败 —— 目录生成 / 事实提取 / 一致性审计等
     **全部 JSON 类任务**同时挂死。

本护栏锁定：
  - 英文关键词**向后兼容**（旧行为逐字不变）；
  - 中文报错必须被识别；
  - 泛化否定词（「不支持」类）必须与 JSON 语义词**组合**才判定，
    否则「不支持该地域」会被误判、白烧一次重试；
  - 判据是**单一事实源**（全仓不得出现第二份 JSON 模式关键词表）。

测试策略：纯函数断言 + AST/源码静态断言，零 AI、零 DB。
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.ai.json_mode_compat import (  # noqa: E402
    JSON_MODE_HINT_TOKENS, JSON_MODE_NEGATION_TOKENS, JSON_MODE_TOKENS,
    json_mode_unsupported, response_format_rejected,
)
from app.services.ai.provider_factory import _json_mode_unsupported  # noqa: E402
from app.services.ai.providers.openai_compatible import (  # noqa: E402
    _is_response_format_unsupported,
)

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(BACKEND, "app")
PF_PATH = os.path.join(APP_DIR, "services", "ai", "provider_factory.py")
JMC_PATH = os.path.join(APP_DIR, "services", "ai", "json_mode_compat.py")
OC_PATH = os.path.join(APP_DIR, "services", "ai", "providers",
                       "openai_compatible.py")


def _src(path: str) -> str:
    with io.open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. 英文关键词向后兼容（旧行为逐字不变）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    "Unsupported parameter: 'response_format' is not supported with this model.",
    "Invalid parameter: response_format",
    "json_object is not supported by this model",
    "JSON mode is not supported",
    "This model does not support json mode",
    "HTTP 400 Bad Request: json",
    "400 invalid json body",
])
def test_english_errors_still_detected(err):
    assert _json_mode_unsupported(err) is True


# ---------------------------------------------------------------------------
# 2. 中文报错必须被识别（本轮修复点）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    "该模型暂不支持 JSON 输出，请更换模型",
    "参数错误：不支持 response_format 参数",
    "此模型不支持json模式，请使用 text 模式",
    "当前模型仅支持文本输出",
    "暂不支持json_object，请调整请求参数",
    "参数无效：json mode 无法开启",
    "invalid parameter: json schema is not allowed",
])
def test_chinese_errors_detected(err):
    assert _json_mode_unsupported(err) is True, (
        f"中文报错未被识别 → 不回退普通模式且污染熔断：{err}")


def test_chinese_detection_is_case_insensitive_on_ascii_part():
    assert _json_mode_unsupported("不支持 JSON 输出") is True
    assert _json_mode_unsupported("不支持 json 输出") is True
    assert _json_mode_unsupported("不支持 JSon 输出") is True


# ---------------------------------------------------------------------------
# 3. 反例：无关错误不得被误判（避免白烧一次普通模式重试）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    "",
    None,
    "403 Forbidden",
    "429 Too Many Requests",
    "500 Internal Server Error",
    "ReadTimeout",
    "Connection reset by peer",
    "不支持该地域，请检查 region 配置",      # 「不支持」无 JSON 语义
    "不支持的操作系统：Windows_NT",         # 「不支持」无 JSON 语义
    "unsupported region: ap-northeast-1",   # unsupported 无 JSON 语义
    "Unsupported parameter: temperature",   # unsupported 无 JSON 语义
    "参数错误：messages 不能为空",           # 参数错误无 JSON 语义
    "invalid region",                       # invalid parameter 不匹配
])
def test_unrelated_errors_not_detected(err):
    assert _json_mode_unsupported(err) is False, (
        f"误判为 JSON 模式问题 → 白烧一次普通模式重试：{err}")


def test_empty_and_none_are_false():
    assert _json_mode_unsupported("") is False
    assert _json_mode_unsupported(None) is False
    assert _json_mode_unsupported(12345) is False


# ---------------------------------------------------------------------------
# 4. 判据为单一事实源
# ---------------------------------------------------------------------------

def test_keyword_tables_are_module_level_tuples():
    """关键词必须集中在模块级常量，禁止内联在函数体里。"""
    for table in (JSON_MODE_TOKENS, JSON_MODE_NEGATION_TOKENS,
                  JSON_MODE_HINT_TOKENS):
        assert isinstance(table, tuple)
        assert all(isinstance(t, str) and t for t in table)
    # 直接命中的关键词必须都小写（判定前统一 lower()）
    assert all(t == t.lower() for t in JSON_MODE_TOKENS)


def test_negation_tokens_never_alone_are_sufficient():
    """反向不变式：任一泛化否定词单独出现都不得触发判定。

    保证「组合命中」这条规则不被后人改成短路 or。
    """
    for tok in JSON_MODE_NEGATION_TOKENS:
        probe = f"系统{tok}该配置项"
        assert json_mode_unsupported(probe) is False, (
            f"泛化否定词 {tok!r} 在无 JSON 语义时被单独命中")


def test_provider_layer_markers_preserved_after_convergence():
    """收敛不得丢失 HTTP 层旧 marker（否则属于静默行为回退）。

    旧实现独有、本轮收敛进唯一事实源时必须保留的 marker。
    """
    for marker in ("does not support", "not support", "unknown parameter",
                   "must be"):
        assert marker in JSON_MODE_NEGATION_TOKENS, (
            f"HTTP 层旧 marker {marker!r} 在收敛中丢失（行为回退）")


# ---------------------------------------------------------------------------
# 4. HTTP 层：第二份判据已收敛为薄包装
# ---------------------------------------------------------------------------

def test_provider_layer_delegates_to_single_exit():
    """providers 侧必须转发到唯一出口，不得自带关键词表。"""
    for status, body in ((400, "该模型暂不支持 JSON 输出"),
                         (400, "invalid parameter: response_format"),
                         (400, "this model does not support response_format"),
                         (500, "response_format not supported"),
                         (400, "不支持该地域")):
        assert _is_response_format_unsupported(status, body) == \
            response_format_rejected(status, body), (
            f"providers 侧与唯一出口结论不一致：({status}, {body!r})")


def test_http_layer_detects_chinese_and_english():
    assert response_format_rejected(400, "该模型暂不支持 JSON 输出") is True
    assert response_format_rejected(400, "参数错误：不支持 response_format") is True
    assert response_format_rejected(400, "unsupported parameter: response_format") is True
    assert response_format_rejected(400, "unknown parameter: response_format") is True
    # 旧行为保留：直接点名参数名即命中（不要求否定词）
    assert response_format_rejected(400, "{'error': 'response_format'}") is True


def test_http_layer_rejects_non_400_and_unrelated():
    assert response_format_rejected(500, "response_format not supported") is False
    assert response_format_rejected(429, "rate limited") is False
    assert response_format_rejected(403, "insufficient balance") is False
    assert response_format_rejected(400, "不支持该地域") is False
    assert response_format_rejected(400, "messages 不能为空") is False
    assert response_format_rejected(400, "") is False


def test_http_layer_tolerates_garbage_status():
    """状态码不可解析时不得抛异常，应降级为「只看正文」。"""
    assert response_format_rejected(None, "response_format not supported") is True
    assert response_format_rejected("abc", "response_format not supported") is True
    assert response_format_rejected(None, "") is False


def test_factory_wrapper_delegates_to_single_exit():
    """编排层薄包装必须与唯一出口逐字一致（含中文与反例）。"""
    for err in ("该模型暂不支持 JSON 输出", "json_object is not supported",
                "不支持该地域", "429 Too Many Requests", "", None):
        assert _json_mode_unsupported(err) == json_mode_unsupported(err)


def test_no_second_json_mode_keyword_table_in_app():
    """全仓静态扫描：JSON 模式关键词只能出现在唯一事实源一处。

    两个调用方（``provider_factory`` / ``providers/openai_compatible``）必须
    是**薄包装** —— 不得自带任何「关键词匹配」代码，否则判据再次分叉
    （本轮修复的根因正是这种分叉：编排层与 HTTP 层各一份、都是英文硬编码）。

    判据必须用 **AST** 而非纯文本：`payload["response_format"] =
    {"type": "json_object"}` 是**发送**参数（合法且必要），与「拿关键词去
    匹配报错文本」是完全不同的两件事，纯文本扫描无法区分、会假失败。
    """
    needles = ("json_object", "json mode", "does not support",
               "unknown parameter", "response_format")
    allowed = {os.path.abspath(PF_PATH), os.path.abspath(JMC_PATH)}
    offenders: list[str] = []

    #: 「请求字段容器」类变量名：``"x" in payload`` / ``"x" not in _stream_downgraded``
    #: 都是**检查该字段是否存在于请求体 / 是否已被摘除**（合法且必要，如
    #: ``if "response_format" in payload and json_mode: ...``），与
    #: 「拿关键词去匹配厂商报错文本」是完全不同的两件事，不能一并禁止。
    field_container_names = ("payload", "body", "data", "headers",
                             "extra_body", "downgraded")

    def _needle_of(node) -> str | None:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            return None
        low = node.value.lower()
        return low if any(n in low for n in needles) else None

    def _is_payload_ref(node) -> bool:
        return (isinstance(node, ast.Name)
                and any(k in node.id.lower() for k in field_container_names))

    for root, _dirs, files in os.walk(APP_DIR):
        if "__pycache__" in root:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) in allowed:
                continue
            tree = ast.parse(_src(path), filename=path)
            rel = os.path.relpath(path, BACKEND)
            for node in ast.walk(tree):
                hit = None
                if isinstance(node, ast.Compare) and any(
                        isinstance(op, (ast.In, ast.NotIn))
                        for op in node.ops):
                    # 关键词作为比较左侧参与 `"x" in text` 判定；
                    # 另一侧是请求体变量 → 属字段存在性检查，放行
                    sides = [node.left, *node.comparators]
                    literals = [s for s in sides if not _is_payload_ref(s)]
                    if len(literals) < len(sides):
                        continue
                    for side in literals:
                        hit = hit or _needle_of(side)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    # 关键词作为 text.find("x") / startswith("x") 的参数
                    if node.func.attr in ("find", "startswith", "endswith",
                                          "count", "split"):
                        for arg in list(node.args) + [k.value for k in node.keywords]:
                            hit = hit or _needle_of(arg)
                if hit:
                    offenders.append(f"{rel}:{node.lineno}:{hit!r}")
    assert not offenders, (
        f"发现第二份 JSON 模式关键词匹配逻辑（判据分叉的起点）：{offenders}")


def test_call_sites_are_thin_wrappers_only():
    """两个调用点必须只转发，不得内联 any('json' in str(e) ...) 之类判据。"""
    pf_tree = ast.parse(_src(PF_PATH))
    calls = [n for n in ast.walk(pf_tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_json_mode_unsupported"]
    assert len(calls) == 1, "编排层 JSON 模式判据的调用点必须唯一"
    func_names = {n.name for n in ast.walk(pf_tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_json_mode_unsupported" in func_names
    assert func_names & {"_is_json_mode_unsupported",
                         "json_mode_unsupported",
                         "_json_mode_supported"} == set(), (
        "编排层出现同义命名的第二份判据函数")

    # providers 侧：所有调用点必须经由薄包装
    oc_tree = ast.parse(_src(OC_PATH))
    direct = [
        n for n in ast.walk(oc_tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "response_format_rejected"
    ]
    assert len(direct) == 1, "providers 侧必须只经由 json_mode_compat 单一出口"
    # 不得再有第二个同名类定义（历史遗留：一处被下方定义整体覆盖，
    # 导致「改上面的 _ensure_user_message 完全不生效」这种静默陷阱）
    classes = [n.name for n in ast.walk(oc_tree)
               if isinstance(n, ast.ClassDef)]
    assert len(classes) == len(set(classes)), (
        f"providers/openai_compatible.py 存在重复类定义：{classes}")


def test_modules_parse():
    for path in (PF_PATH, JMC_PATH, OC_PATH):
        ast.parse(_src(path), filename=path)