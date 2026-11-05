"""R93（2026-10-17）护栏：编制依据「逐条提取 · 多值并存」契约（原拟 R91，同日撞号让号）

病灶回顾：P14 令所有规范同命名「执行标准」→ normalize_key 折叠同一 fact_key →
merge_and_deduplicate 把 N 部规范折成「1 条事实 + N-1 个矛盾候选」——编制依据
只剩一部规范可见，其余被伪装成需人工裁决的矛盾，且整行因 has_conflict=1
失去注入门资格。

口径：并存簇（BASIS_COEXIST 判据单一事实源）内每条唯一取值独立成条，
fact_key 追加「::值域标签」；历史裸键已确认行按 base-key 回查比值兼容。

覆盖：
- A 组合并分簇：同名多值逐条独立 / 同值跨段收敛 / 非并存键原路径不变 /
  未注册名靠 category 兜 / scoped 键幂等 / basis_scoped_key 与 base 往返
- B 键归一与章节派生：标准图集/法规依据别名、scoped 键仍判 basis、白名单锁
- C persist 真实 DB：旧裸键已确认行同值跳过并解除 stale / 异值独立插入且不
  回写 conflict_keys（无假矛盾）/ 五部规范端到端全落库
- D 静态契约：P14 提示词逐条纪律、basis_zone 关键词、别名表、自动分类器
"""

import re
import uuid

import app.db as _appdb
import app.services.ai.prompts.analysis as _analysis_prompts  # noqa: F401 确保提示词注册
import pytest
from app.db import get_conn, init_db
from app.services import facts_extractor as fe
from app.services.ai.prompts._registry import get_prompt
from app.services.facts_classification import classify_chapter_from_text
from app.services.facts_extractor import (
    BASIS_COEXIST_KEYS,
    ExtractionResult,
    FactGroup,
    FactItem,
    basis_base_key,
    basis_scoped_key,
    is_basis_coexist_item,
    is_basis_coexist_key,
    merge_and_deduplicate,
    normalize_key,
    persist_extraction,
)

STANDARDS = [
    "GB 50300-2013《建筑工程施工质量验收统一标准》",
    "GB 50026-2020《工程测量标准》",
    "JGJ 120-2012《建筑基坑支护技术规程》",
    "GB 55002-2021《建筑与市政工程抗震通用规范》",
    "GB 50497-2019《建筑基坑工程监测技术标准》",
]

_SCOPED_RE = re.compile(r"^execution_standard::[0-9a-f]{8}$")


