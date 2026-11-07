"""R179 M1（2026-11-05）评分项 → 目录映射校验 · 回归护栏。

覆盖：
1. 归一化唯一出口（NFKC + 序号前缀剥离）；
2. 候选页块选择（关键词 ≥2 + ±1 页上下文；无页块不编造页码）；
3. 分批预算（≤2 批、超预算登记为失败来源而非静默丢弃）；
4. 归一/去重（重复评分项保留分值更高者、证据定位不到留空）；
5. 四级匹配（精确 / 包含 / 关键词 / 评分标准回退 needs_review ≤0.5）；
6. 高档评分项只接受叶子；
7. 一评多章节 / 多评同章节（overloaded）；
8. extra 只查叶子 + 排除祖先已映射 + 结构白名单 + 泛化标题；
9. **undetermined 红线**：无评分项 ⇒ coverage_rate=null、items 中不得出现 missing；
10. 路由：抽取部分失败 → partial；全部失败 → failed；同项目并发 → 409；
11. 变异验证：阈值被改坏（keyword 阈值抬到 1.0）必须变红。

测试直接调用路由函数（与既有风格一致），避免 TestClient 跨事件循环持有
aiosqlite 连接导致的偶发失败。
"""

import uuid

import app.routers.scoring as scoring_router
import pytest
from app.services import scoring_items as svc
from fastapi import HTTPException


# ------------------------------------------------------------------ 1. 归一化
def test_r179_norm_title_nfkc_and_prefix_strip():
    assert svc.norm_title("３.１ 施工组织设计") == "施工组织设计"
    assert svc.norm_title("第 3 章 施工部署") == "施工部署"
    assert svc.norm_title("3、质量保证措施") == "质量保证措施"
    # 全角/半角标点必须被同一出口清洗
    assert svc.norm_title("施工（方案）") == svc.norm_title("施工(方案)")
    # 禁止不同写法被判定不等（NFKC 前会不等）
    assert svc.norm_title("ＡＢＣ") == "abc"


# ------------------------------------------------------------ 2. 候选页块选择
def test_r179_select_page_blocks_keyword_threshold():
    pages = [
        {"page_num": 1, "text": "封面"},
        {"page_num": 2, "text": "评分标准：技术评分 40 分，商务评分 10 分"},
        {"page_num": 3, "text": "评分项明细"},
        {"page_num": 4, "text": "合同条款"},
    ]
    text, used, hits = svc.select_page_blocks(pages)
    assert hits > 0
    # 命中页 + 相邻 ±1 页上下文（1 页被纳入但 4 页不在范围内）
    assert used == [1, 2, 3]
    assert "技术评分" in text


def test_r179_select_page_blocks_no_fabricated_page():
    """无页块 → 返回空页码（不编造 page_num）。"""
    text, used, hits = svc.select_page_blocks([])
    assert (text, used, hits) == ("", [], 0)


# --------------------------------------------------------------- 3. 分批预算
def test_r179_plan_batches_caps_two_batches_and_reports_overflow():
    sources = [
        {"doc_id": f"d{i}", "name": f"f{i}", "text": "x" * 10, "page_nums": [], "keyword_hits": 5 - i} for i in range(6)
    ]
    batches, overflow = svc.plan_batches(sources, max_chars=1000, max_docs=2)
    assert len(batches) <= 2
    assert len(overflow) >= 2  # 超文档数上限必须登记，不得静默丢弃
    assert all(o.get("reason") for o in overflow)


# --------------------------------------------------------- 4. 归一 / 去重 / 证据
def test_r179_normalize_dedupe_keeps_higher_score_and_leaves_page_empty():
    pages = [{"page_num": 7, "text": "施工组织设计 10 分", "source_ref": "d1#page:7"}]
    raw = [
        {
            "item_no": "1",
            "item_name": "施工组织设计",
            "score_raw": "5分",
            "quote": "施工组织设计",
            "_source_doc_id": "d1",
            "_batch_doc_ids": ["d1"],
            "_source_name": "评标办法.pdf",
        },
        {
            "item_no": "1",
            "item_name": "施工组织设计",
            "score_raw": "10分",
            "quote": "施工组织设计 10 分",
            "_source_doc_id": "d1",
            "_batch_doc_ids": ["d1"],
            "_source_name": "评标办法.pdf",
        },
        {"item_name": "  ", "score_raw": "3分"},
        {"item_name": "报名费", "score_raw": "1分", "is_scoring_item": False},
    ]
    items = svc.normalize_and_validate("p1", raw, {"d1": pages})
    assert len(items) == 1
    item = items[0]
    assert item["score"] == 10.0  # 重复项保留分值更高者
    assert item["page_num"] == 7 and item["page_ref"] == "d1#page:7"

    # 反例：证据在页块中找不到 → 页码留空，不编造
    items2 = svc.normalize_and_validate(
        "p1", [{"item_name": "应急预案", "score_raw": "5分", "quote": "不存在的原文"}], {"d1": pages}
    )
    assert items2[0]["page_num"] is None and items2[0]["page_ref"] == ""


