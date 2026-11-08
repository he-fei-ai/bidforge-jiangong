"""R181（2026-11-07 · 第二批）：静默硬截断可观测化（A 类剩余 + B 类持久化/用户可见）。

本批按三类判据分流，**不得一刀切**：

* 模型/用户可见（事实摘要、文种识别补充文本、图表修复错误、审计详情）
  → ``clip_text_notice``：超长时追加「原文 N 字 / 已省略 M 字」说明；
* 只喂内部判定（危大分类关键词匹配、事实启发式兜底）
  → ``clip_text_logged``：**只记日志/指标**，返回值与旧 ``text[:limit]`` 逐字节一致，
  绝不向用于匹配的字符串插标记（插说明会改变匹配结果，等于用可观测性换
  另一种静默错误）；
* 落库类（``uploaded_outlines.raw_text`` / ``ai_config_audit_logs.detail`` /
  ``ai_configs.remark``）：三列均为 SQLite ``TEXT``，容量足够 → 存全文；
  **未改表结构**。

验证口径：未超长逐字节不变；超长有标记或有日志；「只记日志」点用 caplog
断言日志出现而返回值不变；AST 防复发锁：任一点退回裸切片立即变红。
"""

import ast
import io
import json
import logging
from pathlib import Path

import app.routers.charts as charts
import app.routers.upload_outline as uo
import app.services.facts_extractor as fe
import pytest
from app.routers.ai_config.config import _classify_import_item
from app.routers.sse_handlers import _compact_outline_json
from app.services import audit_service, genre_assist, preflight_engine
from app.services.ai import provider_factory as pf
from app.services.prompt_material_guard import clip_text_logged, clip_text_notice
from fastapi import HTTPException
from starlette.datastructures import UploadFile

APP = Path(__file__).resolve().parents[1] / "app"

# 足够长且带清晰句末边界的文本（用于「不切断」断言）
LONG_TEXT = "".join(f"第{i}项说明：现场应按规范组织施工并做好记录。" for i in range(1, 200))


def _split_notice(out: str) -> tuple[str, str]:
    idx = out.find("\n\n[……")
    assert idx >= 0, f"缺少模型可见截断说明：{out[:80]!r}"
    return out[:idx], out[idx:]


def _assert_notice(out: str, total: int) -> str:
    body, notice = _split_notice(out)
    assert "已截断" in notice and f"原文 {total} 字" in notice and "已省略" in notice
    return body


def _upload(name: str = "outline.txt", text: str = "") -> UploadFile:
    return UploadFile(file=io.BytesIO(text.encode("utf-8")), filename=name)


# ---------------------------------------------------------------------------
# 1. clip_text_logged：内部判定专用，只记日志、文本逐字节等于旧裸切片
# ---------------------------------------------------------------------------


def test_clip_text_logged_short_is_byte_identical():
    text = "基坑深度 6m"
    assert clip_text_logged(text, 2000, "匹配文本") == text


def test_clip_text_logged_long_equals_raw_slice_and_only_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="prompt_material_guard"):
        out = clip_text_logged(LONG_TEXT, 100, "匹配文本")
    assert out == LONG_TEXT[:100], "返回值必须与旧裸切片逐字节一致"
    assert "[……" not in out and "已截断" not in out, "内部判定文本不得插入任何标记"
    assert "上下文截断（仅日志，匹配文本）" in caplog.text
    assert f"已省略 {len(LONG_TEXT) - 100} 字" in caplog.text


def test_clip_text_logged_empty_and_non_positive_limit():
    assert clip_text_logged("", 100, "x") == ""
    assert clip_text_logged("abc", 0, "x") == "abc"
    assert clip_text_logged("abc", -1, "x") == "abc"


# ---------------------------------------------------------------------------
# 2. 文种识别补充文本（进模型提示词）
# ---------------------------------------------------------------------------


async def _genre_user_text(scheme_name: str, extra_text: str) -> str:
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["user"] = messages[1]["content"]
        return "{}"

    await genre_assist.suggest_category(scheme_name, extra_text, chat_fn=fake_chat)
    return captured["user"]