def _basis(name: str, value: str, confidence: float = 0.9) -> FactItem:
    """按 P14 产出形态构造并存类事实（fact_type=basis → category=basis）。"""
    return FactItem(
        name=name,
        value=value,
        key=normalize_key(name),
        category="basis",
        fact_type="basis",
        confidence=confidence,
    )


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _insert_confirmed_legacy(db, pid, sid, *, fact_key, name, value):
    """模拟改判前用户已确认的「裸键」历史行（is_stale=1 便于验证解除）。"""
    await db.execute(
        "INSERT INTO global_facts(id,project_id,scheme_id,group_id,title,"
        "content,category,fact_key,is_resolved,has_conflict,is_stale) "
        "VALUES(?,?,?,?,?,?, 'basis',?,1,0,1)",
        (
            uuid.uuid4().hex,
            pid,
            sid,
            uuid.uuid4().hex,
            name,
            f"- **{name}**: {value}",
            fact_key,
        ),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# A 组：merge 并存分簇
# ---------------------------------------------------------------------------
class TestMergeBasisCoexist:
    def test_five_standards_each_itemized(self):
        """五部同名「执行标准」不同值 → 五条独立事实，零矛盾标记。"""
        out = merge_and_deduplicate([_basis("执行标准", v) for v in STANDARDS])
        assert len(out) == 5
        assert all(not o.has_conflict for o in out)
        assert all(not o.conflict_values for o in out)
        assert all(_SCOPED_RE.match(o.key) for o in out)
        assert len({o.key for o in out}) == 5
        assert {o.value for o in out} == set(STANDARDS)

    def test_same_standard_across_segments_converges(self):
        """同一部规范跨段重复（空白/全角差异）→ 收敛 1 条，仍独立成条。"""
        out = merge_and_deduplicate(
            [
                _basis("执行标准", "GB 50300-2013《建筑工程施工质量验收统一标准》"),
                _basis("执行标准", "GB50300-2013《建筑工程施工质量验收统一标准》"),
                _basis("执行标准", "GB 50300-2013《建筑工程施工质量验收统一标准》 "),
            ]
        )
        assert len(out) == 1
        assert not out[0].has_conflict
        assert _SCOPED_RE.match(out[0].key)

    def test_non_basis_same_key_still_conflict_path(self):
        """不变式：非并存键同键多值仍走「best + 矛盾候选」原路径。"""
        k = normalize_key("基坑深度")
        assert not is_basis_coexist_key(k)
        out = merge_and_deduplicate(
            [
                FactItem(name="基坑深度", value=v, key=k, category="tech_param", confidence=0.9)
                for v in ("12.5m", "13.0m", "12.0m")
            ]
        )
        assert len(out) == 1
        assert out[0].has_conflict
        assert len(out[0].conflict_values) == 2
        assert out[0].key == k  # 非并存键不加 :: 后缀

    def test_unregistered_basis_name_splits_via_category(self):
        """未注册名（设计文件→兜底哈希键）靠 category=basis 兜住拆分。"""
        out = merge_and_deduplicate(
            [
                _basis("设计文件", "XX设计院施工图（建-01）"),
                _basis("设计文件", "XX项目岩土工程勘察报告"),
            ]
        )
        assert len(out) == 2
        assert all(not o.has_conflict for o in out)
        assert out[0].key != out[1].key
        assert "::" in out[0].key

    def test_key_whitelist_catches_missing_fact_type(self):
        """AI 漏标 fact_type（category=other）时键白名单仍兜住「执行标准」。"""
        it = FactItem(name="执行标准", value=STANDARDS[0], key=normalize_key("执行标准"), category="other")
        assert is_basis_coexist_item(it)
        out = merge_and_deduplicate([it])
        assert _SCOPED_RE.match(out[0].key)

    def test_scoped_key_idempotent_under_remerge(self):
        """二次 merge 不叠双后缀，scoped 键逐字稳定（幂等）。"""
        items = [_basis("执行标准", v) for v in STANDARDS[:2]]
        out1 = merge_and_deduplicate(items)
        out2 = merge_and_deduplicate(out1)
        assert len(out2) == 2
        assert {o.key for o in out2} == {o.key for o in out1}
        assert all(o.key.count("::") == 1 for o in out2)

    def test_scoped_key_deterministic_and_base_roundtrip(self):
        """值域标签确定性 + base 往返 + 含后缀键仍判并存。"""
        k1 = basis_scoped_key("execution_standard", STANDARDS[0])
        k2 = basis_scoped_key("execution_standard", STANDARDS[0])
        assert k1 == k2
        assert basis_base_key(k1) == "execution_standard"
        assert is_basis_coexist_key(k1)
        assert not is_basis_coexist_key("execution_standard" + "::zzzzzzzz")
        # 无后缀键原样返回；异值必得异标签
        assert basis_base_key("project_name") == "project_name"
        assert basis_scoped_key("execution_standard", STANDARDS[0]) != basis_scoped_key(
            "execution_standard", STANDARDS[1]
        )


# ---------------------------------------------------------------------------
# B 组：键归一与章节派生
# ---------------------------------------------------------------------------
class TestKeyNormalization:
    def test_new_aliases_registered(self):
        assert normalize_key("标准图集") == "standard_atlas"
        assert normalize_key("法规依据") == "legal_basis"

    def test_basis_keys_all_in_coexist_whitelist(self):
        """新增别名必须同步进并存白名单（否则折叠病灶复发）。"""
        for k in (
            "compilation_basis",
            "execution_standard",
            "tech_standard",
            "acceptance_code",
            "standard_atlas",
            "legal_basis",
        ):
            assert k in BASIS_COEXIST_KEYS

    def test_coexist_whitelist_exact_lock(self):
        assert set(BASIS_COEXIST_KEYS) == {
            "compilation_basis",
            "execution_standard",
            "tech_standard",
            "acceptance_code",
            "standard_atlas",
            "legal_basis",
        }

    def test_scoped_key_chapter_parity_with_bare_key(self):
        """scoped 键与裸键的九大章节派生**逐字一致**（不改判据只加后缀）。

        注：CHAPTER_TEXT_RULES 优先于 fact_key 表 —— 规范名含「验收」等词时
        文本规则先命中（改判前后同行为），本锁只验「::后缀不引入新偏差」。
        """
        for name, value in (
            ("执行标准", STANDARDS[0]),
            ("执行标准", STANDARDS[2]),
            ("标准图集", "16G101-1《混凝土结构施工图平面整体表示方法制图规则和构造详图》"),
            ("法规依据", "《危险性较大的分部分项工程安全管理规定》（住建部令第37号）"),
        ):
            base = normalize_key(name)
            scoped = basis_scoped_key(base, value)
            assert classify_chapter_from_text(name, value, "basis", "basis", scoped) == classify_chapter_from_text(
                name, value, "basis", "basis", base
            ), name

    def test_basis_fact_type_without验收_word_is_basis_chapter(self):
        """值不含他章关键词时，并存条目仍归「编制依据」章（scoped 键可用）。"""
        scoped = basis_scoped_key("execution_standard", STANDARDS[2])
        ch = classify_chapter_from_text(
            name="执行标准", value=STANDARDS[2], category="basis", fact_type="basis", fact_key=scoped
        )
        assert ch == "basis"


# ---------------------------------------------------------------------------
# C 组：persist 真实 DB 兼容（历史裸键已确认行）
# ---------------------------------------------------------------------------
class TestPersistLegacyCompat:
    async def test_legacy_same_value_skipped_and_unstaled(self, ctx):
        """旧裸键已确认行 + 新 scoped 同值 → 不重复插入，is_stale 解除。"""
        db, pid, sid = ctx
        val = STANDARDS[0]
        await _insert_confirmed_legacy(db, pid, sid, fact_key="execution_standard", name="执行标准", value=val)

        it = _basis("执行标准", val)
        it.key = basis_scoped_key("execution_standard", val)
        await persist_extraction(
            ExtractionResult(groups=[FactGroup(title="编制依据", category="basis", items=[it])], total_items=1),
            db,
            pid,
            sid,
        )

        cur = await db.execute(
            "SELECT fact_key,is_stale,has_conflict,conflict_keys " "FROM global_facts WHERE scheme_id=?", (sid,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1, "同值并存条目绝不重复插入"
        assert rows[0]["fact_key"] == "execution_standard"
        assert rows[0]["is_stale"] == 0, "命中重提取应解除过期标记"
        assert rows[0]["has_conflict"] == 0
        assert (rows[0]["conflict_keys"] or "") == ""

    async def test_legacy_diff_value_coexists_no_fake_conflict(self, ctx):
        """旧裸键已确认行 + 新 scoped 异值 → 独立插入，绝不回写矛盾。"""
        db, pid, sid = ctx
        await _insert_confirmed_legacy(db, pid, sid, fact_key="execution_standard", name="执行标准", value=STANDARDS[0])

        it = _basis("执行标准", STANDARDS[2])
        it.key = basis_scoped_key("execution_standard", STANDARDS[2])
        await persist_extraction(
            ExtractionResult(groups=[FactGroup(title="编制依据", category="basis", items=[it])], total_items=1),
            db,
            pid,
            sid,
        )

        cur = await db.execute(
            "SELECT fact_key,content,has_conflict,conflict_keys "
            "FROM global_facts WHERE scheme_id=? ORDER BY fact_key",
            (sid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 2, "异值规范必须并存为两条独立事实"
        legacy = next(r for r in rows if r["fact_key"] == "execution_standard")
        assert legacy["has_conflict"] == 0
        assert (legacy["conflict_keys"] or "") == "", "并存异值绝不回写已确认行的矛盾候选"
        assert STANDARDS[0] in legacy["content"]

    async def test_legacy_notation_variant_treated_as_same_value(self, ctx):
        """记法变体（GB 50300 / GB50300）按既有同值口径收敛，不并列重复。"""
        db, pid, sid = ctx
        await _insert_confirmed_legacy(
            db,
            pid,
            sid,
            fact_key="execution_standard",
            name="执行标准",
            value="GB 50300-2013《建筑工程施工质量验收统一标准》",
        )

        it = _basis("执行标准", "GB50300-2013《建筑工程施工质量验收统一标准》")
        it.key = basis_scoped_key("execution_standard", it.value)
        await persist_extraction(
            ExtractionResult(groups=[FactGroup(title="编制依据", category="basis", items=[it])], total_items=1),
            db,
            pid,
            sid,
        )

        cur = await db.execute("SELECT id FROM global_facts WHERE scheme_id=?", (sid,))
        assert len(await cur.fetchall()) == 1

    async def test_five_standards_end_to_end_all_land(self, ctx):
        """五部规范过 merge 后 persist → 五行独立落库，全行零矛盾。"""
        db, pid, sid = ctx
        merged = merge_and_deduplicate([_basis("执行标准", v) for v in STANDARDS])
        assert len(merged) == 5
        await persist_extraction(
            ExtractionResult(groups=[FactGroup(title="编制依据", category="basis", items=merged)], total_items=5),
            db,
            pid,
            sid,
        )

        cur = await db.execute(
            "SELECT fact_key,has_conflict,conflict_keys,content " "FROM global_facts WHERE scheme_id=?", (sid,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 5
        assert all(r["has_conflict"] == 0 for r in rows)
        assert all((r["conflict_keys"] or "") == "" for r in rows)
        assert len({r["fact_key"] for r in rows}) == 5
        for std in STANDARDS:
            assert any(std in r["content"] for r in rows), f"{std} 丢失"


# ---------------------------------------------------------------------------
# D 组：提示词与语义区静态契约
# ---------------------------------------------------------------------------
class TestPromptAndZoneContract:
    def test_p14_requires_itemize(self):
        p = get_prompt("facts_extract_system")
        for kw in ("逐条提取", "一条不落", "同名多条不是矛盾", "标准图集", "法规依据", "勘察报告", "禁止用"):
            assert kw in p, f"P14 缺关键纪律/成员：{kw}"

    def test_basis_zone_keywords_cover_atlas_forms(self):
        kws = fe._ZONE_KEYWORDS["basis_zone"]
        for kw in ("图集", "标准图集", "规范清单", "引用标准", "法规依据"):
            assert kw in kws

    def test_name_to_key_new_aliases(self):
        assert fe._NAME_TO_KEY["标准图集"] == "standard_atlas"
        assert fe._NAME_TO_KEY["法规依据"] == "legal_basis"

    def test_auto_classify_basis_rule_covers_atlas(self):
        """自动分类器 basis 规则须兜住图集/法规依据（category 兜底判据）。"""
        items = [
            FactItem(name="标准图集", value="16G101-1", key="standard_atlas"),
            FactItem(name="法规依据", value="住建部令第37号", key="legal_basis"),
        ]
        fe.apply_category_auto_classify(items)
        assert all(it.category == "basis" for it in items)
