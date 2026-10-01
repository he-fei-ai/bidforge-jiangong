"""遗留问题收口护栏（2026-09-29 第七轮）。

对应上一轮审计列出的 4 个非阻断遗留项，本轮全部落地为可回归的断言：

1. **死参数** —— ``facts_classification.classify_fact_attr`` 曾声明 ``category=""``
   形参但函数体从未读取。它是「fact_attr 依赖 22 类分类」的误导性暗示，会让后续
   维护者把分类口径错误耦合进属性判定（属性判定是纯文本正则，与 category 正交）。
   现直接删参数（3 处调用点同步），并用 ``inspect.signature`` 锁死签名，
   防止死参数悄悄回归。

2. **冗余 OR** —— ``routers/global_facts._fact_dimension_fields`` 对 ``is_shared``
   做了「外层 bool(stored) or dims / 内层 bool(row) or dims」的双重合并，结果等价
   但看起来像两套语义。更隐蔽的是它另调一次 ``classify_source_kind`` —— 因为传给
   ``dimensions_for_row`` 的 dict **忘了带 source / source_ref**，派生值只能拿到
   默认 ``bid_doc``。现把来源一并传入，判据收敛到 ``dimensions_for_row`` 唯一实现。

3. **章节失效标记（原「需单立子项」）** —— 全局事实变更后，已生成正文仍是当时的
   事实快照，导出缓存被清空，却**没有任何信号告知用户这一章已过时**。本轮以
   「方案级单点时间戳 ``schemes.facts_updated_at`` + 读侧派生 ``facts_stale``」落地：
   写侧只有 1 处（``invalidate_export_cache(..., facts_touched=True)``），而正文
   写路径有 13 处 —— 若给 sections 加布尔列就要改 13 处，「漏改一处」即让重生后
   的章节被永久标成过时（本仓反复踩的同类陷阱）。读侧派生天然自愈：任何一次正文
   重写都会推进 ``sections.updated_at``，标记自动消失。
   **只提示、绝不静默重写**用户已编辑的章节（既定设计红线）。

4. **R13 漏改点** —— AGENTS.md §5.5 记载的全局性问题：单连接 + aiosqlite 下
   ``db.execute()`` 可能返回 None，全仓 7 处 ``.rowcount`` 各自裸取。命中后分两种
   结局：外层有 ``except`` → 异常被吞、**写操作未生效却零日志**
   （``clear_interrupted_items`` 即典型：中断遗留 running 项静默残留，UI 永远
   「运行中」、18 项永久判缺失）；外层无守卫 → 直接 500。现统一收敛到
   ``app.db.safe_rowcount`` 单一出口。
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import uuid
from types import SimpleNamespace

import pytest

import app.db as _appdb
from app.db import get_conn, init_db, safe_rowcount
from app.routers import global_facts as gf
from app.routers import sections as sec
from app.routers.bid_analysis import clear_interrupted_items
from app.services import facts_classification as fc
from app.services.facts_extractor import invalidate_export_cache

pytestmark = pytest.mark.filterwarnings("ignore")


async def _none_coro():
    """R13 模拟：execute() 返回 None（连接瞬时损坏）。"""
    return None


# ---------------------------------------------------------------------------
# 公共 fixture：独立临时库（G12-7 口径 —— 必须还原 DB_PATH 并清空模块级连接，
# 否则后续走真实 get_conn 的用例会连到已废弃的临时库，产生顺序依赖的间歇失败）
# ---------------------------------------------------------------------------

@pytest.fixture
async def ctx(tmp_path):
    prev = _appdb.DB_PATH
    _appdb.DB_PATH = tmp_path / "fixes-20260930.sqlite"
    await init_db()
    db = await get_conn()
    yield db
    await _appdb.close_db()
    _appdb.DB_PATH = prev


async def _seed(db, *, fact_ts: str = "2026-09-01 10:00:00") -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name,facts_updated_at)"
                     " VALUES(?,?,?,?)", (sid, pid, "s", fact_ts))
    await db.commit()
    return pid, sid


async def _add_section(db, sid, *, title="第一章 工程概况",
                       content="开挖深度 12.5m 的基坑。",
                       updated="2026-08-15 10:00:00") -> str:
    nid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections(id,scheme_id,title,content,updated_at)"
        " VALUES(?,?,?,?,?)", (nid, sid, title, content, updated))
    await db.commit()
    return nid


async def _stale_flags(db, sid, include_content=True) -> dict:
    """{title: facts_stale} —— 拍平 _build_tree 的返回，便于逐项断言。"""
    tree = await sec._build_tree(db, sid, include_content=include_content)
    out: dict = {}

    def walk(nodes):
        for n in nodes:
            out[n["title"]] = int(n["facts_stale"])
            if n["children"]:
                walk(n["children"])

    walk(tree)
    return out


async def _row(db, sql, params=()) -> object:
    """R13 口径的取行助手（execute() 可能返回 None）。"""
    cur = await db.execute(sql, params)
    return await cur.fetchone() if cur is not None else None



# ============================================================
# 1. 死参数：classify_fact_attr 只接受 (name, value)
# ============================================================

class TestNoDeadCategoryParam:
    def test_signature_has_exactly_name_and_value(self):
        # 死参数回归的精确护栏：一旦有人为「对称」把 category 加回来，这里立刻失败。
        params = set(inspect.signature(fc.classify_fact_attr).parameters)
        assert params == {"name", "value"}, (
            f"classify_fact_attr 又出现了未使用形参 {params - {'name', 'value'}} —— "
            "事实属性只由 name/value 文本决定，与 category 正交；"
            "若确有需要，请先在函数体内真正使用它，再改本断言。")

    def test_no_category_reference_in_body(self):
        # 双保险：即使有人把死参数改叫别的名字，用 AST 查实际 Name 引用也能抓到。
        # （用 AST 而非文本搜索：docstring 里会合法地提到 category 一词。）
        mod = ast.parse(inspect.getsource(fc.classify_fact_attr))
        fn = mod.body[0]
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Store)}
        assert "category" not in used, (
            "category 又出现在函数体里但未参与任何判定 —— 请确认它是真正的逻辑输入，"
            "而不是装饰性的死参数。")

    @pytest.mark.parametrize("name,value,expected", [
        ("基坑开挖深度", "12.5m", "quantitative"),
        ("适用规范", "GB 50330-2013", "norm"),
        ("条文要求", "第 3.1.2 条规定", "norm"),
        ("与既有建筑距离", "5m", "relation"),       # 关系优先于定量
        ("周边地质", "粉质黏土", "relation"),        # 文本含「周边」即判为关系
        ("质量要求", "无特殊要求", "qualitative"),
    ])
    def test_behaviour_unchanged(self, name, value, expected):
        # 删参数不得改变判定结果（语义只与 name/value 有关）。
        assert fc.classify_fact_attr(name, value) == expected

    def test_dimensions_uses_same_implementation(self):
        # 编排函数与纯函数必须同源 —— 防止「维度派生处另写一份属性判定」。
        for name, value in [("开挖深度", "12.5m"), ("规范", "JGJ 120-2012"),
                            ("周边环境", "距红线 8m"), ("说明", "无特别要求")]:
            assert fc.classify_fact_dimensions(name, value)["fact_attr"] \
                == fc.classify_fact_attr(name, value), (name, value)


# ============================================================
# 2. 四维派生：判据收敛到 dimensions_for_row 唯一实现
# ============================================================

class TestFactDimensionFields:
    ROW = {
        "id": "f1", "category": "tech_param", "fact_type": "design_param",
        "source": "招标文件 3.2 节", "source_ref": "", "fact_key": "depth",
    }

    def test_is_shared_parity_with_single_source(self):
        # 逐字节 parity：外层不再重复合并 stored 与派生值。
        for name, value in [("周边关系", "紧邻既有建筑"), ("开挖深度", "12.5m")]:
            for stored in (0, 1, None):
                row = dict(self.ROW, is_shared=stored)
                got = gf._fact_dimension_fields(row, name, value, "depth", [])
                dims = fc.dimensions_for_row({
                    "name": name, "value": value,
                    "category": row["category"], "fact_type": "design_param",
                    "fact_key": "depth", "source": row["source"],
                    "source_ref": row["source_ref"], "is_shared": stored})
                assert got["is_shared"] == bool(dims["is_shared"]), (name, stored)

    def test_is_shared_union_semantics(self):
        # 库列写 0，但文本命中 SHARED_FACT_RULES（含「深度」）→ 并集语义下必须为 True。
        row = dict(self.ROW, is_shared=0)
        got = gf._fact_dimension_fields(row, "基坑开挖深度", "12.5m", "depth", [])
        assert got["is_shared"] is True
        assert got["shared_chapters"]        # 复用章节集合非空

    def test_source_kind_comes_from_source_text_not_default(self):
        # 关键回归点：旧实现传给 dimensions_for_row 的 dict 缺 source/source_ref，
        # 派生值只能落到默认 bid_doc；现在必须真正识别出「勘察报告」。
        row = dict(self.ROW, is_shared=0, source="", source_ref="")
        assert gf._fact_dimension_fields(
            row, "土层分布", "杂填土", "k", [])["source_kind"] == "bid_doc"

        row2 = dict(self.ROW, is_shared=0, source="地质勘察报告", source_ref="")
        assert gf._fact_dimension_fields(
            row2, "土层分布", "杂填土", "k", [])["source_kind"] == "survey"

    def test_stored_values_take_precedence(self):
        # 人工归类不被重派生覆盖（尊重人工归类是既定语义）。
        row = dict(self.ROW, chapter="overview", fact_attr="qualitative",
                   source_kind="manual", is_shared=1)
        got = gf._fact_dimension_fields(row, "开挖深度", "12.5m", "k", [])
        assert got["chapter"] == "overview"
        assert got["fact_attr"] == "qualitative"
        assert got["source_kind"] == "manual"
        assert got["is_shared"] is True

    def test_missing_columns_fall_back_to_derivation(self):
        # 2026-09-24 前的历史行四个维度列全空 → 惰性派生，不丢事实。
        row = dict(self.ROW)
        for k in ("chapter", "fact_attr", "source_kind", "is_shared"):
            row.pop(k, None)
        got = gf._fact_dimension_fields(row, "深度", "12.5m", "k", [])
        assert got["chapter"] == "technique"
        assert got["fact_attr"] == "quantitative"
        assert got["source_kind"] == "bid_doc"
        assert "chapter_title" in got and isinstance(got["shared_chapters"], list)


# ============================================================
# 3. R13：.rowcount 唯一出口 safe_rowcount
# ============================================================

class TestSafeRowcount:
    def test_none_returns_zero(self, caplog):
        # 契约：cur is None 时必须返回 0 而非 AttributeError，且可观测（打告警）。
        with caplog.at_level("WARNING"):
            assert safe_rowcount(None, what="测试写入") == 0
        assert any("R13" in r.message for r in caplog.records)

    @pytest.mark.parametrize("value,expected", [(3, 3), (0, 0), (-1, 0), (None, 0)])
    def test_real_cursor(self, value, expected):
        assert safe_rowcount(SimpleNamespace(rowcount=value)) == expected


class TestRowcountIsCentralized:
    """静态护栏：.rowcount 只能出现在 app/db.py 的统一出口里。"""

    def test_no_bare_rowcount_outside_db_helper(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app")
        offenders: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith((".", "__")) and d != "tests"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                for i, line in enumerate(
                        open(fp, encoding="utf-8", errors="replace"), 1):
                    if ".rowcount" not in line:
                        continue
                    code = line.split("#", 1)[0].strip()
                    if not code:
                        continue                      # 纯注释/文档里的提及
                    if code.startswith(('"', "'", "f\"", "f'")):
                        continue                      # 字符串字面量
                    rel = os.path.relpath(fp, os.path.dirname(root))
                    if rel.replace(os.sep, "/") != "app/db.py":
                        offenders.append(f"{rel}:{i}: {code}")
        assert not offenders, (
            "发现绕过 safe_rowcount 的裸 .rowcount 访问（R13 回归）：\n"
            + "\n".join(offenders))

    def test_no_getattr_rowcount_bypass_outside_db_helper(self):
        """`getattr(cur, "rowcount", 0)` 变体也必须收敛（R13 静默失败防线）。

        ✅ 2026-09-30 补：本护栏原先只匹配字面 `.rowcount`，于是
        `int(getattr(cur, "rowcount", 0) or 0)` 这一写法可以**完全绕过**它。
        该写法不会抛 AttributeError，但 execute() 返回 None 时**静默返回 0、
        零日志** —— 恰恰是 §4.12 要消除的「写操作没生效却无任何日志」：
          · global_facts._reconcile_parse_status  → 少报修正条数，无人察觉；
          · task_registry._set_task_status_db    → 调用方把 R13 写失败误判为
            「任务已是终态」（正常的竞态守卫结果），故障被伪装成正常语义。

        两者的 rowcount 字符串都出现在**代码**里（不是注释/纯文档），
        故按同样规则排除注释行与字符串字面量行。
        """
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app")
        offenders: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith((".", "__")) and d != "tests"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, os.path.dirname(root))
                if rel.replace(os.sep, "/") == "app/db.py":
                    continue
                for i, line in enumerate(
                        open(fp, encoding="utf-8", errors="replace"), 1):
                    if "rowcount" not in line:
                        continue
                    code = line.split("#", 1)[0].strip()
                    if not code:
                        continue                      # 纯注释行
                    # 排除「整行就是一个字符串字面量」的文档/常量声明行
                    if code.startswith(('"', "'", "f\"", "f'")):
                        continue
                    if re.search(r"getattr\s*\([^,]+,\s*[\"']rowcount[\"']", code):
                        offenders.append(f"{rel}:{i}: {code}")
        assert not offenders, (
            "发现用 getattr(cur,'rowcount',0) 绕过 safe_rowcount 的写法"
            "（R13 静默失败，零日志）：\n" + "\n".join(offenders))


class TestClearInterruptedItemsR13:
    async def test_survives_execute_returning_none(self):
        # R13：execute() 返回 None 时旧实现抛 AttributeError，又被 except 吞掉，
        # 中断遗留的 running 项静默残留。现必须返回 0 而非抛错。
        db = SimpleNamespace(execute=_none_coro, commit=_none_coro)
        assert await clear_interrupted_items(db, "any-project") == 0

    async def test_counts_rows_on_real_connection(self, ctx):
        _, sid = await _seed(ctx)
        pid = (await _row(ctx, "SELECT project_id FROM schemes WHERE id=?",
                          (sid,)))[0]
        await ctx.execute(
            "INSERT INTO bid_analysis_items(id,project_id,item_id,status)"
            " VALUES(?,?,?,?)", (f"{pid}_project_overview", pid,
                                 "project_overview", "running"))
        await ctx.commit()
        assert await clear_interrupted_items(ctx, pid) == 1
        row = await _row(ctx,
                         "SELECT status, error FROM bid_analysis_items"
                         " WHERE project_id=? AND item_id=?",
                         (pid, "project_overview"))
        assert row is not None and row["status"] == "error" and row["error"]


# ============================================================
# 4. 章节失效标记：schemes.facts_updated_at → sections.facts_stale
# ============================================================

class TestFactsStaleMarker:
    async def test_column_exists_after_migrate(self, ctx):
        cols = {r[1] for r in await (await ctx.execute(
            "PRAGMA table_info(schemes)")).fetchall()}
        assert "facts_updated_at" in cols

    async def test_facts_touched_bumps_scheme_timestamp(self, ctx):
        _, sid = await _seed(ctx, fact_ts="")
        assert (await _row(ctx, "SELECT facts_updated_at FROM schemes WHERE id=?",
                           (sid,)))[0] == ""
        await invalidate_export_cache(ctx, sid, facts_touched=True)
        assert (await _row(ctx, "SELECT facts_updated_at FROM schemes WHERE id=?",
                           (sid,)))[0]

    async def test_default_call_does_not_touch_timestamp(self, ctx):
        # 向后兼容：既有调用方（如 bid_analysis 的提取项失效）没改事实表，
        # 绝不得让已生成正文被误标为过时。
        _, sid = await _seed(ctx, fact_ts="")
        await invalidate_export_cache(ctx, sid)          # 默认 facts_touched=False
        assert (await _row(ctx, "SELECT facts_updated_at FROM schemes WHERE id=?",
                           (sid,)))[0] == ""

    async def test_section_before_fact_change_is_stale(self, ctx):
        _, sid = await _seed(ctx, fact_ts="2026-09-01 10:00:00")
        await _add_section(ctx, sid, updated="2026-08-15 10:00:00")
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 1

    @pytest.mark.parametrize("updated,expected", [
        ("2026-08-15 10:00:00", 1),   # 早于事实变更 → 过时
        ("2026-09-01 10:00:00", 0),   # 恰好等于 → 不标记（已含最新事实）
        ("2026-09-02 09:00:00", 0),   # 晚于事实变更 → 不过时
    ])
    async def test_three_way_comparison(self, ctx, updated, expected):
        _, sid = await _seed(ctx, fact_ts="2026-09-01 10:00:00")
        await _add_section(ctx, sid, updated=updated)
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == expected

    async def test_timestamp_format_mismatch_is_normalized(self, ctx):
        # 陷阱：Python datetime.now().isoformat() 用 'T' + 微秒，SQLite
        # datetime('now','localtime') 用空格。字符串直比会得出**相反**结论
        # （'T' 0x54 > ' ' 0x20），必须交给 datetime() 归一化。
        _, sid = await _seed(ctx, fact_ts="2026-09-01T10:00:00.123456")
        await _add_section(ctx, sid, updated="2026-08-15 10:00:00")
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 1

        await _add_section(ctx, sid, title="第二章 施工工艺",
                           updated="2026-09-01 10:00:00.500")
        assert (await _stale_flags(ctx, sid))["第二章 施工工艺"] == 0

    async def test_empty_content_is_never_stale(self, ctx):
        # 没写正文的章节没有任何可过时的内容。
        _, sid = await _seed(ctx, fact_ts="2026-09-01 10:00:00")
        await _add_section(ctx, sid, content="")
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 0

    async def test_no_scheme_timestamp_is_never_stale(self, ctx):
        # 向后兼容：旧方案无时间戳 → 无从判定，不打扰用户。
        _, sid = await _seed(ctx, fact_ts="")
        await _add_section(ctx, sid, updated="2020-01-01 00:00:00")
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 0

    async def test_regeneration_clears_the_marker(self, ctx):
        # 自愈性：正文重写推进 updated_at → 标记自动消失，无需任何清理代码。
        _, sid = await _seed(ctx, fact_ts="2026-09-01 10:00:00")
        nid = await _add_section(ctx, sid, updated="2026-08-15 10:00:00")
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 1
        await ctx.execute(
            "UPDATE sections SET content=?, updated_at=? WHERE id=?",
            ("开挖深度 15.0m 的基坑。", "2026-09-05 10:00:00", nid))
        await ctx.commit()
        assert (await _stale_flags(ctx, sid))["第一章 工程概况"] == 0

    async def test_facts_stale_survives_contentless_poll(self, ctx):
        # include_content=False（3s 轻量轮询）也必须带上标记，否则轮询期间的
        # 徽标会被「指纹相同」的守卫吞掉。
        _, sid = await _seed(ctx, fact_ts="2026-09-01 10:00:00")
        await _add_section(ctx, sid)
        node = (await sec._build_tree(ctx, sid, include_content=False))[0]
        assert int(node["facts_stale"]) == 1
        assert "content" not in node            # 轮询瘦身不被破坏


class TestFactsStaleSql:
    """口径护栏：时间比较必须走 datetime() 归一化，绝不退回字符串直比。"""

    def test_uses_datetime_normalization_and_join(self):
        src = inspect.getsource(sec._build_tree)
        assert "datetime(" in src and "LEFT JOIN schemes" in src
        assert "COALESCE(sec.content, '')" in src
        assert "COALESCE(sch.facts_updated_at, '')" in src


# ============================================================
# 前端指纹护栏（跨语言一致性断言放 pytest 侧 —— 本仓前端未装 @types/node）
# ============================================================

def test_tree_fingerprint_includes_facts_stale():
    """facts_stale 必须参与 treeFingerprint，否则轻量轮询会吞掉刚出现的徽标。"""
    fp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "frontend", "src", "pages", "SchemeWorkbenchPage.tsx")
    if not os.path.exists(fp):
        pytest.skip("前端源码不在当前工作区")
    src = open(fp, encoding="utf-8").read()
    m = re.search(r"export function treeFingerprint[\s\S]{0,900}?\n\}", src)
    assert m, "未找到 treeFingerprint 实现"
    assert "facts_stale" in m.group(0), (
        "treeFingerprint 未纳入 facts_stale —— 3s 轻量轮询会因指纹相同而保留旧树，"
        "事实刚变更后的「事实已变更」徽标要等下一次完整 load() 才出现。")
