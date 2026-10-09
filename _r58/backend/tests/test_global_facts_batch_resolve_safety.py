"""批量确认安全约束回归测试

F1 遗留缺口修复：batch_resolve 不得以「用户显式确认」为由越过模拟值闸门
与安全关键约束。本文件锁定以下不变式：

- is_simulated=1 的事实被批量确认跳过（is_resolved 仍为 0）
- is_safety_critical 白名单命中的事实被批量确认跳过
- 普通真实事实正常确认（is_resolved=1，计入 changed）
- 混合批次中计数守恒：changed + skipped + skipped_safety_count == 总数
- 响应体含 skipped_safety_count / safety_blocked 且与明细长度一致
"""
import uuid

import pytest
from app.routers.global_facts import batch_resolve


async def _seed(db, fact_id, *, title, content, is_simulated=0,
                is_resolved=0, fact_key="", category="basic"):
    """插入一条全局事实，返回行 id。"""
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, group_title, "
        "title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fact_id, "p1", "s1", "g1", "分组",
         title, content, category, "", is_simulated,
         0.9, is_resolved, 0, "", fact_key))
    await db.commit()
    return fact_id


async def _init_schema(db_conn):
    """确保 projects / schemes 存在。"""
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
        ("p1", "项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "方案"))
    await db_conn.commit()


# ---------------------------------------------------------------------------
# 测试 1：模拟值事实被批量确认跳过
# ---------------------------------------------------------------------------

async def test_simulated_fact_skipped_by_batch_resolve(db_conn):
    """is_simulated=1 的事实不得被批量确认放行。"""
    await _init_schema(db_conn)
    fid = uuid.uuid4().hex
    await _seed(db_conn, fid, title="基坑深度",
                content="- **基坑深度**: 12.5m ⚠️*(模拟值)*",
                is_simulated=1, fact_key="foundation_depth")

    res = await batch_resolve(
        {"fact_ids": [fid], "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["safety_blocked"] is True
    assert res["skipped_safety_count"] == 1
    assert len(res["skipped_safety"]) == 1

    # 响应体字段一致性
    assert res["skipped_safety"][0]["id"] == fid
    assert "模拟值" in res["skipped_safety"][0]["reason"]

    # DB 状态：is_resolved 仍为 0
    cur = await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    assert row["is_resolved"] == 0, "模拟值事实不得被批量确认放行"


# ---------------------------------------------------------------------------
# 测试 2：安全关键事实被跳过（通过 fact_key 白名单判定）
# ---------------------------------------------------------------------------

async def test_safety_critical_fact_skipped_by_batch_resolve(db_conn):
    """安全关键白名单命中的事实（非模拟值）不得被批量确认放行。"""
    await _init_schema(db_conn)
    fid = uuid.uuid4().hex
    # 使用 fact_key="foundation_depth"（白名单命中），is_simulated=0
    await _seed(db_conn, fid, title="基坑深度",
                content="- **基坑深度**: 8.0m",
                is_simulated=0, fact_key="foundation_depth")

    res = await batch_resolve(
        {"fact_ids": [fid], "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["safety_blocked"] is True
    assert res["skipped_safety_count"] == 1
    assert len(res["skipped_safety"]) == 1

    # 响应体字段一致性
    assert res["skipped_safety"][0]["id"] == fid
    assert "安全关键" in res["skipped_safety"][0]["reason"]

    # DB 状态：is_resolved 仍为 0
    cur = await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    assert row["is_resolved"] == 0, "安全关键事实不得被批量确认放行"


# ---------------------------------------------------------------------------
# 测试 3：普通真实事实正常确认
# ---------------------------------------------------------------------------

async def test_normal_fact_confirmed_by_batch_resolve(db_conn):
    """普通非模拟值、非安全关键事实应正常确认。"""
    await _init_schema(db_conn)
    fid = uuid.uuid4().hex
    # 使用普通名称（不在安全关键白名单），is_simulated=0
    await _seed(db_conn, fid, title="项目经理",
                content="- **项目经理**: 张伟",
                is_simulated=0, fact_key="project_manager")

    res = await batch_resolve(
        {"fact_ids": [fid], "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["safety_blocked"] is False
    assert res["skipped_safety_count"] == 0
    assert res["skipped_safety"] == []
    assert res["changed"] == 1

    # DB 状态：is_resolved=1
    cur = await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    assert row["is_resolved"] == 1, "普通真实事实应正常确认"


# ---------------------------------------------------------------------------
# 测试 4：混合批次计数守恒
# ---------------------------------------------------------------------------

async def test_mixed_batch_count_conservation(db_conn):
    """混合批次中计数守恒：changed + skipped + skipped_safety_count == 总数。

    场景：4 条事实
    - fact-1：普通真实事实 → 应确认（changed +1）
    - fact-2：模拟值 → 跳过（skipped_safety +1）
    - fact-3：安全关键（非模拟值）→ 跳过（skipped_safety +1）
    - fact-4：已确认事实（is_resolved=1）→ 跳过（skipped +1）
    """
    await _init_schema(db_conn)

    fid1 = uuid.uuid4().hex
    fid2 = uuid.uuid4().hex
    fid3 = uuid.uuid4().hex
    fid4 = uuid.uuid4().hex

    # fact-1: 普通真实事实
    await _seed(db_conn, fid1, title="项目经理",
                content="- **项目经理**: 张伟",
                is_simulated=0, fact_key="project_manager")
    # fact-2: 模拟值
    await _seed(db_conn, fid2, title="混凝土强度等级",
                content="- **混凝土强度等级**: C30 ⚠️*(模拟值)*",
                is_simulated=1, fact_key="concrete_grade")
    # fact-3: 安全关键（非模拟值，通过 fact_key 白名单命中）
    await _seed(db_conn, fid3, title="塔吊",
                content="- **塔吊**: QTZ80",
                is_simulated=0, fact_key="tower_crane_model")
    # fact-4: 已确认事实
    await _seed(db_conn, fid4, title="项目名称",
                content="- **项目名称**: 示例项目",
                is_simulated=0, is_resolved=1, fact_key="project_name")

    fact_ids = [fid1, fid2, fid3, fid4]
    res = await batch_resolve(
        {"fact_ids": fact_ids, "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True

    # 计数守恒
    total = len(fact_ids)
    changed = res["changed"]
    skipped = res["skipped"]
    skipped_safety = res["skipped_safety_count"]
    assert changed + skipped + skipped_safety == total, (
        f"计数不守恒: changed={changed} + skipped={skipped} + "
        f"skipped_safety={skipped_safety} = {changed + skipped + skipped_safety} "
        f"!= {total}")

    # 具体计数
    assert changed == 1  # fact-1
    assert skipped == 1  # fact-4 (already resolved)
    assert skipped_safety == 2  # fact-2 (simulated) + fact-3 (safety_critical)

    # safety_blocked
    assert res["safety_blocked"] is True

    # DB 状态验证
    cur = await db_conn.execute(
        "SELECT id, is_resolved FROM global_facts WHERE id IN (?,?,?,?) "
        "ORDER BY id", (fid1, fid2, fid3, fid4))
    rows = {r["id"]: r["is_resolved"] for r in await cur.fetchall()}
    assert rows[fid1] == 1  # confirmed
    assert rows[fid2] == 0  # simulated, skipped
    assert rows[fid3] == 0  # safety_critical, skipped
    assert rows[fid4] == 1  # already resolved


# ---------------------------------------------------------------------------
# 测试 5：响应体 skipped_safety_count 与明细长度一致
# ---------------------------------------------------------------------------

async def test_skipped_safety_count_matches_detail_length(db_conn):
    """skipped_safety_count 必须等于 skipped_safety 列表长度。"""
    await _init_schema(db_conn)

    # 插入 3 条模拟值事实
    fids = []
    for i in range(3):
        fid = uuid.uuid4().hex
        fids.append(fid)
        await _seed(db_conn, fid, title=f"测试事实{i}",
                    content=f"- **测试事实{i}**: 值 ⚠️*(模拟值)*",
                    is_simulated=1, fact_key=f"test_key_{i}")

    res = await batch_resolve(
        {"fact_ids": fids, "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True

    # 响应体一致性
    assert res["skipped_safety_count"] == len(res["skipped_safety"])
    assert res["skipped_safety_count"] == 3
    assert res["safety_blocked"] is True
    assert res["changed"] == 0

    # 每条明细都包含必要字段
    for item in res["skipped_safety"]:
        assert "id" in item
        assert "title" in item
        assert "reason" in item
        assert "模拟值" in item["reason"]


# ---------------------------------------------------------------------------
# 测试 6：scheme_id 模式（无 fact_ids）也遵守安全约束
# ---------------------------------------------------------------------------

async def test_scheme_id_mode_also_applies_safety_constraints(db_conn):
    """无 fact_ids、仅传 scheme_id 时，安全约束同样生效。"""
    await _init_schema(db_conn)

    fid_sim = uuid.uuid4().hex
    fid_normal = uuid.uuid4().hex

    # 模拟值事实
    await _seed(db_conn, fid_sim, title="基坑深度",
                content="- **基坑深度**: 5.0m ⚠️*(模拟值)*",
                is_simulated=1, fact_key="foundation_depth")
    # 普通事实
    await _seed(db_conn, fid_normal, title="项目名称",
                content="- **项目名称**: 示例项目",
                is_simulated=0, fact_key="project_name")

    res = await batch_resolve({"scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["safety_blocked"] is True
    assert res["skipped_safety_count"] == 1
    assert res["changed"] == 1

    # DB 状态
    cur = await db_conn.execute(
        "SELECT id, is_resolved FROM global_facts WHERE id IN (?,?)",
        (fid_sim, fid_normal))
    rows = {r["id"]: r["is_resolved"] for r in await cur.fetchall()}
    assert rows[fid_sim] == 0, "模拟值事实不得被批量确认"
    assert rows[fid_normal] == 1, "普通事实应正常确认"


# ---------------------------------------------------------------------------
# 测试 7：批量确认不破坏模拟值闸门不变式
# ---------------------------------------------------------------------------

async def test_batch_resolve_preserves_simulated_gate_invariant(db_conn):
    """批量确认操作后，is_simulated=1 的事实必须保持 is_resolved=0。

    不变式：is_simulated=1 ⟹ is_resolved=0
    """
    await _init_schema(db_conn)
    fid = uuid.uuid4().hex
    await _seed(db_conn, fid, title="支护形式",
                content="- **支护形式**: 排桩 ⚠️*(模拟值)*",
                is_simulated=1, fact_key="support_type")

    await batch_resolve({"fact_ids": [fid], "scheme_id": "s1"}, db_conn)

    cur = await db_conn.execute(
        "SELECT is_simulated, is_resolved FROM global_facts WHERE id=?",
        (fid,))
    row = await cur.fetchone()
    assert row["is_simulated"] == 1
    assert row["is_resolved"] == 0, (
        "模拟值闸门不变式被破坏：is_simulated=1 但 is_resolved=1")


# ---------------------------------------------------------------------------
# 测试 8：空 fact_ids 边界情况
# ---------------------------------------------------------------------------

async def test_empty_fact_ids_returns_safe_response(db_conn):
    """fact_ids 全为空字符串时返回安全响应。"""
    await _init_schema(db_conn)

    res = await batch_resolve(
        {"fact_ids": ["", "  ", None], "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["skipped_safety"] == []
    assert res["skipped_safety_count"] == 0
    assert res["safety_blocked"] is False


# ---------------------------------------------------------------------------
# 测试 9：完整混合批次，验证完整守恒
# ---------------------------------------------------------------------------

async def test_full_mixed_batch_complete_conservation(db_conn):
    """完整混合批次：5 条事实，所有口径计数守恒。"""
    await _init_schema(db_conn)

    facts = {}
    # 1. 普通真实事实
    facts["normal"] = uuid.uuid4().hex
    await _seed(db_conn, facts["normal"], title="项目经理",
                content="- **项目经理**: 张伟",
                is_simulated=0, fact_key="project_manager")
    # 2. 模拟值（非安全关键名称）
    facts["simulated"] = uuid.uuid4().hex
    await _seed(db_conn, facts["simulated"], title="项目名称",
                content="- **项目名称**: 示例 ⚠️*(模拟值)*",
                is_simulated=1, fact_key="project_name")
    # 3. 安全关键（非模拟值）
    facts["safety_critical"] = uuid.uuid4().hex
    await _seed(db_conn, facts["safety_critical"], title="混凝土强度等级",
                content="- **混凝土强度等级**: C30",
                is_simulated=0, fact_key="concrete_grade")
    # 4. 已确认
    facts["already_resolved"] = uuid.uuid4().hex
    await _seed(db_conn, facts["already_resolved"], title="工程名称",
                content="- **工程名称**: 测试",
                is_simulated=0, is_resolved=1, fact_key="engineering_name")
    # 5. 另一条普通
    facts["normal2"] = uuid.uuid4().hex
    await _seed(db_conn, facts["normal2"], title="编制依据",
                content="- **编制依据**: 招标文件",
                is_simulated=0, fact_key="basis")

    fact_ids = list(facts.values())
    res = await batch_resolve(
        {"fact_ids": fact_ids, "scheme_id": "s1"}, db_conn)

    # 守恒验证
    total = len(fact_ids)
    assert res["changed"] == 2  # normal + normal2
    assert res["skipped"] == 1  # already_resolved
    assert res["skipped_safety_count"] == 2  # simulated + safety_critical
    assert res["changed"] + res["skipped"] + res["skipped_safety_count"] == total
    assert res["safety_blocked"] is True

    # 每条明细的 reason 正确
    reasons = {item["id"]: item["reason"] for item in res["skipped_safety"]}
    assert "模拟值" in reasons[facts["simulated"]]
    assert "安全关键" in reasons[facts["safety_critical"]]


# ---------------------------------------------------------------------------
# 测试 10：通过 title 名称命中安全关键白名单（无 fact_key）
# ---------------------------------------------------------------------------

async def test_safety_critical_by_title_name_without_fact_key(db_conn):
    """安全关键判定：title 包含白名单关键词时，即使无 fact_key 也应被跳过。"""
    await _init_schema(db_conn)
    fid = uuid.uuid4().hex
    # title 包含"基坑深度"（白名单关键词），fact_key 为空
    await _seed(db_conn, fid, title="基坑深度",
                content="- **基坑深度**: 6.0m",
                is_simulated=0, fact_key="")

    res = await batch_resolve(
        {"fact_ids": [fid], "scheme_id": "s1"}, db_conn)
    assert res["ok"] is True
    assert res["safety_blocked"] is True
    assert res["skipped_safety_count"] == 1

    cur = await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    assert row["is_resolved"] == 0, "安全关键事实（按 title 判定）不得被批量确认"