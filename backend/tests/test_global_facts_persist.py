"""全局事实：分类映射 + 持久化防数据丢失 回归测试

覆盖：
- fact_type → category 映射正确性（地质/技术参数事实不再误归「设备配置」）
- persist_extraction 重新提取时，未覆盖分类的未确认事实应保留（防静默数据丢失）
- persist_extraction 重新提取时，覆盖分类的未确认事实被刷新、已确认/手动事实保留
"""
import asyncio
import uuid

import app.db as _appdb
import app.services.facts_extractor as fe
import pytest
from app.db import get_conn, init_db
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    persist_extraction,
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


# ---------------------------------------------------------------------------
# 分类映射
# ---------------------------------------------------------------------------
class TestCategoryMapping:
    def test_tech_param_maps_to_tech_param_not_equipment(self):
        # 地质/技术参数类事实类型应归入「技术参数」类别，而非「主要设备配置」
        assert fe._FACT_TYPE_TO_CATEGORY["tech_param"] == "tech_param"
        assert fe._FACT_TYPE_TO_CATEGORY["design_param"] == "tech_param"
        assert fe._FACT_TYPE_TO_CATEGORY["equipment"] == "tech_param"

    def test_tech_param_title_exists(self):
        # 反查表应能提供 fact_type，标题表应包含「技术参数」
        assert fe.CATEGORY_TO_FACT_TYPE.get("tech_param") == "tech_param"
        assert fe.CATEGORY_TITLES["tech_param"] == "技术参数"

    def test_actual_fact_classifies_correctly(self):
        # 模拟从文档提取的基坑深度（fact_type=tech_param）
        it = fe._parse_fact_dict({
            "name": "基坑深度", "value": "12.5m",
            "fact_type": "tech_param", "source_text": "基坑深度12.5m",
            "confidence": 0.9,
        })
        assert it is not None
        assert it.category == "tech_param", "地质参数应归入技术参数类别"


# ---------------------------------------------------------------------------
# 持久化防数据丢失
# ---------------------------------------------------------------------------
class TestPersistNoSilentLoss:
    async def test_reextract_preserves_other_categories(self, ctx):
        db, pid, sid = ctx
        # 第 1 次：人员 + 机械（均未确认）
        await persist_extraction(ExtractionResult(groups=[
            FactGroup(title="人员角色", category="personnel", items=[
                FactItem(name="项目经理", value="张伟", key="project_manager",
                         category="personnel", confidence=0.9)]),
            FactGroup(title="机械统计", category="machinery", items=[
                FactItem(name="塔吊型号", value="QTZ80", key="tower_crane_model",
                         category="machinery", confidence=0.9)]),
        ], total_items=2), db, pid, sid)

        # 第 2 次：仅重新提取「人员」分类（模拟 AI 本轮只产出人员相关段落）
        await persist_extraction(ExtractionResult(groups=[
            FactGroup(title="人员角色", category="personnel", items=[
                FactItem(name="安全员", value="李雷", key="safety_officer",
                         category="personnel", confidence=0.9)]),
        ], total_items=1), db, pid, sid)

        rows = await _rows_async(db, sid)
        titles = {r["title"] for r in rows}
        cats = {r["category"] for r in rows}
        # ✅ 修复前：机械统计（塔吊型号）会被静默删除；
        #    修复后：未被本次结果覆盖的分类（machinery）的未确认事实应保留
        assert "塔吊型号" in titles, "未覆盖分类的未确认事实不应被删除"
        assert "machinery" in cats
        assert "项目经理" not in titles, "被覆盖分类的未确认旧事实应被刷新删除"
        assert "安全员" in titles

    async def test_reextract_keeps_resolved_and_manual(self, ctx):
        db, pid, sid = ctx
        await persist_extraction(ExtractionResult(groups=[
            FactGroup(title="人员角色", category="personnel", items=[
                FactItem(name="项目经理", value="张伟", key="project_manager",
                         category="personnel", confidence=0.9)]),
        ], total_items=1), db, pid, sid)
        # 用户确认项目经理
        await db.execute(
            "UPDATE global_facts SET is_resolved=1 WHERE title='项目经理'")
        # 模拟手动新增（来源标记 manual）
        await db.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
            "group_title, title, content, category, source_ref, is_simulated, "
            "confidence, is_resolved, has_conflict, conflict_keys, fact_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, pid, sid, "g_manual", "其他事实", "联系人",
             "- **联系人**: 王工", "other",
             '[{"file":"手动录入","quote":""}]', 0, 1.0, 1, 0, "", "contact"))
        await db.commit()

        # 重新提取人员分类
        await persist_extraction(ExtractionResult(groups=[
            FactGroup(title="人员角色", category="personnel", items=[
                FactItem(name="安全员", value="李雷", key="safety_officer",
                         category="personnel", confidence=0.9)]),
        ], total_items=1), db, pid, sid)

        rows = await _rows_async(db, sid)
        titles = {r["title"] for r in rows}
        assert "项目经理" in titles, "已确认事实应保留"
        assert "联系人" in titles, "手动新增事实应保留"
        assert "安全员" in titles, "新提取事实应追加"


async def _rows_async(db, sid):
    cur = await db.execute(
        "SELECT title, category, group_id, is_resolved FROM global_facts WHERE scheme_id=?",
        (sid,))
    return [dict(r) for r in await cur.fetchall()]
