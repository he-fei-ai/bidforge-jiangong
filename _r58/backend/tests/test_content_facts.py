# -*- coding: utf-8 -*-
"""正文生成的「项目关键事实」注入链路单测（逐章精选 + 编号工具）。

覆盖修复项：
1. **致命 BUG 回归**：`generate_content` 曾在生成循环外用 `relevant_to=leaf`
   构建 facts_text，而 `leaf` 只在「传了 word_budget_override」的分支里被绑定
   —— 用户使用默认字数（前端默认档，不传该参数）时 `leaf` 未定义 → NameError
   → 整批章节一章都生成不了。现改为「事实行加载一次 + 逐章精选」，
   本文件用源码级断言防止该写法回归。
2. 逐章精选（`_render_facts_text(relevant_to=leaf)`）真正按章生效：
   不同章节得到不同的事实文本，而非所有章节共享同一份。
3. 相关性预筛命中为空时必须回退全量（绝不因预筛丢事实）。
4. 一级章节号 > 20 时仍输出中文编号（`_cn_number`）。
"""
import inspect
import time

from app.routers.sse_handlers import (
    _cn_number,
    _facts_keywords,
    _filter_facts_rows,
    _load_facts_rows,
    _render_facts_text,
)

# ---------- 工具 ----------

class FakeDb:
    """最小异步 db mock：返回预设的事实行。"""

    def __init__(self, rows=None, raise_on_execute=False):
        self._rows = list(rows or [])
        self.raise_on_execute = raise_on_execute

    async def execute(self, sql, params=None):
        if self.raise_on_execute:
            raise RuntimeError("no such table: global_facts")

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            async def fetchall(self):
                return self._rows

            async def fetchone(self):
                return self._rows[0] if self._rows else None

        return _Cursor(self._rows)


def _rows():
    """(group_title, title, content) 事实行，覆盖通用组与两个专业组。"""
    return [
        ("项目概况", "工程名称", "某某产业园项目"),
        ("基坑支护", "开挖深度", "基坑开挖深度 8.6m，采用灌注桩支护"),
        ("模板工程", "搭设高度", "高支模搭设高度 12.4m，立杆间距 0.9m"),
    ]


def _rows_without_generic():
    """无通用组的事实行：用于验证「命中为空 → 回退全量」。"""
    return [r for r in _rows() if r[0] != "项目概况"]


# ---------- _facts_keywords ----------

def test_facts_keywords_chinese_and_ascii():
    kws = _facts_keywords("深基坑支护 HDPE 施工")
    assert "深基" in kws and "基坑" in kws          # 中文 2-gram
    assert "hdpe" in kws                            # ASCII 单词转小写
    assert "施" not in kws                          # 单字不入词


def test_facts_keywords_empty():
    assert _facts_keywords("") == set()
    assert _facts_keywords(None) == set()


# ---------- _filter_facts_rows ----------

def test_filter_keeps_generic_group_and_matches():
    leaf = {"title": "基坑开挖", "description": "开挖深度与分层"}
    out = _filter_facts_rows(_rows(), leaf)
    titles = [r[1] for r in out]
    assert "工程名称" in titles      # 通用组（项目概况）始终保留
    assert "开挖深度" in titles      # 命中关键词
    assert "搭设高度" not in titles  # 未命中且非通用组 → 剔除


def test_filter_fallback_to_all_when_no_hit():
    """命中为空必须回退全部，绝不因预筛丢事实。"""
    rows = _rows_without_generic()
    out = _filter_facts_rows(rows, {"title": "安全生产责任制", "description": ""})
    assert out == rows


def test_filter_no_keywords_returns_all():
    out = _filter_facts_rows(_rows(), {"title": "", "description": ""})
    assert out == _rows()


# ---------- _render_facts_text ----------

def test_render_groups_by_group_title():
    text = _render_facts_text(_rows())
    assert "### 项目概况" in text
    assert "### 基坑支护" in text
    assert "### 模板工程" in text


def test_render_per_leaf_selection_differs():
    """✅ 逐章精选生效：不同章节得到不同的事实文本。"""
    rows = _rows()
    a = _render_facts_text(rows, relevant_to={"title": "开挖深度", "description": ""})
    b = _render_facts_text(rows, relevant_to={"title": "搭设高度", "description": ""})
    assert "开挖深度" in a and "搭设高度" not in a
    assert "搭设高度" in b and "开挖深度" not in b
    assert a != b


def test_render_truncates_per_fact_and_total():
    rows = [("组", "标题", "甲" * 500)]
    text = _render_facts_text(rows, per_fact=100)
    assert "甲" * 100 in text and "甲" * 101 not in text
    assert len(text) <= len("### 组\n") + 101


