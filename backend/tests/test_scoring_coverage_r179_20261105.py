# -*- coding: utf-8 -*-
"""R179 M1 评分项 → 目录映射校验（方案 §1.8 用例 1~17）。

覆盖面：
  - norm_title 归一（NFKC / 序号前缀 / 标点空白）
  - 四级匹配（exact / contains / keyword / criteria）与高档叶子约束
  - 三态四标签（mapped / missing / extra / unmatched）与 undetermined 降级
  - 抽取链路（去重 / UPSERT / stale 标记 / 页码取证 / v1 全扫语义）

变异验证点（方案 §1.8）：去掉 NFKC → 用例 1 红；keyword 阈值改 0 → 用例 3 红；
undetermined 输出 coverage_rate=0 或 items 标 missing → 用例 9/10/11 红；
高档项允许命中父节点 → 用例 4 红；extra 不排除祖先已映射 → 用例 8 红。
"""

import inspect

import pytest
from app.routers import scoring as scoring_router
from app.services import scoring_items as svc
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def _sec_rows(*specs):
    """specs: (id, parent_id, title) → sections 行（供 build_outline_tree）。"""
    return [
        {"id": sid, "parent_id": pid, "title": title, "sort_order": idx, "content": ""}
        for idx, (sid, pid, title) in enumerate(specs)
    ]


def _flat(*specs):
    return svc.flatten_outline(svc.build_outline_tree(list(_sec_rows(*specs))))


def _item(item_id, name, score=10, **kw):
    base = {
        "id": item_id,
        "project_id": "p1",
        "source_doc_id": "d1",
        "source_name": "评标办法.pdf",
        "item_no": kw.get("item_no", ""),
        "item_name": name,
        "score": score,
        "score_raw": f"{score}分" if score is not None else "",
        "criteria": kw.get("criteria", ""),
        "is_primary": 0,
        "page_ref": "",
        "page_num": None,
        "source_quote": "",
    }
    base.update({k: v for k, v in kw.items() if k in ("item_no", "criteria", "page_ref", "page_num", "source_quote")})
    return base


_DEFAULT_WHITELIST = ["附图", "附表", "附件", "封面", "目录", "编制说明"]


def _report(items, sections, **kw):
    return svc.build_coverage_report(
        items=items,
        sections=sections,
        thresholds=kw.pop("thresholds", None),
        extra_whitelist=kw.pop("extra_whitelist", _DEFAULT_WHITELIST),
        **kw,
    )


def _by_id(report):
    return {i["item_name"]: i for i in report["items"]}


# ---------------------------------------------------------------------------
# 匹配算法（用例 1~8）
# ---------------------------------------------------------------------------
def test_case01_fullwidth_and_numbering_norm_exact():
    """用例 1：全角数字 / 序号前缀 / 空白差异 → exact 命中，confidence 0.95。"""
    items = [_item("i1", "施工组织设计２")]  # 全角 2
    sections = _flat(("s1", "", "1、施 工 组 织 设 计2"))  # 半角 2 + 序号 + 空格
    rep = _report(items, sections)
    row = _by_id(rep)["施工组织设计２"]
    assert row["status"] == "mapped"
    assert row["method"] == "exact"
    assert row["confidence"] == pytest.approx(0.95)


def test_case02_synonym_keyword_hit():
    """用例 2：同义表达（词序不同）→ keyword 命中且 ≥ 阈值。"""
    items = [_item("i1", "安全管理、文明施工")]
    sections = _flat(("s1", "", "文明施工、安全管理"))  # 叶子
    rep = _report(items, sections)
    row = _by_id(rep)["安全管理、文明施工"]
    assert row["status"] == "mapped"
    assert row["method"] == "keyword"
    assert row["confidence"] >= 0.55


def test_case03_below_threshold_missing():
    """用例 3：相似度低于档位阈值 → missing（阈值改 0 时本用例必红）。"""
    items = [_item("i1", "安全管理、消防维护")]
    sections = _flat(("s1", "", "安全管理"))
    rep = _report(items, sections)
    row = _by_id(rep)["安全管理、消防维护"]
    assert row["status"] == "missing"
    assert row["method"] is None


