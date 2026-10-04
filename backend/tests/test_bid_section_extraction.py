"""AI 多标段识别（bid_section_extraction）回归测试（2026-09-22）。

引入背景（对齐 OpenBidKit `services/bidSectionExtractionTask.cjs`）：
规则检测器只回答「是否疑似多标段」，无法给出可选择的标段清单。本模块用
「带行号文本分段 → 结构化提取 → 候选合并 → 去重归一 → 必须 ≥2 个有效标段」
产出含标题/描述/依据/原文行号区间的标段清单，供用户精确选择投标范围。

本文件覆盖：
1. 行号前缀与标题归一化；
2. 行号区间校验（越界/倒序/脏值）；
3. 去重合并（同标段跨段合并、无区间候选剔除、排序与重编号）；
4. 响应归一化（兼容 camelCase 字段）+ ≥2 校验；
5. 编排：单段 / 多段（触发合并调用）/ 去重 / 有效标段不足 / 空文本；
6. 路由端点：缺文档 400、识别不足 422、成功落库并回传提示。
"""
import json
import uuid

import app.routers.bid_analysis as ba
import app.services.bid_section_extraction as bse
import pytest

# ---------------------------------------------------------------------------
# 1. 行号与标题归一化
# ---------------------------------------------------------------------------

def test_number_markdown_lines_format():
    out = bse.number_markdown_lines("第一行\n第二行")
    assert out == "L000001 | 第一行\nL000002 | 第二行"


def test_normalize_section_title_strips_and_collapses():
    assert bse.normalize_section_title(" 一 标段 ") == "一标段"
    assert bse.normalize_section_title("第一标段") == "一标段"
    assert bse.normalize_section_title("第2标包") == "2标包"
    assert bse.normalize_section_title("二标段：设备采购") == "二标段：设备采购"


def test_get_section_merge_key_falls_back_to_index():
    assert bse.get_section_merge_key({"title": "一标段", "unit": "标段"}) == "标段:一标段"
    assert bse.get_section_merge_key({"title": "", "index": 3, "unit": "标包"}) == "标包:3"


# ---------------------------------------------------------------------------
# 2. 行号区间校验
# ---------------------------------------------------------------------------

def test_normalize_line_range_valid_and_camel_case():
    assert bse.normalize_line_range({"startLine": 1, "endLine": 5}, 10) == {
        "start_line": 1, "end_line": 5}
    assert bse.normalize_line_range(
        {"start_line": "2", "end_line": "3", "reason": "采购清单"}, 10) == {
        "start_line": 2, "end_line": 3, "reason": "采购清单"}


def test_normalize_line_range_rejects_invalid():
    assert bse.normalize_line_range({"start_line": 0, "end_line": 5}, 10) is None
    assert bse.normalize_line_range({"start_line": 5, "end_line": 3}, 10) is None
    assert bse.normalize_line_range({"start_line": 1, "end_line": 99}, 10) is None
    assert bse.normalize_line_range({"start_line": "x", "end_line": "y"}, 10) is None
    assert bse.normalize_line_range("not-a-dict", 10) is None


# ---------------------------------------------------------------------------
# 3. 去重合并
# ---------------------------------------------------------------------------

def test_dedupe_merges_same_section_across_segments():
    sections = [
        {"title": "一标段", "unit": "标段", "index": 1,
         "include_ranges": [{"start_line": 10, "end_line": 20}],
         "evidence": ["依据A"]},
        {"title": "第 一 标段", "unit": "标段", "index": 1,
         "include_ranges": [{"start_line": 30, "end_line": 40}],
         "evidence": ["依据A", "依据B"], "description": "设备采购"},
        {"title": "二标段", "unit": "标段", "index": 2,
         "include_ranges": [{"start_line": 5, "end_line": 9}]},
    ]
    out = bse.dedupe_sections(sections)
    assert len(out) == 2
    # 按首个行号排序：二标段(5) 在前
    assert out[0]["title"] == "二标段" and out[0]["id"] == "section-1"
    one = out[1]
    assert one["title"] == "一标段"
    assert [(r["start_line"], r["end_line"]) for r in one["include_ranges"]] == [
        (10, 20), (30, 40)]
    assert one["evidence"] == ["依据A", "依据B"]
    assert one["description"] == "设备采购"