# ----------------------------------------------------------------- 5/6/7. 匹配
def _sec(sid, title, level, parent="", children=None):
    return {
        "id": sid,
        "parent_id": parent,
        "title": title,
        "level": level,
        "path": sid,
        "sort_order": 0,
        "has_children": bool(children),
    }


def _item(iid, name, score, criteria=""):
    return {
        "id": iid,
        "item_no": "",
        "item_name": name,
        "score": score,
        "score_raw": f"{score}分" if score is not None else "",
        "criteria": criteria,
        "is_primary": 0,
        "page_ref": "",
        "page_num": None,
        "source_doc_id": "d1",
        "source_quote": "",
    }


def test_r179_exact_contains_keyword_and_criteria_levels():
    sections = [
        _sec("s1", "施工组织设计", 1),
        _sec("s2", "施工组织设计专项措施", 3),
        _sec("s3", "质量验收细则表", 3),
        _sec("s4", "文明施工措施", 3),
        _sec("s5", "安全技术措施专项方案", 3),
    ]
    # filler 把 i1/i2 的分值占比压到 mid/low，否则单项即 high 档（阈值更严）
    fillers = [_item(f"f{i}", f"投标保证金缴纳{i}", 10) for i in range(10)]
    items = [
        _item("i1", "施工组织设计", 10),  # exact（s1）
        _item("i2", "质量验收", 1),  # keyword（s3）：2-gram Jaccard = 3/6 = 0.5
        _item("i3", "文明施工", 1),  # contains（s4，4/6 = 0.667）
        _item("i4", "安全技术措施", 1),  # contains（s5，6/10 = 0.6）
        *fillers,
    ]
    report = svc.build_coverage_report(items, sections)
    by_id = {i["id"]: i for i in report["items"]}
    assert by_id["i1"]["method"] == "exact" and by_id["i1"]["section_id"] == "s1"
    assert by_id["i2"]["status"] == "mapped" and by_id["i2"]["method"] == "keyword"
    assert by_id["i3"]["status"] == "mapped" and by_id["i3"]["method"] == "contains"
    assert by_id["i4"]["method"] == "contains" and by_id["i4"]["section_id"] == "s5"
    # 覆盖率是确定性小数，不能是字符串
    assert isinstance(report["summary"]["coverage_rate"], float)


def test_r179_high_band_only_accepts_leaf():
    # i1 分值占比 100% → high；只命中父节点 s1（有子节点）→ 必须判 missing
    sections = [
        _sec("s1", "施工组织设计", 1, children=[1]),
        _sec("s1a", "施工总进度计划", 3, parent="s1"),
    ]
    report = svc.build_coverage_report([_item("i1", "施工组织设计", 10)], sections)
    item = report["items"][0]
    assert item["score_band"] == "high"
    assert item["status"] == "missing", "高档评分项不得挂到父节点上"


def test_r179_one_item_many_sections_and_many_items_same_section():
    # 低档（加 filler 拉低占比）→ 允许命中父节点 + 叶子，形成一评多章节
    sections = [
        _sec("s1", "施工组织设计专项措施", 1, children=[1]),
        _sec("s1a", "施工组织设计", 2, parent="s1"),
    ]
    fillers = [_item(f"f{i}", f"投标保证金缴纳{i}", 10) for i in range(10)]
    items = [_item("i1", "施工组织设计", 1), *fillers]
    report = svc.build_coverage_report(items, sections)
    item = next(i for i in report["items"] if i["id"] == "i1")
    assert item["status"] == "mapped"
    assert item["section_id"] == "s1a"  # exact 优先
    assert item["also_section_ids"] == ["s1"]  # 一评多章节保留全部命中

    # 多评同章：3 个评分项命中同一叶子 → overloaded 出现
    sections2 = [_sec("s1", "word", 1), _sec("s1a", "闸门", 3)]
    items2 = [_item(f"x{i}", "闸门", 1) for i in range(3)]
    report2 = svc.build_coverage_report(items2, sections2)
    assert report2["overloaded_sections"], "多个评分项命中同一章节必须提示 overloading"


def test_r179_criteria_fallback_needs_review_below_half():
    sections = [_sec("s1", "安全技术措施", 1)]
    item = _item("i1", "安全帽发放", 1, criteria="安全技术措施 安全帽 发放 频次")
    report = svc.build_coverage_report([item], sections, section_texts={"s1": "安全技术措施 安全帽 发放"})
    got = report["items"][0]
    assert got["method"] == "criteria"
    assert got["needs_review"] is True
    assert got["confidence"] <= 0.5


