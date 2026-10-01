"""全局事实 + 正文生成 双模块深度审计护栏（2026-09-29 第六轮）。

锁定四类缺陷，全部属于本仓反复出现的同一模式 ——
**「同一业务判据在 2~3 处各自实现，改一处漏一处」**：

A. 【P1 · 静默数据丢失】分组重建（PATCH /global-facts/{group_id}）的 INSERT 只写
   15 列，把提取期已落库的 8 个溯源/单位列（chunk_hash / value_unit / fact_type /
   evidence_kind / page_ref / zone_type / is_safety_critical / is_stale）静默清空。
   ``list_facts`` 的注释明确承诺这些字段「不因重新拉取丢失（不丢溯源信息）」，
   但一次分组编辑即全部归零 → 界面「刷新 == 流式」的承诺被自己打破。

B. 【P1 · 分类口径分叉】``_apply_item_updates`` 改 category 只写 category 列，
   chapter/fact_attr 沿用旧值；而两条读路径（``list_facts`` / ``_load_facts_rows``）
   都是「库值优先」→ 改了分类后九大章节归属永远停在旧值，直到下一次重新提取。

C. 【P1 · 惰性派生漏改点】``sse_handlers._load_facts_rows`` 直接读 chapter 原始列，
   没有 ``routers/global_facts.list_facts`` 的惰性派生兜底 → 手工新增、分组编辑
   后落库的 fact 其 chapter 恒为空 → ``facts_chapter_inject`` 的「本章事实前置」
   对这批事实完全失效（界面章节视图显示已分类，正文生成却匹配不到）。

D. 【P2 · 口径分叉 / 注释与实现不符】``_load_facts_rows`` 自维护 WHERE/ORDER BY，
   而 ``build_injectable_facts_query`` 的 docstring 声称「与 sse_handlers
   _load_facts_rows 共用同一段 SQL」—— 声明与实现不符，改一处必漏一处。

另含：分组重建内 ``meta_by_name`` 同名事实覆盖导致第一条的溯源被丢弃。

E. 【P2 · 导出缓存漏失效】事实写路径的缓存失效口径分叉：``create_fact`` /
   ``update_fact`` 两处行级分支 / ``adjust_facts`` 只判 ``scheme_id`` 非空并裸调
   ``invalidate_export_cache`` —— 项目级事实（scheme_id 为空、供项目下全部方案
   共用）落库后**一个缓存都不失效**，导出继续复用旧产物。现全部收敛到
   ``_invalidate_fact_scope_cache``，并有源码护栏禁止裸调回归。

F. 【P2 · 向后兼容】``_load_facts_rows`` 改用统一查询后只按 ``dict(r)`` 解包行对象，
   位置序列行（测试 mock / 历史调用方）会抛 TypeError 并被吞成「无事实」——
   表现为「明明有事实却不注入正文」且无日志。现位置序列原样透传（保持既有
   行契约），映射行才做投影 + chapter 惰性派生。
"""
import uuid

import pytest

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.sse_handlers as sh
from app.db import get_conn, init_db
from app.models import FactItem
from app.services import facts_classification as fc


pytestmark = pytest.mark.filterwarnings("ignore")


# global_facts 全列（与 schema_sql 顺序一致）；种子 SQL 由列名动态生成，
# 避免手写 VALUES 占位符时出现「列数与占位数不一致」的静默错位。
_FACT_COLS = (
    "id", "project_id", "scheme_id", "group_id", "group_title", "title",
    "content", "category", "source_ref", "is_simulated", "confidence",
    "is_resolved", "has_conflict", "conflict_keys", "fact_key", "chunk_hash",
    "value_unit", "fact_type", "evidence_kind", "page_ref", "zone_type",
    "is_safety_critical", "norm_group", "chapter", "fact_attr", "source_kind",
    "is_shared", "is_stale",
)
_SEED_SQL = (
    "INSERT INTO global_facts (%s) VALUES (%s)"
    % (", ".join(_FACT_COLS), ",".join("?" for _ in _FACT_COLS))
)


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "audit-20260929.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


