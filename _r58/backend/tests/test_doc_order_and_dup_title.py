"""文档序（前序 DFS）与同名章节守卫测试（2026-09-22 遗留修复回归）

覆盖三项修复：
1. compliance.py 四处 sections 消费（/check、/expert-review、
   /consistency-audit、/preflight 上下文）全部先过 order_sections_dfs，
   不再直接消费 `ORDER BY sort_order` 扁平序（sort_order 是同级序号非文档序）。
2. consistency_scanner.load_leaf_sections 返回目录树前序 DFS 序。
3. preflight_engine.check_completeness 同名章节正文聚合：
   旧实现 by_title 字典后者覆盖前者，最后一个同名章节正文为空即误报。
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.services.audit_rules import rule_catalog
from app.services.preflight_engine import PreflightContext, check_completeness

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "doc-order.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "s", "目录已确认"))
    await db.commit()
    yield db, pid, sid


async def _insert_section(db, sid, title, content="", level=1, parent_id="",
                          sort_order=0, commit=False):
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", parent_id, title, "", level,
         "generated" if content else "empty",
         len(content), 0, content, "", sort_order))
    if commit:
        await db.commit()
    return sec_id


# ---------------------------------------------------------------------------
# 一、行为级：consistency_scanner.load_leaf_sections 返回文档序
# ---------------------------------------------------------------------------

async def test_load_leaf_sections_returns_dfs_order(db_ctx):
    """扁平 SQL 序（按"列"展开）与目录树文档序不同 → 断言修复生效。

    构造：章A(sort=1) → A-1(sort=1)、章B(sort=2)
    旧 ORDER BY sort_order 输出：A(1), A-1(1), B(2) 恰好与文档序一致的情况
    无法暴露问题；构造 A-1 的 sort_order 与另一章的某个深层节点同序的场景：
    章A(sort=1) 子 A-1(sort=2)；章B(sort=2) —— 扁平序 A, B, A-1（B 插到
    A-1 前面），文档序应为 A, A-1, B。
    """
    from app.services.consistency_scanner import load_leaf_sections

    db, _pid, sid = db_ctx
    a = await _insert_section(db, sid, "第一章", "内容A", level=1, sort_order=1)
    b = await _insert_section(db, sid, "第二章", "内容B", level=1, sort_order=2)
    a1 = await _insert_section(db, sid, "1.2节", "内容A1", level=2,
                               parent_id=a, sort_order=2)
    await db.commit()

    leaves = await load_leaf_sections(db, sid)
    titles = [r["title"] for r in leaves]
    # A 有正文子节点 A-1 → A 不是叶子；文档序：A-1（1.2节）先于 第二章
    assert titles == ["1.2节", "第二章"], f"实际顺序 {titles} 非前序 DFS 文档序"


# ---------------------------------------------------------------------------
# 二、源码级守卫：compliance 四处消费必须过 order_sections_dfs
# ---------------------------------------------------------------------------

def test_compliance_consumers_use_dfs_ordering():
    """守卫：防止回退为直接消费扁平 SQL 序（历史 pitfall 回归防线）。"""
    import app.routers.compliance as compliance

    src = inspect.getsource(compliance)
    # 四个函数体内都必须出现 order_sections_dfs 调用
    for fn in ("compliance_check", "expert_review", "run_consistency_audit",
               "_build_preflight_context"):
        fn_src = src[src.index(f"async def {fn}"):]
        # 截到下一个顶层 def 为止
        nxt = fn_src.find("\n@router", 10)
        fn_src = fn_src[:nxt] if nxt > 0 else fn_src
        assert "order_sections_dfs" in fn_src, f"{fn} 未过前序 DFS 重排"


# ---------------------------------------------------------------------------
# 三、preflight check_completeness：同名章节正文聚合不再误报
# ---------------------------------------------------------------------------

def _cmp_rule_with_keywords():
    for r in rule_catalog():
        rid = r.get("rule_id") if isinstance(r, dict) else getattr(r, "rule_id", "")
        if str(rid).startswith("CMP"):
            kws = r.get("keywords") if isinstance(r, dict) else getattr(r, "keywords", None)
            if kws:
                return rid, list(kws)
    pytest.skip("规则目录中无带关键词的 CMP 规则")


def test_check_completeness_duplicate_title_empty_not_false_positive():
    """同名两章：第一个有正文、第二个为空 → 旧实现 dict 覆盖后误报"正文为空"。"""
    rid, kws = _cmp_rule_with_keywords()
    kw = kws[0]
    sections = [
        {"id": "s1", "title": f"{kw}章节", "content": "扎实的正文内容" * 50,
         "word_count": 250, "parent_id": "", "level": 1},
        {"id": "s2", "title": f"{kw}章节", "content": "",
         "word_count": 0, "parent_id": "", "level": 1},
    ]
    ctx = PreflightContext(scheme_id="x", sections=sections)
    findings = check_completeness(ctx)
    hit = [f for f in findings if f.get("rule_id") == rid]
    # 同名章节任一有正文 → 不得报"存在章节但正文为空"
    assert not any("正文为空" in (f.get("detail") or "") for f in hit), \
        f"同名空章节覆盖有正文同名章，误报未消除: {hit}"


def test_check_completeness_all_duplicate_titles_empty_still_reports():
    """反向守卫：所有同名章节正文都为空 → 仍必须报"正文为空"（修复不得吞真问题）。"""
    rid, kws = _cmp_rule_with_keywords()
    kw = kws[0]
    sections = [
        {"id": "s1", "title": f"{kw}章节", "content": "",
         "word_count": 0, "parent_id": "", "level": 1},
        {"id": "s2", "title": f"{kw}章节", "content": "   ",
         "word_count": 0, "parent_id": "", "level": 1},
    ]
    ctx = PreflightContext(scheme_id="x", sections=sections)
    findings = check_completeness(ctx)
    assert any(f.get("rule_id") == rid and "正文为空" in (f.get("detail") or "")
               for f in findings), "全部同名章节为空时应报正文为空"