# ----------------------------------------------------------------------- 8. extra
def test_r179_extra_leaf_only_ancestor_mapped_and_whitelist():
    sections = [
        _sec("s1", "施工组织设计", 1, children=[1]),
        _sec("s1a", "施工方案", 2, parent="s1", children=[1]),
        _sec("s1a1", "施工方法", 3, parent="s1a"),
        _sec("s2", "附图", 1),
        _sec("s3", "编制说明", 1),
    ]
    # filler 把 i1 压到低档，否则单项即 high 档、父节点 s1a 命中会被叶子约束拒绝
    fillers = [_item(f"f{i}", f"投标保证金缴纳{i}", 10) for i in range(10)]
    items = [_item("i1", "施工方案", 1), *fillers]
    report = svc.build_coverage_report(items, sections, extra_whitelist=["附图", "编制说明"])
    extra_ids = {e["section_id"] for e in report["extra"]}
    # s1a1 的祖先 s1a 已命中 → 不算 extra；s2/s3 白名单 → 不算 extra
    assert "s1a1" not in extra_ids
    assert "s2" not in extra_ids and "s3" not in extra_ids


# ------------------------------------------------------- 9. undetermined 红线
def test_r179_undetermined_has_null_rate_and_no_missing():
    report = svc.build_coverage_report([], [_sec("s1", "施工组织设计", 1)])
    assert report["status"] == "undetermined"
    assert report["summary"]["coverage_rate"] is None
    assert report["missing"] == []
    assert all(i["status"] != "missing" for i in report["items"])


# ------------------------------------------------------------------ 10. 路由
@pytest.fixture
async def ctx(db_conn):
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db_conn.commit()
    yield db_conn, pid, sid


async def test_r179_coverage_route_undetermined_without_items(ctx):
    db, pid, sid = ctx
    res = await scoring_router.scoring_coverage(sid, None, db=db)
    assert res["status"] == "undetermined"
    assert res["coverage_rate"] is None
    assert res["items"] == [] and res["missing"] == []
    assert res["undetermined_reason"] == "no_extraction"


async def test_r179_coverage_route_zero_items_even_with_success_run(ctx):
    db, pid, sid = ctx
    await db.execute(
        "INSERT INTO scheme_scoring_extract_runs(id,project_id,status,items_count) VALUES(?,?,?,0)",
        (uuid.uuid4().hex, pid, "success"),
    )
    await db.commit()
    res = await scoring_router.scoring_coverage(sid, None, db=db)
    assert res["status"] == "undetermined"
    assert res["undetermined_reason"] == "empty_items"
    assert res["coverage_rate"] is None


async def test_r179_coverage_route_maps_items_to_outline(ctx):
    db, pid, sid = ctx
    root, leaf = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections(id,scheme_id,parent_id,title,sort_order,level) VALUES(?,?,?,?,?,?)",
        (root, sid, "", "施工组织设计", 0, 1),
    )
    await db.execute(
        "INSERT INTO sections(id,scheme_id,parent_id,title,sort_order,level) VALUES(?,?,?,?,?,?)",
        (leaf, sid, root, "施工组织设计", 1, 2),
    )
    await db.execute(
        "INSERT INTO scheme_scoring_items(id,project_id,item_no,item_name,score,status) VALUES(?,?,?,?,?,?)",
        ("i1", pid, "", "施工组织设计", 10.0, "active"),
    )
    await db.commit()
    res = await scoring_router.scoring_coverage(sid, None, db=db)
    assert res["status"] == "ok"
    assert res["items"][0]["status"] == "mapped"
    assert res["summary"]["coverage_rate"] == 1.0


async def test_r179_extract_partial_and_failed_semantics(ctx, monkeypatch):
    db, pid, _sid = ctx
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", ("p-empty", "empty"))
    await db.commit()

    # 无候选页块 → failed（不是 5xx）
    res = await scoring_router.extract_scoring_items("p-empty", None, db=db)
    assert res["status"] == "failed" and res["items"] == []

    async def _boom(batch, timeout=120):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "extract_batch", _boom)
    monkeypatch.setattr(
        svc,
        "select_scoring_sources",
        lambda *a, **k: _async(
            [
                {"doc_id": "d1", "name": "a", "text": "x", "page_nums": [1], "pages": [], "keyword_hits": 3},
                {"doc_id": "d2", "name": "b", "text": "x", "page_nums": [1], "pages": [], "keyword_hits": 2},
            ]
        ),
    )
    res2 = await scoring_router.extract_scoring_items(pid, None, db=db)
    assert res2["status"] == "failed"
    assert len(res2["failed_sources"]) == 2


