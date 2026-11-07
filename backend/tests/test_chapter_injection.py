"""#10 正文按九大章节字段映射注入：_build_chapter_extraction_map 解析验证。"""

import json
import logging

from app.routers.sse_handlers import _build_chapter_extraction_map
from app.services.scheme_classification import build_chapter_extraction_text


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, sql, params=()):
        return _Cur(self.rows)


def _sections():
    # L1「工程概况」(c1) 下挂一个叶子 l1；另有 L1「施工工艺技术」(c2) 下挂 l2
    return [
        {"id": "c1", "title": "1 工程概况", "level": 1, "parent_id": ""},
        {"id": "l1", "title": "1.1 工程概况展开", "level": 2, "parent_id": "c1"},
        {"id": "c2", "title": "4 施工工艺技术", "level": 1, "parent_id": ""},
        {"id": "l2", "title": "4.1 工艺流程", "level": 2, "parent_id": "c2"},
    ]


def _nodes_map(sections):
    return {s["id"]: s for s in sections}


def _by_parent(sections):
    bp = {}
    for s in sections:
        bp.setdefault(s.get("parent_id", ""), []).append(s)
    return bp


async def test_build_chapter_extraction_map():
    sections = _sections()
    rows = [
        # overview 的 source_items 含 projectBasicInfo
        {
            "item_id": "projectBasicInfo",
            "label": "项目基本信息",
            "output_type": "json",
            "content": "工程名称：某基坑工程",
        },
        # constructionTechnique 的 source_items 含 constructionTechnique
        {
            "item_id": "constructionTechnique",
            "label": "施工工艺技术",
            "output_type": "markdown",
            "content": "采用分层开挖工艺",
        },
    ]
    result = await _build_chapter_extraction_map(_Db(rows), "pid", sections, _nodes_map(sections), _by_parent(sections))

    # 叶子 l1 → 顶层 c1=工程概况 → 应命中 projectBasicInfo 内容
    assert "某基坑工程" in result["l1"]
    # 叶子 l2 → 顶层 c2=施工工艺技术 → 应命中 constructionTechnique 内容
    assert "分层开挖" in result["l2"]
    # 顶层章节自身（level=1）不注入
    assert "c1" not in result and "c2" not in result


async def test_build_chapter_extraction_map_empty():
    # 无提取结果 → 空映射（退回旧行为）
    result = await _build_chapter_extraction_map(
        _Db([]), "pid", _sections(), _nodes_map(_sections()), _by_parent(_sections())
    )
    assert result == {}


def test_build_chapter_extraction_text_truncation_is_visible(caplog):
    """G4 连带项：超限截断必须留日志（不再静默硬切）。

    ✅ R180 起本函数改走 ``clip_text_notice``：正文仍按 max_chars 收刀，
    「模型可见说明」是**额外附加**在正文之后，总长因此超过上限。断言必须
    区分「内容上限」与「提示开销」—— 只放宽总数等于把截断护栏也放宽；
    提示长度从被测函数的实际输出取，不把提示文案的长度再硬编码一遍。
    """
    content = "超长提取内容" * 400
    raw = f"### 项目基本信息\n{content}"
    with caplog.at_level(logging.WARNING, logger="prompt_material_guard"):
        text = build_chapter_extraction_text(
            [{"label": "项目基本信息", "content": content}], "overview", max_chars=2000
        )
    body, sep, notice = text.partition("\n\n[……")
    assert sep, "超限必须附加模型可见截断说明（不再是静默硬切）"
    assert len(body) == 2000, "正文部分仍按 max_chars 收刀，不因附加说明而缩水"
    assert body == raw[:2000], "截断必须保留原文前缀（不得改写正文）"
    assert notice.startswith("本章专项提取成果已截断：")
    assert f"原文 {len(raw)} 字" in notice
    assert f"此处保留前 {len(body)} 字" in notice
    assert f"已省略 {len(raw) - len(body)} 字" in notice
    assert len(text) == len(body) + len(sep) + len(notice), "说明是额外附加的，不是从正文里挤出来的"
    assert "上下文截断（本章专项提取成果）" in caplog.text


def test_build_chapter_extraction_text_within_limit_is_byte_identical():
    """未超限时 clip_text 原样返回，不得加省略号/换行。"""
    items = [{"label": "项目基本信息", "content": "工程名称：某基坑"}]
    expected = "### 项目基本信息\n工程名称：某基坑"
    assert build_chapter_extraction_text(items, "overview", max_chars=2000) == expected