# 提取期已落库、分组重建曾静默清空的列（列名 → 种子值）
_PROVENANCE_SEED = {
    "chunk_hash": "sha1_of_source_chunk",
    "value_unit": "m",
    "fact_type": "design_param",
    "evidence_kind": "table",
    "page_ref": 12,
    "zone_type": "machinery_stat",
    "is_safety_critical": 1,
    "is_stale": 1,
}


async def _seed_fully_populated_fact(db, pid, sid, gid="g1", *,
                                     value="12.5", category="tech_param",
                                     chapter="overview", name="基坑深度",
                                     is_stale=1, fact_attr="quantitative"):
    """按提取管线同口径写入一条【全部溯源列非空】的事实，返回行 id。

    is_stale 默认 1：非默认值，才能证明「分组重建」真的保住了旧值而非巧合为 0。
    需要走注入路径的用例请显式传 is_stale=0（门控排除 stale 行）。
    fact_attr 可设为明显错误的值，用于证明「改分类」真的触发了重派生。
    """
    fid = uuid.uuid4().hex
    content = gf._build_fact_content(name, value, False)
    vals = (
        fid, pid, sid, gid, "技术参数", name, content, category,
        '[{"file":"招标文件","quote":"基坑深度12.5m"}]', 0, 0.9, 1, 0, "",
        name, _PROVENANCE_SEED["chunk_hash"], _PROVENANCE_SEED["value_unit"],
        _PROVENANCE_SEED["fact_type"], _PROVENANCE_SEED["evidence_kind"],
        _PROVENANCE_SEED["page_ref"], _PROVENANCE_SEED["zone_type"],
        _PROVENANCE_SEED["is_safety_critical"], "material", chapter,
        fact_attr, "bid_doc", 0, is_stale,
    )
    assert len(vals) == len(_FACT_COLS), (
        f"种子值 {len(vals)} 个 vs 列 {len(_FACT_COLS)} 个")
    await db.execute(_SEED_SQL, vals)
    await db.commit()
    return fid


async def _row(db, fid):
    cur = await db.execute("SELECT * FROM global_facts WHERE id=?", (fid,))
    return dict(await cur.fetchone())


# ---------------------------------------------------------------------------
# A. 分组重建不得清空已落库的溯源/单位列
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", sorted(_PROVENANCE_SEED))
async def test_group_rebuild_preserves_provenance_columns(ctx, col):
    """分组编辑（重建）必须保留提取期已落库的溯源/单位列。

    旧实现的 INSERT 只写 15 列，未列入的列回落到 DB 默认值
    （'' / NULL / 0）→ 用户一次分组编辑即永久丢失来源页码、计量单位、
    证据类型、语义区与增量指纹。
    """
    db, pid, sid = ctx
    await _seed_fully_populated_fact(db, pid, sid, gid="g1")
    await gf.update_fact(
        "g1", gf.FactGroupUpdate(id="g1", title="技术参数",
                                 content="- **基坑深度**: 12.5 m"),
        scheme_id=sid, db=db)
    cur = await db.execute("SELECT id FROM global_facts WHERE group_id='g1'")
    ids = [dict(r)["id"] for r in await cur.fetchall()]
    assert len(ids) == 1, "单行分组合建后仍应为单行"
    new_row = await _row(db, ids[0])
    for c, expect in _PROVENANCE_SEED.items():
        got = new_row.get(c)
        assert got == expect, (
            f"分组重建清空了已落库列 {c}：{expect!r} → {got!r}")


async def test_group_rebuild_keeps_dimension_columns(ctx):
    """九大章节四维标注：库列有值时重建后仍保留（尊重提取期标注）。"""
    db, pid, sid = ctx
    await _seed_fully_populated_fact(db, pid, sid, gid="g2", chapter="overview")
    await gf.update_fact(
        "g2", gf.FactGroupUpdate(id="g2", title="技术参数",
                                 content="- **基坑深度**: 12.5 m"),
        scheme_id=sid, db=db)
    cur = await db.execute(
        "SELECT chapter, fact_attr, source_kind FROM global_facts "
        "WHERE group_id='g2'")
    r = await cur.fetchone()
    assert (r["chapter"], r["fact_attr"], r["source_kind"]) == (
        "overview", "quantitative", "bid_doc"), \
        "重建后四维标注应与提取期一致（overview/quantitative/bid_doc）"