def test_dedupe_drops_candidates_without_ranges():
    out = bse.dedupe_sections([
        {"title": "一标段", "include_ranges": []},
        {"title": "二标段", "include_ranges": [{"start_line": 3, "end_line": 4}]},
    ])
    assert [s["title"] for s in out] == ["二标段"]


# ---------------------------------------------------------------------------
# 4. 响应归一化与校验
# ---------------------------------------------------------------------------

def test_normalize_sections_response_accepts_camel_case_and_drops_untitled():
    out = bse.normalize_sections_response({"sections": [
        {"title": "一标段", "includeRanges": [{"startLine": 1, "endLine": 2}]},
        {"title": "", "include_ranges": [{"start_line": 3, "end_line": 4}]},
    ]}, total_lines=10)
    assert len(out["sections"]) == 1
    assert out["sections"][0]["include_ranges"] == [{"start_line": 1, "end_line": 2}]


def test_validate_sections_response_requires_two():
    bse.validate_sections_response({"sections": [{}, {}]})
    with pytest.raises(ValueError):
        bse.validate_sections_response({"sections": [{}]})
    with pytest.raises(ValueError):
        bse.validate_sections_response({})


# ---------------------------------------------------------------------------
# 5. 编排（注入假 AI 收集器）
# ---------------------------------------------------------------------------

def _section(title: str, start: int, end: int, **extra) -> dict:
    return {"title": title, "unit": "标段",
            "include_ranges": [{"start_line": start, "end_line": end}], **extra}


def _text_lines(n: int) -> str:
    """构造 n 行正文（行号区间校验依赖真实行数，太少会把候选全部判为越界）。"""
    return "\n".join(f"第{i}行：招标文件正文内容" for i in range(1, n + 1))


def _collector(segment_payload: dict, merge_payload: dict | None = None):
    """构造假收集器；按 user 消息是否含「分段结果：」区分合并调用。"""
    calls: list[list[dict]] = []

    async def _collect(messages, validate_fn=None, **kwargs):
        calls.append(messages)
        user = messages[-1]["content"]
        payload = merge_payload if ("分段结果：" in user and merge_payload) else segment_payload
        return payload, json.dumps(payload, ensure_ascii=False)

    return _collect, calls


@pytest.mark.asyncio
async def test_extract_single_segment_no_merge_call():
    collect, calls = _collector({"sections": [
        _section("一标段", 1, 5), _section("二标段", 6, 9)]})
    res = await bse.extract_bid_sections(
        ai_collect=collect, markdown=_text_lines(20), chunk_size=10 ** 6)

    assert res["segment_count"] == 1
    assert res["estimated_calls"] == 1, "单段不应触发合并调用"
    assert len(calls) == 1
    assert [s["title"] for s in res["sections"]] == ["一标段", "二标段"]


@pytest.mark.asyncio
async def test_extract_multi_segment_triggers_merge_and_dedupes():
    seg_payload = {"sections": [
        _section("一标段", 1, 5), _section("二标段", 8, 12)]}
    merge_payload = {"sections": [
        _section("一标段", 1, 5), _section("一标段", 20, 25),
        _section("二标段", 8, 12)]}
    collect, calls = _collector(seg_payload, merge_payload)

    res = await bse.extract_bid_sections(
        ai_collect=collect, markdown=_text_lines(30), chunk_size=120,
        chunk_overlap=0)

    assert res["segment_count"] > 1
    assert res["estimated_calls"] == res["segment_count"] + 1
    assert len(res["sections"]) == 2, "跨段重复标段必须合并"
    one = next(s for s in res["sections"] if s["title"] == "一标段")
    assert len(one["include_ranges"]) == 2, "合并必须保留两段的区间"
    # 合并调用是最后一次
    assert "分段结果：" in calls[-1][-1]["content"]


@pytest.mark.asyncio
async def test_extract_raises_when_fewer_than_two_sections():
    collect, _ = _collector({"sections": [_section("一标段", 1, 5)]})
    with pytest.raises(ValueError):
        await bse.extract_bid_sections(
            ai_collect=collect, markdown="内容", chunk_size=10 ** 6)


@pytest.mark.asyncio
async def test_extract_empty_markdown_raises():
    collect, _ = _collector({"sections": []})
    with pytest.raises(ValueError):
        await bse.extract_bid_sections(ai_collect=collect, markdown="   ")