def test_render_never_exceeds_budget_and_covers_all_facts():
    """预算硬上限 + 「不丢尾部事实」。

    ✅ 2026-09-27 契约变更：旧实现是**头部优先的 break**
    （`total+len(fact) > max_total` 即跳出），第 3 条之后整段消失 ——
    危大参数/验收标准这类**排在后面**的事实在提示词里完全不可见，
    AI 判定「事实缺失」→ 写出【待补充】（假性占位符）。
    现改为按比例分配预算：超预算时**每条都拿到保底额度**，宁可都短一点，
    也不丢任何一条；未超预算时与旧实现逐字一致。
    外层契约「len(text) <= max_total」不变（提示词长度是硬上限）。
    """
    rows = [(f"组{i}", "t", "乙" * 100) for i in range(10)]
    text = _render_facts_text(rows, max_total=250, per_fact=100)
    # 10 组全部出现（旧实现只有2组）；
    # 每组内容被等比例缩短，但**一条不丢**。
    assert text.count("### ") == 10, "尾部事实被整段丢弃"
    assert len(text) <= 250, "不得突破提示词长度硬上限"      # 第 3 条会超出 250 → 提前停止


def test_render_empty_rows():
    assert _render_facts_text([]) == ""


# ---------- _load_facts_rows ----------

async def test_load_facts_rows_returns_rows():
    rows = [(("项目概况"), "工程名称", "X")]
    db = FakeDb(rows)
    assert await _load_facts_rows(db, "scheme-1") == rows


async def test_load_facts_rows_includes_project_scope():
    class ScopeDb(FakeDb):
        async def execute(self, sql, params=None):
            if "SELECT project_id FROM schemes" in sql:
                class Cursor:
                    async def fetchone(self):
                        return ("project-1",)
                return Cursor()
            assert "scheme_id=? OR (project_id=?" in sql
            assert params == ("scheme-1", "project-1")
            return await super().execute(sql, params)

    rows = [("项目级", "工程名称", "X")]
    assert await _load_facts_rows(ScopeDb(rows), "scheme-1") == rows


async def test_load_facts_rows_failure_degrades_to_empty():
    """全局事实表缺失/查询异常时降级为空，绝不阻断生成。"""
    db = FakeDb(raise_on_execute=True)
    assert await _load_facts_rows(db, "scheme-1") == []


# ---------- 一级章节中文编号 ----------

def test_cn_number_within_static_table():
    assert _cn_number(1) == "一"
    assert _cn_number(10) == "十"
    assert _cn_number(20) == "二十"


def test_cn_number_beyond_twenty():
    """✅ 增强：> 20 章仍输出中文编号（旧实现会退化为阿拉伯数字）。"""
    assert _cn_number(21) == "二十一"
    assert _cn_number(30) == "三十"
    assert _cn_number(35) == "三十五"
    assert _cn_number(99) == "九十九"
    assert _cn_number(100) == "100"     # 100+ 仍回退阿拉伯数字


def test_cn_number_invalid():
    assert _cn_number(0) == "0"
    assert _cn_number(-3) == "-3"


# ---------- 致命 BUG 源码级回归 ----------

def test_generate_content_has_no_unbound_leaf_reference():
    """回归护栏：正文生成不得再用「整批共享」的事实构建方式。

    旧代码在 `for leaf in leaves`（仅 word_budget_override 分支执行）之外的函数体里
    直接以 `leaf` 作为 relevant_to 调用事实构建 → 默认字数（不传 word_budget_override）
    时 `leaf` 未定义 → NameError → 整批正文一章都生成不了。
    现改为：事实行加载一次（`_load_facts_rows`），逐章在 `_build_generation_context`
    （leaf 是形参，必然有值）内用 `_render_facts_text(..., relevant_to=leaf)` 精选。
    """
    from app.routers import sse_handlers

    src = inspect.getsource(sse_handlers.generate_content)
    # 不得再出现把 relevant_to 交给 _build_facts_text 的写法（会退化为整批共享）
    assert "_build_facts_text(db, scheme_id, relevant_to=" not in src
    # 逐章精选必须在已有 leaf 形参的上下文构建函数内完成。
    # ✅ 2026-09-24：该调用因章节优先特性（facts_chapter_inject）改为多行写法并
    #    追加 chapter= 关键字，整行匹配会误报；改为两个独立锚点，护栏意图不变
    #    （_render_facts_text 必须以逐章 leaf 为 relevant_to，而非整批共享）。
    assert "_render_facts_text(" in src
    assert "relevant_to=leaf" in src
    # 事实行只加载一次（避免逐章查库）
    assert "facts_rows = await _load_facts_rows(db, scheme_id)" in src


def test_render_facts_text_is_fast_enough_for_per_leaf_use():
    """逐章精选是内存内过滤（无 DB），N 章不应退化为 N 次查询。"""
    rows = [(f"组{i}", f"标题{i}", "丙" * 200) for i in range(200)]
    t0 = time.perf_counter()
    for i in range(50):
        _render_facts_text(rows, relevant_to={"title": f"主题{i}", "description": ""})
    assert time.perf_counter() - t0 < 1.0