async def test_genre_assist_short_extra_is_byte_identical():
    user = await _genre_user_text("深基坑专项施工方案", "补充短文本。")
    assert user == "方案名称：深基坑专项施工方案\n补充文本：补充短文本。"


async def test_genre_assist_long_extra_has_notice():
    extra = LONG_TEXT
    user = await _genre_user_text("深基坑专项施工方案", extra)
    body = _assert_notice(user, len(extra))
    assert body.endswith("。"), "切点应落在句末，不得切在句子中间"
    assert body.startswith("方案名称：深基坑专项施工方案\n补充文本：")


# ---------------------------------------------------------------------------
# 2b. 目录描述（进审核修复提示词，模型可见）
# ---------------------------------------------------------------------------


def test_compact_outline_json_short_desc_byte_identical():
    payload = json.loads(_compact_outline_json([{"title": "第一章", "description": "短描述"}], max_desc=80))
    assert payload["outline"][0]["description"] == "短描述"


def test_compact_outline_json_long_desc_has_notice():
    payload = json.loads(_compact_outline_json([{"title": "第一章", "description": LONG_TEXT}], max_desc=80))
    body = _assert_notice(payload["outline"][0]["description"], len(LONG_TEXT))
    assert body.endswith("。"), "切点应落在句末"
    assert len(body) <= 80


# ---------------------------------------------------------------------------
# 3. 图表修复错误信息（进模型提示词，JSON / Mermaid 两分支同型）
# ---------------------------------------------------------------------------


async def _capture_chart_error(monkeypatch, code: str, error: str) -> str:
    captured: list[str] = []

    def fake_render(name, **kwargs):
        captured.append(kwargs.get("error"))
        return "p"

    async def fake_chat(*args, **kwargs):
        return ""

    monkeypatch.setattr(charts, "render", fake_render)
    monkeypatch.setattr(charts, "chat_with_fallback", fake_chat)
    with pytest.raises(HTTPException):
        await charts.fix_mermaid({"code": code, "error": error}, db=None)
    assert captured, "未进入 AI 修复提示词渲染"
    return captured[0]


async def test_fix_mermaid_short_error_is_byte_identical(monkeypatch):
    assert await _capture_chart_error(monkeypatch, "flowchart TD\n A-->B", "短错误") == "短错误"


async def test_fix_mermaid_long_error_has_notice(monkeypatch):
    error = "错" * 600
    out = await _capture_chart_error(monkeypatch, "flowchart TD\n A-->B", error)
    body = _assert_notice(out, 600)
    assert body == "错" * 500, "无自然边界时应退化为原上限硬切"


JSON_CODE = '{"type": "labor", "labels": ["钢筋工"], "values": [12]}'


async def test_fix_mermaid_json_short_error_is_byte_identical(monkeypatch):
    assert await _capture_chart_error(monkeypatch, JSON_CODE, "短错误") == "短错误"


async def test_fix_mermaid_json_long_error_has_notice(monkeypatch):
    error = "错" * 600
    out = await _capture_chart_error(monkeypatch, JSON_CODE, error)
    body = _assert_notice(out, 600)
    assert body == "错" * 500


# ---------------------------------------------------------------------------
# 4. 危大判定匹配文本（只喂内部关键词/正则，只记日志）
# ---------------------------------------------------------------------------


def _patch_danger_check(monkeypatch, captured: dict):
    def fake_check(scheme_name, facts, extra_text=None):
        captured["extra_text"] = extra_text
        return {"classification": {"hazards": []}}

    monkeypatch.setattr(preflight_engine, "facts_danger_check", fake_check)