async def test_group_rebuild_same_title_twice_keeps_first_provenance(ctx):
    """同名两条事实（不同值）重建时不得丢弃其中一条的溯源。

    旧实现用 dict 推导按 title 建索引，同名时后写覆盖先写 →
    第一条的 source_ref / page_ref 被第二条顶掉。
    """
    db, pid, sid = ctx
    await _seed_fully_populated_fact(
        db, pid, sid, gid="g3", value="12.5", name="基坑深度")
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, fact_key, page_ref, value_unit) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, sid, "g3", "技术参数", "基坑深度",
         gf._build_fact_content("基坑深度", "14.0", False), "tech_param",
         '[{"file":"补充通知","quote":"基坑深度14.0m"}]', 0, 0.7, 1,
         "基坑深度", 37, "m"))
    await db.commit()

    await gf.update_fact(
        "g3", gf.FactGroupUpdate(id="g3", title="技术参数",
                                 content="- **基坑深度**: 12.5 m\n"
                                         "- **基坑深度**: 14.0 m"),
        scheme_id=sid, db=db)
    out = [dict(r) for r in await (await db.execute(
        "SELECT content, page_ref FROM global_facts "
        "WHERE group_id='g3' ORDER BY page_ref")).fetchall()]
    assert len(out) == 2, f"两条同名事实重建后应为两行，实际 {len(out)} 行"
    assert [o["page_ref"] for o in out] == [12, 37], \
        "两条同名事实的页码溯源必须各自保留（12 与 37）"


# ---------------------------------------------------------------------------
# B. 改分类必须重派生九大章节归属
# ---------------------------------------------------------------------------

async def _first_id_of_group(db, gid):
    cur = await db.execute("SELECT id FROM global_facts WHERE group_id=?", (gid,))
    return (await cur.fetchone())["id"]


async def test_item_update_category_change_rederives_chapter(ctx):
    """改 category 后 chapter 必须随之重派生（旧实现永远停在旧章节）。"""
    db, pid, sid = ctx
    await _seed_fully_populated_fact(
        db, pid, sid, gid="g4", chapter="overview", name="合同工期",
        value="365", category="schedule")
    fid = await _first_id_of_group(db, "g4")

    n, _ = await gf._apply_item_updates(
        db, [{"fact_id": fid, "category": "personnel"}])
    assert n == 1
    r = await _row(db, fid)
    assert r["category"] == "personnel"
    # 与派生规则单一出口同口径（chapter 由文本关键词优先，故不硬编码具体
    # 章节名，只断言「已按新分类重派生，且与规则一致」）。
    expect = fc.classify_chapter_from_text("合同工期", "365", "personnel", "")
    assert r["chapter"] == expect, (
        f"chapter 应为规则派生值 {expect!r}，实际 {r['chapter']!r}")
    assert r["chapter"] != "overview", (
        f"category 已改为 personnel，chapter 却停在旧值 {r['chapter']!r}："
        "改分类后九大章节归属过期，与分类分组口径分叉")


async def test_item_update_category_change_rederives_fact_attr(ctx):
    """改 category 后 fact_attr 同步重派生（与 chapter 同一根源）。"""
    db, pid, sid = ctx
    # 故意写入与规则相矛盾的 fact_attr，才能证明「真的重派生」而非巧合
    await _seed_fully_populated_fact(
        db, pid, sid, gid="g5", chapter="overview", name="合同工期",
        value="365", category="schedule", fact_attr="norm")
    fid = await _first_id_of_group(db, "g5")

    await gf._apply_item_updates(db, [{"fact_id": fid, "category": "basis"}])
    r = await _row(db, fid)
    # 第七轮（2026-09-30）：classify_fact_attr 删除了从未被读取的 category 死参数，
    # fact_attr 只由 name/value 文本决定，与 category 正交（见 §4.12 第 1 条）。
    expect = fc.classify_fact_attr("合同工期", "365")
    assert r["fact_attr"] == expect, (
        f"fact_attr 应为规则派生值 {expect!r}，实际 {r['fact_attr']!r}")
    assert r["fact_attr"] != "norm", (
        "fact_attr 仍停在明显错误的旧值，说明改分类未触发重派生")