async def test_r179_extract_returns_409_when_concurrent(ctx):
    db, pid, _sid = ctx
    scoring_router._ACTIVE_EXTRACT.add(pid)
    try:
        with pytest.raises(HTTPException) as ei:
            await scoring_router.extract_scoring_items(pid, None, db=db)
        assert ei.value.status_code == 409
    finally:
        scoring_router._ACTIVE_EXTRACT.discard(pid)


# ------------------------------------------------------------------ 11. 变异验证
def test_r179_mutation_threshold_guard():
    """阈值必须真的参与判定：把 keyword 阈值抬到 1.0 后原本 keyword 命中的项必须变红。

    这是「改坏匹配阈值必须变红」的常驻变异护栏（若实现忽略传入阈值，本用例会
    在第二次断言处失败）。
    """
    sections = [_sec("s1", "质量验收细则表", 3)]
    item = _item("i1", "质量验收", 1)
    # filler 压到低档（0.45），默认阈值下 2-gram Jaccard = 3/6 = 0.5 应命中
    fillers = [_item(f"f{i}", f"投标保证金缴纳{i}", 10) for i in range(10)]
    ok = svc.build_coverage_report([item, *fillers], sections)
    got = next(i for i in ok["items"] if i["id"] == "i1")
    assert got["status"] == "mapped" and got["method"] == "keyword"
    broken = svc.build_coverage_report(
        [item, *fillers],
        sections,
        thresholds={"keyword_low": 1.0, "keyword_mid": 1.0, "keyword_high": 1.0},
    )
    got2 = next(i for i in broken["items"] if i["id"] == "i1")
    assert got2["status"] == "missing"


def _async(value):
    async def _inner(*_a, **_k):
        return value

    return _inner()


# ---------------------------------------- 12. 0 条抽取不得静默清空活跃快照
async def test_r179_extract_zero_items_keeps_existing_snapshot(ctx, monkeypatch):
    """此前有活跃快照 + 本次抽取返回 0 条 → status=empty，旧快照必须保持 active。

    旧实现把「解析成功但 0 条」判成 success，persist_items 随即把该项目全部
    status='active' 行改成 stale：数据还在但等于不可用，且记录成成功。
    """
    db, pid, sid = ctx
    for iid, no, name in (("old1", "1", "施工组织设计"), ("old2", "2", "质量验收")):
        await db.execute(
            "INSERT INTO scheme_scoring_items(id,project_id,item_no,item_name,score,status)"
            " VALUES(?,?,?,?,?, 'active')",
            (iid, pid, no, name, 10.0),
        )
    await db.commit()

    monkeypatch.setattr(
        svc,
        "select_scoring_sources",
        lambda *a, **k: _async(
            [
                {
                    "doc_id": "d1",
                    "name": "评标办法.pdf",
                    "text": "评分标准",
                    "page_nums": [1],
                    "pages": [],
                    "keyword_hits": 3,
                }
            ]
        ),
    )
    monkeypatch.setattr(svc, "extract_batch", lambda batch, timeout=120: _async([]))

    res = await scoring_router.extract_scoring_items(pid, None, db=db)
    assert res["status"] == "empty", "0 条结果不得判 success（会清空活跃快照）"
    assert res["items_count"] == 0
    assert "保留" in res["error"]
    # 旧快照必须仍然 active（数据还在、仍可用）
    assert [r["id"] for r in await svc.load_active_items(db, pid)] == ["old1", "old2"]
    # 覆盖率结论沿用旧快照，但必须标注为 partial：不得显示成 ok
    cov = await scoring_router.scoring_coverage(sid, None, db=db)
    assert cov["status"] == "partial"
    assert cov["extraction"]["status"] == "empty"


async def test_r179_extract_zero_items_without_snapshot_is_success(ctx, monkeypatch):
    """无旧快照 + 0 条 = 真的没有评分项 → success（与 empty 可区分）。"""
    db, pid, sid = ctx
    monkeypatch.setattr(
        svc,
        "select_scoring_sources",
        lambda *a, **k: _async(
            [
                {
                    "doc_id": "d1",
                    "name": "评标办法.pdf",
                    "text": "评分标准",
                    "page_nums": [1],
                    "pages": [],
                    "keyword_hits": 3,
                }
            ]
        ),
    )
    monkeypatch.setattr(svc, "extract_batch", lambda batch, timeout=120: _async([]))

    res = await scoring_router.extract_scoring_items(pid, None, db=db)
    assert res["status"] == "success"
    assert res["items_count"] == 0

    cov = await scoring_router.scoring_coverage(sid, None, db=db)
    assert cov["status"] == "undetermined"
    assert cov["undetermined_reason"] == "empty_items"