def test_preflight_extra_text_short_is_byte_identical(monkeypatch, caplog):
    captured = {}
    _patch_danger_check(monkeypatch, captured)
    facts = [{"name": "基坑深度", "value": "6m"}]
    ctx = preflight_engine.PreflightContext(scheme_name="某工程", facts=facts)
    with caplog.at_level(logging.WARNING, logger="prompt_material_guard"):
        assert preflight_engine.check_hazard_params(ctx) == []
    assert captured["extra_text"] == "基坑深度 6m"
    assert "上下文截断（仅日志" not in caplog.text


def test_preflight_extra_text_long_only_logs_and_value_unchanged(monkeypatch, caplog):
    captured = {}
    _patch_danger_check(monkeypatch, captured)
    facts = [{"name": "基坑深度", "value": "甲" * 3000}]
    expected = " ".join(f"{f.get('name') or ''} {f.get('value') or ''}" for f in facts)
    assert len(expected) > 2000
    ctx = preflight_engine.PreflightContext(scheme_name="某工程", facts=facts)
    with caplog.at_level(logging.WARNING, logger="prompt_material_guard"):
        assert preflight_engine.check_hazard_params(ctx) == []
    assert captured["extra_text"] == expected[:2000], "匹配文本必须与旧裸切片逐字节一致"
    assert "[……" not in captured["extra_text"] and "已截断" not in captured["extra_text"]
    assert "上下文截断（仅日志，危大判定匹配文本）" in caplog.text
    assert f"已省略 {len(expected) - 2000} 字" in caplog.text


# ---------------------------------------------------------------------------
# 5. 本地事实摘要（进模型提示词的全局锚点）
# ---------------------------------------------------------------------------


def _summary_chunks(n: int = 12) -> list[str]:
    lines = [f"项目名称 编号{i} " + "甲" * 100 + "。" for i in range(n)]
    return ["\n".join(lines)]


def test_local_summary_short_is_byte_identical():
    out = fe._build_local_summary(["项目名称：某工程\n"], max_chars=800)
    assert out == "项目名称：某工程"


def test_local_summary_long_has_notice():
    chunks = _summary_chunks()
    full = fe._build_local_summary(chunks, max_chars=10**9)
    out = fe._build_local_summary(chunks, max_chars=800)
    body = _assert_notice(out, len(full))
    # 边界优先级：行 > 句；按行收刀时保留完整行（行尾即句末）
    assert body.endswith("\n"), "切点应落在行边界"
    assert body.rstrip("\n").endswith("。"), "保留的最后一行不得被切断"
    assert len(body) <= 800


# ---------------------------------------------------------------------------
# 6. B 类落库：列均为 TEXT → 存全文（未改表结构）
# ---------------------------------------------------------------------------


async def test_upload_outline_long_raw_text_stored_full(db_conn):
    text = ("第一章 工程概况\n" + "内容填充行用于构造超长原文。\n") * 240
    assert len(text) > 5000
    res = await uo.parse_outline(file=_upload("long.txt", text), db=db_conn, scheme_name="", reorganize=False)
    assert "raw_text_truncated" not in res
    cur = await db_conn.execute("SELECT raw_text FROM uploaded_outlines WHERE id=?", (res["id"],))
    row = await cur.fetchone()
    assert row is not None and row["raw_text"] == text


async def test_upload_outline_short_raw_text_byte_identical(db_conn):
    text = "第一章 工程概况\n第二章 施工部署\n"
    res = await uo.parse_outline(file=_upload("small.txt", text), db=db_conn, scheme_name="", reorganize=False)
    cur = await db_conn.execute("SELECT raw_text FROM uploaded_outlines WHERE id=?", (res["id"],))
    row = await cur.fetchone()
    assert row is not None and row["raw_text"] == text


async def test_audit_detail_kept_full_for_text_column(db_conn):
    detail = "变更说明" * 200
    assert len(detail) > 500
    await audit_service.record_config_audit(db_conn, "update", detail=detail)
    cur = await db_conn.execute("SELECT detail FROM ai_config_audit_logs")
    row = await cur.fetchone()
    assert row is not None and row["detail"] == detail