async def test_item_update_chapter_parity_with_list_facts(ctx):
    """改分类后，注入路径与列表路径的 chapter 必须一致（跨侧 parity）。"""
    db, pid, sid = ctx
    await _seed_fully_populated_fact(
        db, pid, sid, gid="g6", chapter="overview", name="合同工期",
        value="365", category="schedule", is_stale=0)
    fid = await _first_id_of_group(db, "g6")
    await gf._apply_item_updates(
        db, [{"fact_id": fid, "category": "safety_critical"}])

    listed = await gf.list_facts(scheme_id=sid, project_id="", db=db)
    item = listed["groups"][0]["items"][0]
    inject_rows = await sh._load_facts_rows(db, sid)
    assert len(inject_rows) == 1
    assert item["chapter"] == inject_rows[0][4], (
        f"list_facts chapter={item['chapter']!r} 与注入路径 "
        f"{inject_rows[0][4]!r} 分叉")


# ---------------------------------------------------------------------------
# C. 注入路径必须与列表路径共享「历史/未标注行惰性派生」口径
# ---------------------------------------------------------------------------

async def test_manual_fact_chapter_derived_on_inject_path(ctx):
    """手工新增的事实（chapter 列未写）在注入路径也必须能按章命中。

    旧实现只读 chapter 原始列 → 手工/编辑后事实恒为 '' →
    facts_chapter_inject 的「本章事实前置」对这批事实完全失效。
    """
    db, pid, sid = ctx
    body = gf.FactGroupIn(
        title="工期安排", content="", category="schedule",
        items=[FactItem(name="合同工期", value="365", category="schedule")])
    await gf.create_fact(body, scheme_id=sid, project_id="", db=db)

    rows = await sh._load_facts_rows(db, sid)
    assert len(rows) == 1
    assert rows[0][4] == "plan", (
        f"手工新增的 schedule 类事实 chapter 应为 plan（惰性派生），"
        f"实际 {rows[0][4]!r}：注入路径缺惰性派生兜底")


async def test_render_facts_text_chapter_priority_works_for_manual_facts(ctx):
    """端到端：手工新增事实 + 章节归属 → 本章事实必须排在前面。"""
    db, pid, sid = ctx
    await gf.create_fact(
        gf.FactGroupIn(
            title="工期安排", content="", category="schedule",
            items=[FactItem(name="合同工期", value="365",
                               category="schedule")]),
        scheme_id=sid, project_id="", db=db)
    await gf.create_fact(
        gf.FactGroupIn(
            title="安全措施", content="", category="safety_critical",
            items=[FactItem(name="监测频率", value="每日一次",
                               category="safety_critical")]),
        scheme_id=sid, project_id="", db=db)

    rows = await sh._load_facts_rows(db, sid)
    text = sh._render_facts_text(rows, max_total=6000, per_fact=300,
                                 chapter="plan")
    pos_plan = text.find("合同工期")
    pos_other = text.find("监测频率")
    assert pos_plan != -1 and pos_other != -1
    assert pos_plan < pos_other, (
        "本章（plan）事实必须排在其余事实之前；"
        f"实际 plan@{pos_plan} other@{pos_other}")


def test_render_facts_text_never_drops_facts_when_chapter_filter_misses():
    """章节前置不得丢事实：无命中时全部事实原序保留。"""
    rows = [
        ("安全", "监测频率", "- **监测频率**: 每日一次", 0.9, "safety"),
        ("工期", "合同工期", "- **合同工期**: 365 天", 0.9, "plan"),
    ]
    text = sh._render_facts_text(rows, max_total=6000, per_fact=300,
                                 chapter="emergency")
    assert "监测频率" in text and "合同工期" in text, "章节无命中也不得丢事实"
    assert text.index("监测频率") < text.index("合同工期"), "无命中时保持原序"


