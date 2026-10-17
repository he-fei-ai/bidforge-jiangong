"""全局事实模块 · 前后端契约 parity 与组件级缺口收口护栏（R89 · 2026-10-17）

本轮修复的真实缺陷与该缺陷**为何能长期存活**是同一件事，故护栏按「让缺陷
无法再逃逸」而非「记录缺陷」来写。

BUG-1【P0 · 前后端契约断裂】九章事实分布面板自 2026-10-06 引入起永久空转
    后端 ``list_facts`` 的 ``stats.by_chapter`` 是**对象**
    ``{chapters, totals, by_fact_attr, by_source_kind, shared_count}``，
    前端 ``buildFactChapterStats`` 却按 ``Array.isArray`` 取值 ⇒ 恒 ``[]``。
    ⚠️ 致命之处：``frontend/src/types/facts.ts`` 当时把该字段也声明成**数组**，
    契约与实现**同错** ⇒ ``tsc --noEmit`` 在原理上不可能报错。
    该缺陷存活至今的第二个原因是 ``FactsChapterCoveragePanel`` /
    ``buildFactChapterStats`` **零用例** —— 一个「看起来正常的」空面板。

    ⇒ A 组用**真实 DB + 真实路由调用**坐实后端形状（本组在修复前即失败）；
    ⇒ B 组把形状锁成**跨语言断言**：后端真实响应 vs 前端类型声明 vs 前端读取
       判据三者必须一致，任一侧改动而另两侧未跟随时红。
    ⇒ 判据纪律：形状锚定**后端真实返回**（`list_facts` 的实际调用结果），
       绝不锚定任一侧的自我声明 —— 否则同错仍可能再次同错（这正是原缺陷）。

BUG-2【P1 · 分页页码可产出小数 / 非有限值】``buildFactsPagination``
    ``(offset)/limit + 1`` 未取整 ⇒ offset 非 limit 整倍数时得到小数页码并
    交给 antd ``Pagination current``；``Math.min/max`` 对 NaN 恒返回 NaN。

BUG-3【P1 · checkpoint 契约漂移】``facts_generation`` 缺席于单一事实源
    ``services/checkpoint_schema.CHECKPOINT_KINDS``（内容/目录两族都在，且都有
    ``sh._CHECKPOINT_KINDS[t] == cs.CHECKPOINT_KINDS[t]`` 的 parity 断言）。
    后果：生成的 ``checkpointSchema.ts`` 无 ``facts_result`` ⇒ 前端页面只能
    **内联第三份手抄**；且 facts 是**唯一没有** parity 断言的一族。
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import os
import re
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # …\backend
APP = os.path.join(BACKEND, "app")                                   # …\backend\app
ROOT = os.path.dirname(BACKEND)                                      # 仓库根
FE = os.path.join(ROOT, "frontend", "src")
sys.path.insert(0, BACKEND)


def _gf():
    from app.routers import global_facts as gf
    return gf


def _read(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    assert b"\xef\xbf\xbd" not in raw, f"{path} 含 U+FFFD（编码损坏）"
    return raw.decode("utf-8")


def _types_facts() -> str:
    return _read(os.path.join(FE, "types", "facts.ts"))


def _page() -> str:
    return _read(os.path.join(FE, "pages", "SchemeWorkbenchPage.tsx"))


# 三条真实事实（名称/取值均落在九大章节内，见 facts_classification）。
# ⚠️ is_shared 是**内容派生**（shared_chapters_for 关键词规则），不是可直写的列；
#    因此断言一律**与生产判据对账**，绝不手写第二份关键词规则。
SEEDS = (
    ("基坑开挖深度", "12.5m", "tech_param"),
    ("施工总工期", "90 日历天", "schedule"),
)


async def _seed(db):
    """经模块自身单一 INSERT 出口建事实（避免列清单漂移）。"""
    gf = _gf()
    # 项目须真实存在：`/chapters` 的 _validate_fact_scope 会校验（不存在即 404）
    await db.execute(
        "INSERT OR REPLACE INTO projects (id, name) VALUES ('r89p', 'R89')")
    for i, (name, value, cat) in enumerate(SEEDS):
        await db.execute(
            gf.MANUAL_FACT_INSERT_SQL,
            gf._manual_fact_row(
                fid=f"r89f{i}", pid="r89p", sid="", group_id="r89g",
                group_title="工程概况", name=name,
                content=gf._build_fact_content(name, value, False),
                category=cat, source_file="测试录入",
                is_simulated=False, confidence=1.0, is_resolved=True,
            ))
    await db.commit()


def _expected_shared_by_chapter() -> dict:
    """用**生产判据**算出逐章共性期望值（不复制关键词规则）。"""
    from app.routers.global_facts import CATEGORY_TO_FACT_TYPE
    from app.services.facts_classification import (
        chapter_of_row,
        shared_chapters_for,
    )
    out: dict[str, int] = {}
    for name, value, cat in SEEDS:
        if not shared_chapters_for(name, value):
            continue
        ch = chapter_of_row({
            "name": name, "value": value, "content": "",
            "category": cat, "fact_type": CATEGORY_TO_FACT_TYPE.get(cat, ""),
            "fact_key": "", "source": "", "source_ref": "",
        })
        if ch:
            out[ch] = out.get(ch, 0) + 1
    return out


async def _list_stats(db):
    """走真实路由函数取 list_facts 的 stats（不经 HTTP，避免鉴权噪声）。

    ⚠️ 签名是 ``list_facts(scheme_id, project_id, limit, offset, db)`` ——
    首位是 **scheme_id**，误传 project_id 会 404（历史 R79 同款坑）。
    本组事实是**项目级**（sid=''），故 scheme 传空串、project 传 "r89p"。
    """
    gf = _gf()
    res = await gf.list_facts("", "r89p", 0, 0, db=db)
    return res["stats"]


def _ts_fn_body(src: str, name: str) -> str:
    """取 TS 模块级函数的**函数体**（花括号配平）。

    不用正则切签名：签名跨行、含泛型/可选参数，且**返回类型本身是对象类型**
    （`): { page: number; ... } {`）—— 从第一个 ``{`` 起配平会取到**返回类型**
    而不是函数体。本轮首版连续踩了两次（先切到紧邻的下一个函数、再切到返回
    类型），判据本身错比缺判据更糟，故改为：先配平参数括号，再逐个跨过返回
    类型注解，**第一个**其后紧跟 ``{`` 的配平块才是函数体。
    """
    m = re.search(rf"export function {re.escape(name)}\s*\(", src)
    assert m, f"取不到函数 {name}"
    depth, j = 0, m.end() - 1
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    assert depth == 0, f"{name} 参数括号未配平"
    # 从参数列表结束处向右扫描，跳过返回类型注解
    k = j + 1
    while k < len(src):
        ch = src[k]
        if ch == "{":
            d2, e2 = 0, k
            while e2 < len(src):
                if src[e2] == "{":
                    d2 += 1
                elif src[e2] == "}":
                    d2 -= 1
                    if d2 == 0:
                        break
                e2 += 1
            rest = src[e2 + 1:]
            if rest.lstrip().startswith("{"):
                k = e2 + 1          # 这是**返回类型注解**，跳过继续找函数体
                continue
            return src[k:e2 + 1]   # 其后是代码 ⇒ 这才是函数体
        if ch in ";":
            k += 1
            continue
        if ch == "{" or ch == "\n" and src[k:k + 2].strip().startswith("{"):
            break
        k += 1
    raise AssertionError(f"{name} 未能定位函数体")


def _strip_ts_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释后再做源码断言。

    纪律：护栏判据不得被**自己写的说明注释**命中（本轮首版即踩：解释旧缺陷的
    注释里逐字引用了旧判据字符串，导致反向锁自咬）。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


# ============================================================
# A 组 · 后端真实形状（BUG-1 的行为基线；修复前本组即失败）
# ============================================================

class TestBackendByChapterRealShape:
    """用**真实 DB + 真实路由**坐实 ``stats.by_chapter`` 的形状。"""

    async def test_by_chapter_is_object_not_array(self, db_conn):
        """后端下发的是对象包裹 —— 数组假设必然取空。"""
        await _seed(db_conn)
        stats = await _list_stats(db_conn)
        bc = stats.get("by_chapter")
        assert isinstance(bc, dict), (
            "stats.by_chapter 应为对象包裹 {chapters, totals, ...}；"
            f"实得 {type(bc).__name__}。前端按数组读取会恒取空 —— 即 R89 BUG-1。"
        )
        assert not isinstance(bc, list), "by_chapter 不得退回裸数组（会与旧缺陷同形）"

    async def test_block_carries_all_consumer_keys(self, db_conn):
        """前端 buildFactChapterStats 读取的键必须在块上真实存在。"""
        await _seed(db_conn)
        bc = (await _list_stats(db_conn))["by_chapter"]
        # 前端读取面（buildFactChapterStats / FactsChapterCoveragePanel）
        for key in ("chapters", "totals", "by_fact_attr", "by_source_kind",
                    "shared_count"):
            assert key in bc, f"by_chapter 缺 {key}（前端/未来消费方会静默取空）"
        assert isinstance(bc["chapters"], list) and bc["chapters"], \
            "by_chapter.chapters 必须是九章非空列表（章节数恒为 9）"
        assert len(bc["chapters"]) == 9, \
            f"九大章节应恒为 9 条，实得 {len(bc['chapters'])}"

    async def test_chapter_row_carries_every_field_frontend_reads(self, db_conn):
        """章条目上必须有前端逐项读取的字段（缺一即面板少一列）。"""
        await _seed(db_conn)
        rows = (await _list_stats(db_conn))["by_chapter"]["chapters"]
        required = {"order", "key", "title", "count", "coverage", "missing_fields"}
        for row in rows:
            missing = required - set(row)
            assert not missing, f"章条目缺字段 {missing}（key={row.get('key')}）"

    async def test_per_chapter_shared_count_is_additive(self, db_conn):
        """✅ R89 加法式：逐章 shared_count 必须真实下发（此前只到顶层合计）。

        前端「共性 N」读的是**章条目**上的 shared_count；该键此前不存在 ⇒
        该列自引入起恒不显示。同时顶层合计不得因此丢失。

        ⚠️ 语义要点（判据据此设计）：一条事实可被**多个章节**复用
        （``shared_chapters_for`` 返回章节元组），所以
        「逐章之和」≥「顶层合计」，**不得**断言两者相等 —— 那会把
        「多章复用」误判成缺陷。
        """
        await _seed(db_conn)
        bc = (await _list_stats(db_conn))["by_chapter"]
        assert all("shared_count" in r for r in bc["chapters"]), \
            "每个章条目都必须带 shared_count（前端逐章读该键）"

        expected = _expected_shared_by_chapter()
        assert expected, "测试语料未命中任何共性规则 —— 断言会空转，先证伪语料"
        got = {r["key"]: int(r["shared_count"]) for r in bc["chapters"]}
        for key, n in expected.items():
            assert got.get(key, 0) == n, \
                f"章 {key} 的 shared_count 应为 {n}，实得 {got.get(key, 0)}"
        # 顶层合计（加法式不得破坏原键）
        from app.services.facts_classification import shared_chapters_for
        total_expected = sum(
            1 for name, value, _ in SEEDS if shared_chapters_for(name, value))
        assert bc["shared_count"] == total_expected, \
            f"顶层合计应为 {total_expected}，实得 {bc['shared_count']}"
        # 不变式：逐章之和 ≥ 顶层合计（多章复用只会放大，不会缩小）
        assert sum(got.values()) >= bc["shared_count"]

    async def test_existing_block_keys_untouched(self, db_conn):
        """零回归：既有键集合与类型不得因加法式改动而变。"""
        await _seed(db_conn)
        bc = (await _list_stats(db_conn))["by_chapter"]
        assert set(bc) == {
            "chapters", "totals", "by_fact_attr", "by_source_kind",
            "shared_count",
        }, f"by_chapter 键集合被改动：{sorted(bc)}"
        assert set(bc["chapters"][0]) == {
            "order", "key", "title", "count", "coverage", "total_fields",
            "covered_fields", "missing_fields", "shared_count",
        }, f"章条目键集合被改动：{sorted(bc['chapters'][0])}"

    async def test_chapters_endpoint_shape_unchanged(self, db_conn):
        """/chapters 走的是**另一条**路径（顶层 chapters 数组），不得被波及。"""
        await _seed(db_conn)
        gf = _gf()
        res = await gf.list_facts_by_chapters("", "r89p", db=db_conn)
        assert isinstance(res["chapters"], list) and len(res["chapters"]) == 9
        # 该端点复用 _chapter_stats_for_items 的章条目 ⇒ 新键加法式透传
        assert all("shared_count" in c for c in res["chapters"])

    async def test_counts_track_seeded_rows(self, db_conn):
        """汇总不是空壳：两条事实真的被计入（防「形状对了但恒 0」）。"""
        await _seed(db_conn)
        bc = (await _list_stats(db_conn))["by_chapter"]
        assert bc["totals"]["facts"] == len(SEEDS), bc["totals"]
        # 跨章合计必须等于种子条数（防某章漏计/重复计）
        assert sum(int(r["count"]) for r in bc["chapters"]) == len(SEEDS)
        assert any(int(r["count"]) > 0 for r in bc["chapters"]), \
            "九章全为 0 —— 计数链路断了（形状对但值恒 0）"


# ============================================================
# B 组 · 跨语言 parity（BUG-1 的复发防线）
# ============================================================

class TestFrontendBackendShapeParity:
    """三侧形状必须一致：后端真实响应 / 前端类型声明 / 前端读取判据。

    ⚠️ 判据纪律：形状锚定**后端真实返回**（A 组），前端两侧只做「与之比对」。
       若改为断言「前端声明 == 前端实现」，同错仍可能再次同错 —— 原缺陷正是
       「类型声明与实现同时写成数组」而双双逃过 tsc。
    """

    def test_ts_by_chapter_declared_as_object_block(self):
        """types/facts.ts 必须声明 FactChapterStatBlock（对象），不得回退数组。"""
        src = _types_facts()
        assert "export interface FactChapterStatBlock" in src, \
            "types/facts.ts 缺 FactChapterStatBlock —— by_chapter 的对象形状无类型承载"
        assert re.search(
            r"by_chapter\?:\s*FactChapterStatBlock\s*;", src), \
            "FactStats.by_chapter 必须声明为 FactChapterStatBlock（B-1 复发形态）"
        assert not re.search(r"by_chapter\?:\s*FactChapterStat\[\]", src), \
            "by_chapter 不得再声明为裸数组 —— 那正是 BUG-1 的根因（契约与实现同错）"

    def test_ts_block_declares_backend_keys(self):
        """块类型必须覆盖后端真实下发的全部键（逐键比对 A 组读到的形状）。"""
        src = _types_facts()
        m = re.search(
            r"export interface FactChapterStatBlock \{(.*?)\n\}", src, re.S)
        assert m, "取不到 FactChapterStatBlock 定义"
        body = m.group(1)
        for key in ("chapters", "totals", "by_fact_attr", "by_source_kind",
                    "shared_count"):
            assert re.search(rf"\b{key}\s*:", body), \
                f"FactChapterStatBlock 缺后端真实下发的键 {key}"

    def test_ts_per_chapter_shared_count_declared(self):
        """逐章 shared_count 必须在章条目类型上（B-2：前端读该键）。"""
        src = _types_facts()
        m = re.search(r"export interface FactChapterStat \{(.*?)\n\}", src, re.S)
        assert m, "取不到 FactChapterStat 定义"
        assert re.search(r"shared_count\?\s*:\s*number", m.group(1)), \
            "FactChapterStat 缺 shared_count —— 前端「共性 N」列恒为 0"

    def test_page_reads_object_shape_not_array_guard(self):
        """前端读取判据必须是「先 .chapters 再兜底裸数组」，不得只有数组分支。"""
        body = _ts_fn_body(_page(), "buildFactChapterStats")
        assert "bc?.chapters" in body, \
            "buildFactChapterStats 未读取 by_chapter.chapters（B-3 复发形态）"
        assert re.search(r"Array\.isArray\(bc\?\.chapters\)", body), \
            "by_chapter.chapters 必须做数组性判定"
        # 对象分支必须是**主**路径：裸数组只作兜底（否则等于回退到缺陷形态）
        assert re.search(
            r"Array\.isArray\(bc\)\s*\?\s*\(bc as unknown as FactChapterStat\[\]\)",
            body), "裸数组兜底分支形态被改动（应保留为第二分支）"
        assert re.search(
            r":\s*\(Array\.isArray\(bc\?\.chapters\) \? bc\.chapters : \[\]\)", body), \
            "对象分支必须是三元主路径（by_chapter.chapters）"

    def test_no_stale_array_only_criterion_left(self):
        """反向锁：**代码**中不得残留「仅判 Array.isArray(stats.by_chapter)」旧判据。

        ⚠️ 判据必须剥注释后再扫 —— 否则本文件/源码里解释该缺陷的说明文字会自咬。
        """
        code = _strip_ts_comments(_page())
        assert "Array.isArray(stats?.by_chapter)" not in code, \
            "旧判据 Array.isArray(stats?.by_chapter) 仍在代码中（对对象恒 false）"
        assert "Array.isArray(stats?.by_chapter) ? stats.by_chapter" not in code

    def test_facts_pagination_integer_guard(self):
        """分页页码必须取整并处理非有限值（B-4）。"""
        body = _ts_fn_body(_page(), "buildFactsPagination")
        assert "Math.floor(offset / limit) + 1" in body, \
            "页码必须 Math.floor 后再加 1（小数页码会让 Pagination 落在空页）"
        assert "Number.isFinite" in body, \
            "offset 必须做 isFinite 守卫（Math.min/max 对 NaN 恒返回 NaN）"
        # 反向锁：不得残留未取整的页码表达式。
        # ⚠️ 必须剥注释：函数体内说明该缺陷的注释逐字引用了旧形态 `offset/limit+1`，
        #    不剥注释则判据自咬（本轮实踩）。
        code = _strip_ts_comments(body)
        assert "/ limit + 1" not in code.replace(
            "Math.floor(offset / limit) + 1", ""), \
            "残留未取整的 `offset / limit + 1`（小数页码复发形态）"

    def test_checkpoint_schema_is_single_source_for_facts(self):
        """facts_generation 必须在单一事实源里（B-5：漂移根源）。"""
        from app.services import checkpoint_schema as cs
        assert "facts_generation" in cs.CHECKPOINT_KINDS, \
            "checkpoint_schema 缺 facts_generation ⇒ 生成的 TS 无 facts_result"
        kind, fields = cs.CHECKPOINT_KINDS["facts_generation"]
        assert kind == "facts_result"
        assert set(fields) == {
            "event", "message", "segment_stats", "cross_conflicts",
            "warnings", "group_count", "total_items",
        }, f"facts 白名单漂移：{sorted(fields)}"
        assert "facts_result" in cs.CHECKPOINT_FIELD_TYPES, \
            "facts_result 缺字段类型声明（生成的 TS 会全是 unknown）"

    def test_write_side_whitelist_derived_not_handwritten(self):
        """写入侧筛选白名单必须**派生**自 schema，不得再手写第二份。"""
        from app.routers import sse_checkpoint as sc
        from app.services.checkpoint_schema import FACTS_CHECKPOINT_FIELDS
        assert set(sc._FACTS_CHECKPOINT_FIELDS) == set(FACTS_CHECKPOINT_FIELDS) - {
            "event", "message"}, "sse_checkpoint 的筛选白名单与 schema 不一致"
        src = inspect.getsource(sc)
        m = re.search(r"_FACTS_CHECKPOINT_FIELDS\s*=\s*(.+?)\n\n", src, re.S)
        assert m and "SCHEMA_FACTS_CHECKPOINT_FIELDS" in m.group(1), \
            "_FACTS_CHECKPOINT_FIELDS 必须由 schema 派生（手写副本会静默分叉）"

    def test_router_whitelist_matches_schema(self):
        """路由侧 _CHECKPOINT_KINDS 与 schema 逐项相等（facts 族的 parity 断言）。"""
        from app.routers import sse_handlers as sh
        from app.services.checkpoint_schema import CHECKPOINT_KINDS
        for task_type in CHECKPOINT_KINDS:
            assert task_type in sh._CHECKPOINT_KINDS, \
                f"路由侧缺 {task_type}"
            assert sh._CHECKPOINT_KINDS[task_type][0] == CHECKPOINT_KINDS[task_type][0]
            assert set(sh._CHECKPOINT_KINDS[task_type][1]) == set(
                CHECKPOINT_KINDS[task_type][1]), \
                f"{task_type} 白名单与单一事实源不一致"

    def test_generated_ts_contains_facts_result(self):
        """生成的 checkpointSchema.ts 必须含 facts_result（前端不再内联手抄）。"""
        src = _read(os.path.join(FE, "types", "checkpointSchema.ts"))
        assert "export interface facts_result {" in src
        assert re.search(r"facts_result\?:\s*facts_result\s*;", src), \
            "TaskTerminalInfo 缺 facts_result 键"
        assert "AUTO-GENERATED" in src, "checkpointSchema.ts 必须是生成产物"

    def test_generated_ts_matches_generator_output_byte_exact(self):
        """再生成一次必须与磁盘**逐行**一致（防手改生成物）。

        ⚠️ 必须归一行尾：`tools/generate_checkpoint_schema.py` 用
        ``Path.write_text``，在 Windows 上会把 ``\\n`` 翻译成 ``\\r\\n``，
        而 ``generate_typescript_schema()`` 返回纯 LF ⇒ 直接比对**恒不等**。
        这是本轮实测踩到的陷阱（不是缺陷，故不修生成器，只在判据里归一）。
        """
        from app.services.checkpoint_schema import generate_typescript_schema
        disk = _read(os.path.join(FE, "types", "checkpointSchema.ts"))
        gen = generate_typescript_schema()
        assert gen.replace("\r\n", "\n") == disk.replace("\r\n", "\n"), \
            "checkpointSchema.ts 与生成器输出不一致 —— 有人手改了生成物"

    def test_page_uses_generated_facts_result_type(self):
        """页面必须引用生成的类型，不得再内联第三份手抄形状。"""
        src = _page()
        assert 'from "../types/checkpointSchema"' in src, \
            "页面未 import 生成的 checkpointSchema"
        assert re.search(
            r"import type \{ facts_result as GeneratedFactsResult \}", src), \
            "页面未引用生成的 facts_result 类型（手抄副本会再次分叉）"
        assert re.search(r"facts_result\?:\s*GeneratedFactsResult\s*;", src), \
            "TaskTerminalInfo.facts_result 必须用生成类型"
        # 旧的内联形状：segment_stats 逐字段声明
        assert not re.search(
            r"facts_result\?:\s*\{\s*event\?:\s*string", src), \
            "页面仍在内联 facts_result 形状（第三份手抄）"

    def test_fact_category_options_match_backend(self):
        """前端兜底分类表 ↔ 后端 CATEGORY_TITLES 逐项相等（F-5 缺失的 parity）。"""
        gf = _gf()
        backend = dict(gf.CATEGORY_TITLES)
        src = _page()
        m = re.search(
            r"const FACT_CATEGORY_OPTIONS[^=]*=\s*\[(.*?)\n\];", src, re.S)
        assert m, "取不到 FACT_CATEGORY_OPTIONS"
        fe = dict(re.findall(
            r"\{\s*value:\s*\"([^\"]+)\",\s*label:\s*\"([^\"]+)\"\s*\}",
            m.group(1)))
        assert fe, "FACT_CATEGORY_OPTIONS 解析为空"
        assert set(fe) == set(backend), (
            f"分类键不一致：前端缺 {sorted(set(backend) - set(fe))} / "
            f"前端多 {sorted(set(fe) - set(backend))}")
        diff = {k: (fe[k], backend[k]) for k in backend if fe[k] != backend[k]}
        assert not diff, f"分类中文名不一致：{diff}"


# ============================================================
# C 组 · 零回归与字节体检
# ============================================================

class TestNoRegressionAndByteHealth:
    TOUCHED = [
        os.path.join(APP, "routers", "global_facts.py"),
        os.path.join(APP, "services", "checkpoint_schema.py"),
        os.path.join(APP, "routers", "sse_checkpoint.py"),
        os.path.join(FE, "types", "facts.ts"),
        os.path.join(FE, "types", "checkpointSchema.ts"),
        os.path.join(FE, "pages", "SchemeWorkbenchPage.tsx"),
    ]

    @pytest.mark.parametrize("path", TOUCHED)
    def test_byte_health(self, path):
        raw = open(path, "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path} 含 BOM"
        assert b"\xef\xbf\xbd" not in raw, f"{path} 含 U+FFFD"
        assert b"\r\r\n" not in raw, f"{path} 行尾损坏（\\r\\r\\n）"
        assert b"\x00" not in raw, f"{path} 含 NUL"
        if path.endswith(".py"):
            ast.parse(raw.decode("utf-8"))

    def test_fact_classification_not_touched_by_derivation(self):
        """per-chapter shared 聚合不得引入新的维度派生调用（零 AI、零额外查询）。"""
        src = _read(_gf().__file__)
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_chapter_stats_for_items")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        # 该函数是纯聚合：不得出现 DB 读写或 AI 调用
        assert not any(c.startswith(("db.", "await", "cursor")) for c in called), \
            f"_chapter_stats_for_items 出现了非聚合调用：{sorted(called)}"
        assert "classify" not in " ".join(called), \
            "per-chapter 聚合不得触发维度派生（纯聚合即可算出）"