async def test_audit_detail_short_byte_identical(db_conn):
    detail = "新增：plan=pay_as_you_go"
    await audit_service.record_config_audit(db_conn, "create", detail=detail)
    cur = await db_conn.execute("SELECT detail FROM ai_config_audit_logs")
    row = await cur.fetchone()
    assert row is not None and row["detail"] == detail


def test_import_remark_kept_full():
    long_remark = "备注" * 3000
    item = _classify_import_item(
        {
            "provider_name": "openai",
            "model": "gpt-4o",
            "plan": "pay_as_you_go",
            "base_url": "https://api.test.com/v1",
            "remark": long_remark,
        },
        existing=set(),
        overwrite=False,
    )
    assert item["remark"] == long_remark


def test_import_remark_short_byte_identical():
    item = _classify_import_item(
        {
            "provider_name": "openai",
            "model": "gpt-4o",
            "plan": "pay_as_you_go",
            "base_url": "https://api.test.com/v1",
            "remark": "  主力模型  ",
        },
        existing=set(),
        overwrite=False,
    )
    assert item["remark"] == "主力模型"


async def test_save_ai_config_remark_kept_full(db_conn):
    long_remark = "备注" * 2500
    cid = await pf.save_ai_config(
        {
            "provider_name": "openai",
            "model": "gpt-4o",
            "plan": "pay_as_you_go",
            "base_url": "https://api.test.com/v1",
            "remark": long_remark,
        }
    )
    cur = await db_conn.execute("SELECT remark FROM ai_config WHERE id=?", (cid,))
    row = await cur.fetchone()
    assert row is not None and row["remark"] == long_remark


async def test_save_ai_config_remark_short_byte_identical(db_conn):
    cid = await pf.save_ai_config(
        {
            "provider_name": "openai",
            "model": "gpt-4o",
            "plan": "pay_as_you_go",
            "base_url": "https://api.test.com/v1",
            "remark": "主力模型",
        }
    )
    cur = await db_conn.execute("SELECT remark FROM ai_config WHERE id=?", (cid,))
    row = await cur.fetchone()
    assert row is not None and row["remark"] == "主力模型"


# ---------------------------------------------------------------------------
# 7. 接线锁：内部判定点必须用 clip_text_logged（不得改用可见标记函数）
# ---------------------------------------------------------------------------


def test_internal_judgement_points_use_logged_clip():
    facts_src = (APP / "services" / "facts_extractor.py").read_text(encoding="utf-8")
    preflight_src = (APP / "services" / "preflight_engine.py").read_text(encoding="utf-8")
    assert 'clip_text_logged(full_text, 3000, "事实启发式兜底原文")' in facts_src
    assert 'summary=clip_text_notice(context_summary, 500, "全文摘要")' in facts_src
    assert 'summary = clip_text_notice(summary, max_chars, "本地事实摘要")' in facts_src
    assert "extra_text = clip_text_logged(" in preflight_src


# ---------------------------------------------------------------------------
# 8. AST 防复发锁：任何一点退回裸切片立刻变红
# ---------------------------------------------------------------------------

#: (相对 app 的路径, 变量名, 旧裸切片上限)
BARE_SLICE_SPECS = [
    ("routers/charts.py", "last_error", 500),
    ("routers/upload_outline.py", "raw_text", 5000),
    ("services/facts_extractor.py", "context_summary", 500),
    ("services/facts_extractor.py", "full_text", 3000),
    ("services/genre_assist.py", "extra_text", 800),
    ("services/preflight_engine.py", "extra_text", 2000),
]


def _module_tree(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _name_used(tree: ast.AST, name: str) -> bool:
    """模块里是否真的用了这个**标识符**（``Name`` 节点）。

    注释 / docstring / 普通字符串字面量里的同名文字不产生 ``Name`` 节点，因此
    不会触发 —— 这正是「别再被散文绊倒」的判据。
    """
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(tree))


def _string_key_used(tree: ast.AST, key: str) -> bool:
    """模块里是否把该字符串当**下标键**用（``x["key"]`` / ``result["key"] = ...``）。

    只认「键访问」这一种结构性位置；docstring、日志文案、普通字面量里的同名
    文字都不会被当成键，故不触发。
    """
    return any(
        isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == key
        for n in ast.walk(tree)
    )