async def test_load_facts_rows_keeps_five_column_row_contract(ctx):
    """注入行必须保持 5 元组（gt, title, content, confidence, chapter）。

    _filter_facts_rows / _row_chapter 依赖该长度自适应口径；
    任何新增列都必须追加在末尾，不得插入中部。
    """
    db, pid, sid = ctx
    await _seed_fully_populated_fact(db, pid, sid, gid="g7", is_stale=0)
    rows = await sh._load_facts_rows(db, sid)
    assert rows and all(len(r) == 5 for r in rows)
    assert sh._row_chapter(rows[0]) == "overview"


# ---------------------------------------------------------------------------
# E. 新建事实必须失效导出缓存（项目级事实此前完全漏失效）
# ---------------------------------------------------------------------------

async def test_create_project_level_fact_invalidates_all_scheme_caches(ctx):
    """项目级（scheme_id 为空）新事实必须失效项目下全部方案的导出缓存。

    旧实现只在 scheme_scope 非空时失效**单个**方案缓存 —— 项目级事实
    （供项目下全部方案共用）落库后一个缓存都不失效，用户新建「合同工期」
    后导出仍复用旧产物，产物缺该事实却查不出原因。
    """
    db, pid, sid = ctx
    sid2 = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid2, pid, "s2"))
    await db.execute(
        "INSERT INTO export_cache(id, scheme_id, result_path) VALUES(?,?,?)",
        (uuid.uuid4().hex, sid, "x1"))
    await db.execute(
        "INSERT INTO export_cache(id, scheme_id, result_path) VALUES(?,?,?)",
        (uuid.uuid4().hex, sid2, "x2"))
    await db.commit()

    await gf.create_fact(
        gf.FactGroupIn(
            title="工期安排", content="", category="schedule",
            items=[FactItem(name="合同工期", value="365", category="schedule")]),
        scheme_id="", project_id=pid, db=db)

    cur = await db.execute(
        "SELECT COUNT(*) FROM export_cache WHERE scheme_id IN (?,?)", (sid, sid2))
    left = (await cur.fetchone())[0]
    assert left == 0, (
        f"新建项目级事实后仍有 {left} 条导出缓存未失效 —— 导出会继续复用旧产物")