def test_case04_high_band_rejects_parent_only():
    """用例 4：高档评分项只命中父节点 → missing（weak 不接受）。"""
    items = [_item("iA", "施工组织设计", score=60), _item("iB", "施工部署", score=10)]
    sections = _flat(("s1", "", "施工组织设计"), ("s1a", "s1", "施工部署"))
    rep = _report(items, sections)
    rows = _by_id(rep)
    assert rows["施工组织设计"]["status"] == "missing"
    assert rows["施工部署"]["status"] == "mapped"


def test_case05_multi_match_primary_unique():
    """用例 5：一个评分项命中多节点 → primary 唯一，其余进 matched_sections。"""
    items = [
        _item("iA", "安全管理", score=5),
        _item("iB", "质量管理", score=20),
        _item("iC", "进度管理", score=20),
    ]
    sections = _flat(
        ("s1", "", "安全管理"),
        ("s2", "", "安全管理措施"),
        ("s3", "", "质量管理"),
        ("s4", "", "进度管理"),
    )
    rep = _report(items, sections)
    row = _by_id(rep)["安全管理"]
    assert row["status"] == "mapped"
    assert len(row["matched_sections"]) == 2
    assert sum(1 for m in row["matched_sections"] if m["primary"]) == 1
    assert row["matched_sections"][0]["section_id"] == "s1"  # 置信度最高者为主
    assert row["also_section_ids"] == ["s2"]


def test_case06_overloaded_section_hint():
    """用例 6：多个评分项命中同一节点 → 均 mapped + overloaded 提示。"""
    items = [
        _item("i1", "安全管理 1", score=5),
        _item("i2", "安全管理 2", score=20),
        _item("i3", "安全管理 3", score=20),
    ]
    sections = _flat(("s1", "", "安全管理"))
    rep = _report(items, sections)
    assert all(i["status"] == "mapped" for i in rep["items"])
    assert len(rep["overloaded_sections"]) == 1
    assert rep["overloaded_sections"][0]["matched_item_count"] == 3


def test_case07_extra_leaves_and_whitelist():
    """用例 7：无支撑叶子 → extra；白名单节点（附图）不报。"""
    items = [_item("iA", "施工部署", score=10)]
    sections = _flat(
        ("s1", "", "施工组织设计"),
        ("s1a", "s1", "施工部署"),
        ("s2", "", "附图"),
        ("s3", "", "节能措施"),
    )
    rep = _report(items, sections)
    assert [e["title"] for e in rep["extra"]] == ["节能措施"]
    assert "附图" not in [e["title"] for e in rep["extra"]]


def test_case08_extra_skips_mapped_ancestors():
    """用例 8：祖先已被映射 → 后代叶子不报 extra。"""
    items = [
        _item("iA", "施工组织设计", score=5),  # 中档（5/45 < 0.15）→ 允许命中父节点
        _item("iB", "质量管理", score=20),
        _item("iC", "进度管理", score=20),
    ]
    sections = _flat(
        ("p1", "", "施工组织设计"),
        ("c1", "p1", "施工部署"),
        ("c2", "p1", "文明施工措施"),
        ("s3", "", "质量管理"),
        ("s4", "", "进度管理"),
    )
    rep = _report(items, sections)
    assert rep["extra"] == []


# ---------------------------------------------------------------------------
# 降级语义（用例 9~12，路由层口径）
# ---------------------------------------------------------------------------
async def _seed_scheme(db):
    await db.execute("INSERT INTO projects (id, name) VALUES ('p1', '测试项目')")
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES ('sc1', 'p1', '测试方案')")
    await db.commit()