@pytest.mark.asyncio
async def test_extract_raises_when_merge_result_empty():
    """合并结果为空 → ≥2 校验失败（宁可报错，也不返回不确定的标段清单）。"""
    seg_payload = {"sections": [_section("一标段", 1, 5), _section("二标段", 8, 12)]}
    collect, _ = _collector(seg_payload, merge_payload={"sections": []})
    with pytest.raises(ValueError):
        await bse.extract_bid_sections(
            ai_collect=collect, markdown="甲" * 200, chunk_size=80, chunk_overlap=0)


def test_split_for_analysis_terminates_when_chunk_size_smaller_than_overlap():
    """【BUG 回归】chunk_size <= overlap 时必须前进（旧实现会死循环）。

    旧实现 `start = max(end - overlap, 0)`：chunk_size=40、overlap=500 时
    start 每轮都被算回 0 → while 永不退出 → 请求挂死、任务永久 running。
    """
    from app.services.bid_analysis_service import split_for_analysis

    segments = split_for_analysis("甲" * 200, 40, 500)   # 不允许挂住
    assert len(segments) > 1
    assert all(segments)


# ---------------------------------------------------------------------------
# 6. 路由端点
# ---------------------------------------------------------------------------

async def _seed_scheme_with_doc(db, parsed_markdown: str = "招标文件正文") -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, parsed_markdown) VALUES (?,?,?,?,?)",
        (uuid.uuid4().hex, pid, "招标文件.docx", "docx", parsed_markdown))
    await db.commit()
    return pid, sid


@pytest.mark.asyncio
async def test_extract_sections_endpoint_requires_parsed_docs(db_conn):
    from fastapi import HTTPException

    pid, sid = await _seed_scheme_with_doc(db_conn, parsed_markdown="")
    with pytest.raises(HTTPException) as ei:
        await ba.extract_bid_sections_api(scheme_id=sid, project_id="", db=db_conn)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_extract_sections_endpoint_persists_and_returns(db_conn, monkeypatch):
    pid, sid = await _seed_scheme_with_doc(db_conn)
    sections = [
        {"id": "section-1", "index": 1, "unit": "标段", "title": "一标段",
         "head_line": "一标段：土建", "description": "土建施工",
         "include_ranges": [{"start_line": 1, "end_line": 5}],
         "evidence": ["一标段：土建"]},
        {"id": "section-2", "index": 2, "unit": "标段", "title": "二标段",
         "head_line": "二标段：安装", "description": "设备安装",
         "include_ranges": [{"start_line": 6, "end_line": 9}],
         "evidence": ["二标段：安装"]},
    ]

    async def fake_extract(*args, **kwargs):
        return {"sections": sections, "segment_count": 2, "estimated_calls": 3}

    monkeypatch.setattr(bse, "extract_bid_sections", fake_extract)

    res = await ba.extract_bid_sections_api(scheme_id=sid, project_id="", db=db_conn)
    assert res["ok"] is True and res["count"] == 2
    assert res["estimated_calls"] == 3
    assert res["context_hint"] == "", "尚未选择投标范围 → 不注入上下文"

    got = await ba.get_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert got["is_multi"] is True and got["total_declared"] == 2
    assert got["needs_selection"] is True
    assert [s["title"] for s in got["detected_sections"]] == ["一标段", "二标段"]

    # 选择后提示注入
    await ba.select_bid_section(
        {"scheme_id": sid, "section_id": "section-2", "section_title": "二标段"},
        db=db_conn)
    got2 = await ba.get_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert "当前选择标段：二标段" in got2["context_hint"]


@pytest.mark.asyncio
async def test_extract_sections_endpoint_maps_low_count_to_422(db_conn, monkeypatch):
    from fastapi import HTTPException

    pid, sid = await _seed_scheme_with_doc(db_conn)

    async def fake_extract(*args, **kwargs):
        raise ValueError("未识别到至少两个有效标段")

    monkeypatch.setattr(bse, "extract_bid_sections", fake_extract)
    with pytest.raises(HTTPException) as ei:
        await ba.extract_bid_sections_api(scheme_id=sid, project_id="", db=db_conn)
    assert ei.value.status_code == 422
    assert "标段" in ei.value.detail