async def test_create_scheme_scoped_fact_invalidates_that_scheme(ctx):
    """方案级新事实仍须失效该方案缓存（回归：口径统一后不得漏失效）。"""
    db, pid, sid = ctx
    await db.execute(
        "INSERT INTO export_cache(id, scheme_id) VALUES(?,?)",
        (uuid.uuid4().hex, sid))
    await db.commit()
    await gf.create_fact(
        gf.FactGroupIn(
            title="安全措施", content="", category="safety_critical",
            items=[FactItem(name="监测频率", value="每日一次",
                            category="safety_critical")]),
        scheme_id=sid, project_id="", db=db)
    cur = await db.execute(
        "SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())[0] == 0, "方案级新事实未失效该方案导出缓存"


def test_fact_write_paths_use_scope_invalidation_helper():
    """源码护栏：事实写路径不得裸调 invalidate_export_cache。

    裸调只失效「单个方案」，项目级事实（scheme_id 为空、供项目下全部方案
    共用）落库后一个缓存都不失效 → 导出继续复用旧产物，而用户无从得知。
    口径必须收敛到 _invalidate_fact_scope_cache（scheme 非空失效单方案，
    为空则按项目覆盖全部方案）。本模块历史上 create_fact / update_fact 两处
    行级分支 / adjust_facts 共 4 处曾漏失效，改一处必漏一处。
    """
    import inspect

    lines = inspect.getsource(gf).splitlines()

    # 跳过所有「缓存失效 helper」自身实现（它们内部调 invalidate_export_cache
    # 是正确用法）：函数名含 _invalidate 的 def 起始行到下一个顶层 def 之间。
    skip_ranges: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        head = line.rstrip()
        if (head.startswith("async def _invalidate") or head.startswith("def _invalidate")):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith(("async def ", "def ", "@router.")):
                    end = j
                    break
            skip_ranges.append((i, end))

    def _in_skip(i: int) -> bool:
        return any(a <= i < b for a, b in skip_ranges)

    offenders = []
    for i, line in enumerate(lines):
        if "invalidate_export_cache" not in line:
            continue
        if _in_skip(i):
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "_invalidate_fact_scope_cache" in stripped:
            continue
        # import 语句
        if stripped.startswith("from app.services.facts_extractor import") or \
                stripped.startswith("invalidate_export_cache,"):
            continue
        # 裸调必须与「项目级兜底」分支成对出现（if/else 或紧邻），
        # 否则漏失效项目级事实 —— 前后各查几行覆盖两种排版
        tail = "\n".join(lines[max(0, i - 3):i + 6])
        if "_invalidate_fact_scope_cache" not in tail:
            offenders.append(f"L{i + 1}: {stripped}")
    assert not offenders, (
        "事实写路径不得裸调 invalidate_export_cache（只失效单方案，项目级事实会"
        "漏失效导出缓存），请改走 _invalidate_fact_scope_cache 或紧跟其兜底分支：\n"
        + "\n".join(offenders))


# ---------------------------------------------------------------------------
# D. 查询口径收敛：_load_facts_rows 必须复用 build_injectable_facts_query
# ---------------------------------------------------------------------------

async def test_load_facts_rows_accepts_positional_row_shape(ctx):
    """注入路径必须兼容「位置序列」行（mock / 历史调用方）。

    db.execute 的行对象在真实链路是 sqlite3.Row（可按键取值）；但测试 mock 与
    部分历史调用方返回的是位置序列 (gt, title, content[, ...])。若实现只按
    dict(r) 解包，这类输入会直接抛 TypeError 并被吞成「无事实」—— 表现为
    「明明有事实却不注入正文」，且无任何日志（降级为 no-op）。
    """
    class _SeqCursor:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

    class _SeqDb:
        async def execute(self, sql, params=None):
            return _SeqCursor([
                ("项目概况", "工程名称", "某某产业园项目"),
                ("基坑支护", "开挖深度", "8.6m", 0.9, "overview"),
            ])

    rows = await sh._load_facts_rows(_SeqDb(), "scheme-x")
    assert len(rows) == 2, f"位置序列行应被正确解析，实际解析出 {len(rows)} 行"
    # 位置序列原样透传（既有行契约），_row_chapter 按长度自适应取 chapter
    assert rows[0] == ("项目概况", "工程名称", "某某产业园项目")
    assert rows[1][4] == "overview"
    assert sh._row_chapter(rows[0]) == ""
    assert sh._row_chapter(rows[1]) == "overview"


def test_load_facts_rows_shares_inject_query_builder():
    """源码护栏：_load_facts_rows 不得自维护 WHERE/ORDER BY。

    build_injectable_facts_query 的 docstring 声称「与 sse_handlers
    _load_facts_rows 共用同一段 SQL」，但旧实现自行拼装 WHERE/ORDER BY，
    声明与实现不符 → 改门控一处必漏一处。
    """
    import inspect
    src = inspect.getsource(sh._load_facts_rows)
    assert "build_injectable_facts_query" in src, (
        "_load_facts_rows 必须复用 build_injectable_facts_query 单一出口，"
        "不得自维护 WHERE/ORDER BY（否则与导出侧口径分叉）")
    assert "ORDER BY gt, title" not in src, (
        "_load_facts_rows 不得自带 ORDER BY，排序口径必须来自 helper")


async def test_load_facts_rows_excludes_stale_and_simulated(ctx):
    """注入门控 fail-closed：模拟值与过期值不得进入注入路径。"""
    db, pid, sid = ctx

    async def _add(fid, title, val, sim, resolved, stale):
        await db.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
            "group_title, title, content, category, is_simulated, confidence, "
            "is_resolved, chapter, is_stale) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, pid, sid, "g8", "技术参数", title,
             gf._build_fact_content(title, val, bool(sim)), "tech_param",
             sim, 0.9, resolved, "overview", stale))

    await _add(uuid.uuid4().hex, "基坑深度", "12.5 m", 0, 1, 0)
    await _add(uuid.uuid4().hex, "模拟值", "99", 1, 0, 0)
    await _add(uuid.uuid4().hex, "过期值", "88", 0, 1, 1)
    await db.commit()

    rows = await sh._load_facts_rows(db, sid)
    titles = [r[1] for r in rows]
    assert titles == ["基坑深度"], (
        f"注入路径必须剔除模拟值与过期值，实际得到 {titles}")