async def test_case09_no_extraction_undetermined(db_conn, monkeypatch):
    """用例 9：抽取未跑过 → undetermined + coverage_rate=null + items 无 missing。"""
    await _seed_scheme(db_conn)
    resp = await scoring_router.scoring_coverage(scheme_id="sc1", thresholds=None, db=db_conn)
    assert resp["status"] == "undetermined"
    assert resp["undetermined_reason"] == "no_extraction"
    assert resp["coverage_rate"] is None
    assert resp["summary"]["coverage_rate"] is None
    assert resp["items"] == []
    assert all(i.get("status") != "missing" for i in resp["items"])


async def test_case10_failed_extraction_undetermined(db_conn):
    """用例 10：抽取整体失败 → undetermined + failed_sources 进 unmatched。"""
    await _seed_scheme(db_conn)
    await svc.insert_run(
        db_conn,
        {
            "id": "run1",
            "project_id": "p1",
            "status": "failed",
            "failed_sources": [{"doc_id": "d1", "file_name": "评标办法.pdf", "reason": "AI 超时"}],
            "items_count": 0,
            "error": "AI 超时",
        },
    )
    await db_conn.commit()
    resp = await scoring_router.scoring_coverage(scheme_id="sc1", thresholds=None, db=db_conn)
    assert resp["status"] == "undetermined"
    assert resp["undetermined_reason"] == "extraction_failed"
    assert resp["coverage_rate"] is None
    assert len(resp["unmatched"]) == 1
    assert resp["unmatched"][0]["file_name"] == "评标办法.pdf"


async def test_case11_empty_items_undetermined(db_conn):
    """用例 11：抽取成功但 0 条有效评分项 → undetermined_reason=empty_items。"""
    await _seed_scheme(db_conn)
    await svc.insert_run(db_conn, {"id": "run1", "project_id": "p1", "status": "success", "items_count": 0})
    await db_conn.commit()
    resp = await scoring_router.scoring_coverage(scheme_id="sc1", thresholds=None, db=db_conn)
    assert resp["status"] == "undetermined"
    assert resp["undetermined_reason"] == "empty_items"
    assert resp["coverage_rate"] is None


async def test_case12_partial_extraction_keeps_results(db_conn):
    """用例 12：部分来源失败 → status=partial，成功来源正常 mapped/missing。"""
    await _seed_scheme(db_conn)
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, level) VALUES ('s1', 'sc1', '文明施工、安全管理', 1)"
    )
    await db_conn.commit()
    items = [_item("i1", "安全管理、文明施工", score=10)]
    await svc.persist_items(db_conn, "p1", items, run_status="partial")
    await svc.insert_run(
        db_conn,
        {
            "id": "run1",
            "project_id": "p1",
            "status": "partial",
            "failed_sources": [{"doc_id": "d2", "file_name": "补遗.pdf", "reason": "解析失败"}],
            "items_count": 1,
        },
    )
    await db_conn.commit()
    resp = await scoring_router.scoring_coverage(scheme_id="sc1", thresholds=None, db=db_conn)
    assert resp["status"] == "partial"
    assert resp["summary"]["mapped"] == 1
    assert len(resp["unmatched"]) == 1


# ---------------------------------------------------------------------------
# 抽取链路（用例 13~16）
# ---------------------------------------------------------------------------
def test_case13_dedupe_same_item():
    """用例 13：同一评分项重复出现（同编号同名）→ 按 id 去重。"""
    raw = [
        {"item_no": "2.1", "item_name": "施工组织设计针对性", "score_raw": "10分", "_source_doc_id": "d1"},
        {"item_no": "2.1", "item_name": "施工组织设计针对性", "score_raw": "8分", "_source_doc_id": "d1"},
    ]
    out = svc.normalize_and_validate("p1", raw)
    assert len(out) == 1
    assert out[0]["score"] == 10  # 保留分值更高者