def test_no_bare_slice_regressions():
    problems: list[str] = []
    for rel, var, limit in BARE_SLICE_SPECS:
        tree = _module_tree(rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.slice, ast.Slice):
                continue
            upper = node.slice.upper
            if not isinstance(upper, ast.Constant) or upper.value != limit:
                continue
            if isinstance(node.value, ast.Name) and node.value.id == var:
                problems.append(f"{rel}: {var}[:{limit}] 裸切片回流")

    # summary[:max_chars]（上限来自参数名，不是常量）
    facts_tree = _module_tree("services/facts_extractor.py")
    for node in ast.walk(facts_tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        upper = node.slice.upper
        if (
            isinstance(upper, ast.Name)
            and upper.id == "max_chars"
            and isinstance(node.value, ast.Name)
            and node.value.id == "summary"
        ):
            problems.append("services/facts_extractor.py: summary[:max_chars] 裸切片回流")

    # desc[:budget[0]]（目录描述，上限取自 budget 列表）
    review_tree = _module_tree("routers/_sse_outline_review.py")
    for node in ast.walk(review_tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        upper = node.slice.upper
        if (
            isinstance(upper, ast.Subscript)
            and isinstance(upper.value, ast.Name)
            and upper.value.id == "budget"
            and isinstance(node.value, ast.Name)
            and node.value.id == "desc"
        ):
            problems.append("routers/_sse_outline_review.py: desc[:budget[0]] 裸切片回流")

    # 落库列不得再按旧上限裁剪
    for rel in ("routers/ai_config/config.py", "services/ai/provider_factory.py"):
        tree = _module_tree(rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
                continue
            upper = node.slice.upper
            if not isinstance(upper, ast.Constant) or upper.value != 2000:
                continue
            if "remark" in ast.unparse(node.value).lower():
                problems.append(f"{rel}: remark[:2000] 裸切片回流")

    # ✅ 2026-11-08：两处原整文件子串判断改为 AST —— 整文件 ``in`` 分不清代码与
    #    散文：``upload_outline.py`` 那句「不再回传 raw_text_truncated」的**说明
    #    文字**曾让全量连续两轮误红，而 ``git show HEAD:`` 源码零命中。
    #    ``_DETAIL_MAX`` 认标识符（常量定义与其切片使用都产生 Name 节点）；
    #    ``raw_text_truncated`` 认**下标键**（``result["raw_text_truncated"]``），
    #    两者的同名散文都不再触发。
    if _name_used(_module_tree("services/audit_service.py"), "_DETAIL_MAX"):
        problems.append("services/audit_service.py: _DETAIL_MAX 截断常量回流")
    if _string_key_used(_module_tree("routers/upload_outline.py"), "raw_text_truncated"):
        problems.append("routers/upload_outline.py: raw_text_truncated 标记回流")

    assert not problems, "发现静默硬截断回流：" + "；".join(problems)


def test_regression_detector_ignores_prose_but_catches_code():
    """探针自检：同样的词写进 docstring / 注释 / 普通字面量不触发，写成代码才触发。"""
    prose = '''
"""说明文字：不再回传 raw_text_truncated，也不再使用 _DETAIL_MAX。"""
# 注释里也写 raw_text_truncated 与 _DETAIL_MAX
MSG = "raw_text_truncated / _DETAIL_MAX 均已废弃"
'''
    prose_tree = ast.parse(prose)
    assert not _name_used(prose_tree, "_DETAIL_MAX")
    assert not _string_key_used(prose_tree, "raw_text_truncated")

    assert _name_used(ast.parse("_DETAIL_MAX = 500"), "_DETAIL_MAX")
    assert _string_key_used(
        ast.parse('def f(result):\n    result["raw_text_truncated"] = True\n    return result\n'),
        "raw_text_truncated",
    )