async def test_case14_upsert_idempotent_and_stale(db_conn):
    """用例 14：重复抽取幂等（行数不增）；success run 把未覆盖旧行标 stale。"""
    await db_conn.execute("INSERT INTO projects (id, name) VALUES ('p1', 'P')")
    a = [_item("iA", "安全管理", score=10)]
    await svc.persist_items(db_conn, "p1", a, run_status="success")
    await db_conn.commit()

    b = [_item("iB", "质量管理", score=10)]
    await svc.persist_items(db_conn, "p1", b, run_status="success")
    await db_conn.commit()

    rows = await svc.load_active_items(db_conn, "p1")
    assert [r["id"] for r in rows] == ["iB"]  # A 已被 success run 标 stale

    # partial run 不触发 stale 标记
    await svc.persist_items(db_conn, "p1", a, run_status="partial")
    await db_conn.commit()
    rows = await svc.load_active_items(db_conn, "p1")
    assert {r["id"] for r in rows} == {"iA", "iB"}


def test_case15_page_evidence_optional():
    """用例 15：有 doc_chunks 页码时填充，匹配不上留空（不编造）。"""
    pages = [
        {"page_num": 11, "text": "无关内容", "source_ref": "d1#page:11"},
        {"page_num": 12, "text": "施工组织设计的针对性满足现场要求", "source_ref": "d1#page:12"},
    ]
    ev = svc.attach_evidence("施工组织设计的针对性", "针对性满足现场要求", pages, "d1")
    assert ev["page_num"] == 12
    assert ev["page_ref"] == "d1#page:12"

    miss = svc.attach_evidence("不存在的评分项名称", "无此原文", pages, "d1")
    assert miss["page_num"] is None
    assert miss["page_ref"] == ""


async def test_case16_v1_doc_ids_type_only(db_conn, monkeypatch):
    """用例 16：v1 传非空 doc_ids 仍全扫候选块（仅类型校验，不改变范围）。"""
    await _seed_scheme(db_conn)
    called = {}

    async def fake_sources(db, project_id):
        called["args"] = (project_id,)
        return []

    monkeypatch.setattr(svc, "select_scoring_sources", fake_sources)

    # 非空合法 doc_ids：不报错，且扫描范围不受影响（fake 直接证明只按 project 全扫）
    resp = await scoring_router.extract_scoring_items("p1", body={"doc_ids": ["d1"], "force": False}, db=db_conn)
    assert resp["status"] == "failed"  # 无候选块 → failed run
    assert called["args"] == ("p1",)

    # 非字符串数组 → 400
    with pytest.raises(HTTPException) as ei:
        await scoring_router.extract_scoring_items("p1", body={"doc_ids": [1, 2]}, db=db_conn)
    assert ei.value.status_code == 400

    # 结构性证据：v1 的扫描入口签名不接受 doc_ids（范围不可被请求改变）
    assert "doc_ids" not in inspect.signature(svc.select_scoring_sources).parameters


def test_case17_missing_action_suggestion_only():
    """用例 17：缺失项只产「新增建议章节」建议（纯函数，不写库/不生成正文）。"""
    sections = _flat(
        ("s1", "", "施工组织设计"),
        ("s1a", "s1", "施工部署"),
        ("s1b", "s1", "安全、文明施工"),
    )
    sug = svc._suggest_section(_item("i1", "文明施工、安全管理"), sections)
    assert sug["title"] == "文明施工、安全管理"
    assert sug["parent_id"] == "s1b"  # 关键词最相近的二级章节（s1a 无 token 重叠）
    # 无任何二级章节可挂时 → 挂到最后一个一级章节
    sections2 = _flat(("s1", "", "施工组织设计"), ("s2", "", "绿色施工管理"))
    sug2 = svc._suggest_section(_item("i2", "深基坑降水专项"), sections2)
    assert sug2["parent_id"] == "s2"


# ---------------------------------------------------------------------------
# 路由基础行为（补充：404 / 空列表接口）
# ---------------------------------------------------------------------------
async def test_router_project_not_found(db_conn):
    with pytest.raises(HTTPException) as ei:
        await scoring_router.list_scoring_items("nope", db=db_conn)
    assert ei.value.status_code == 404


async def test_router_scheme_not_found(db_conn):
    with pytest.raises(HTTPException) as ei:
        await scoring_router.scoring_coverage(scheme_id="nope", thresholds=None, db=db_conn)
    assert ei.value.status_code == 404
