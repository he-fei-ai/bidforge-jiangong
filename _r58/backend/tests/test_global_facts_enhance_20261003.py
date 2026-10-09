"""全局事实模块增强回归测试（2026-10-03）。

锁定本轮三项增强：
- E1：list_facts 分页（limit/offset，默认不限，向后兼容；stats 始终基于全量）
- E2：XV-NUM-UNIT 量纲一致性冲突（单位不一致 / 疑似单位换算错误）
- E3：跨模块只读桥接 load_resolved_facts_for_scope（fail-soft、受同一注入门控）
"""
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.services.facts_cross_validators as xv
import app.services.facts_extractor as fe
import pytest
from app.db import get_conn, init_db
from app.services.facts_extractor import FactItem


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-enhance.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _seed_group(db, pid, sid, gid, name, value, *, simulated=False,
                      resolved=1, conflict=0, stale=0, unit=""):
    fid = uuid.uuid4().hex
    content = gf._build_fact_content(name, value + (f" {unit}" if unit else ""), simulated)
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "is_stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, gid, "工期安排", name, content, "schedule",
         '[{"file":"招标文件","quote":"x"}]', 1 if simulated else 0, 0.9,
         resolved, conflict, "", name, stale))
    await db.commit()
    return fid


# ---------------------------------------------------------------------------
# E1 · list_facts 分页
# ---------------------------------------------------------------------------
async def test_list_facts_default_returns_all(ctx):
    """默认 limit=0 返回全量（向后兼容，前端旧行为不变）。"""
    db, pid, sid = ctx
    for i in range(5):
        await _seed_group(db, pid, sid, f"g{i}", f"工期{i}", str(10 + i))
    resp = await gf.list_facts(scheme_id=sid, project_id=pid, db=db)
    assert resp["pagination"]["limit"] == 0
    assert resp["pagination"]["total_groups"] == 5
    assert resp["pagination"]["returned"] == 5
    assert len(resp["groups"]) == 5
    # stats 反映全量
    assert resp["stats"]["total"] == 5


async def test_list_facts_pagination_slices_and_stats_full(ctx):
    """limit/offset 仅裁剪返回分组，stats 仍基于全量。"""
    db, pid, sid = ctx
    for i in range(7):
        await _seed_group(db, pid, sid, f"g{i}", f"工期{i}", str(10 + i))
    resp = await gf.list_facts(scheme_id=sid, project_id=pid,
                               limit=3, offset=2, db=db)
    assert resp["pagination"]["total_groups"] == 7
    assert resp["pagination"]["returned"] == 3
    assert len(resp["groups"]) == 3
    # 第 3 页（offset=2, limit=3）应拿到 g2/g3/g4 对应的排序后分组
    assert resp["stats"]["total"] == 7
    # 分页字段本身随请求变化
    resp2 = await gf.list_facts(scheme_id=sid, project_id=pid,
                                limit=2, offset=0, db=db)
    assert resp2["pagination"]["returned"] == 2


async def test_list_facts_pagination_overflow_returns_partial(ctx):
    """offset 超出总量时返回空列表，不报错。"""
    db, pid, sid = ctx
    await _seed_group(db, pid, sid, "g0", "工期", "10")
    resp = await gf.list_facts(scheme_id=sid, project_id=pid,
                               limit=10, offset=99, db=db)
    assert resp["pagination"]["total_groups"] == 1
    assert resp["pagination"]["returned"] == 0
    assert resp["groups"] == []


# ---------------------------------------------------------------------------
# E2 · XV-NUM-UNIT 量纲一致性冲突
# ---------------------------------------------------------------------------
def _num_item(name, value, key):
    return FactItem(name=name, value=value, key=key, category="technique",
                    source="doc", confidence=0.9)


def test_xv_num_unit_inconsistent_low():
    """数值等价、仅单位不同（12.5m vs 1250cm）→ 低危「统一单位」提示。"""
    items = [
        _num_item("开挖深度", "12.5m", "excavation_depth"),
        _num_item("开挖深度", "1250cm", "excavation_depth"),
    ]
    confs = xv.run_cross_validations(items)
    num = [c for c in confs if c["rule_id"] == "XV-NUM-UNIT"]
    assert len(num) == 1
    assert num[0]["conflict_type"] == "numeric_unit_inconsistent"
    assert num[0]["severity"] == "low"
    assert num[0]["auto_resolvable"] is False
    # 两项的矛盾候选互填，下游可渲染裁决
    assert items[0].has_conflict and items[1].has_conflict
    assert any(c["value"] == "1250cm" for c in items[0].conflict_values)
    assert any(c["value"] == "12.5m" for c in items[1].conflict_values)


def test_xv_num_scale_suspect_medium():
    """量级差恰为换算比（1250cm vs 0.125m，100 倍，且单位不同）→ 中危「疑似单位换算错误」。

    注意：两值须为不同单位才会进入本规则；同单位（如 12.5m vs 0.125m）由
    XV-SAME-NAME 判「值不同」，本规则按设计跳过（避免与 XV-SAME-NAME 重复）。
    """
    items = [
        _num_item("开挖深度", "1250cm", "excavation_depth"),
        _num_item("开挖深度", "0.125m", "excavation_depth"),
    ]
    confs = xv.run_cross_validations(items)
    num = [c for c in confs if c["rule_id"] == "XV-NUM-UNIT"
           and c["conflict_type"] == "numeric_scale_suspect"]
    assert len(num) == 1
    assert num[0]["severity"] == "medium"


def test_xv_num_unit_ignores_unitless_and_same_unit():
    """纯数字（工期天数）与同单位不同值不进本规则（交 XV-SAME-NAME）。"""
    items = [
        _num_item("总工期", "365", "total_duration"),
        _num_item("总工期", "360", "total_duration"),
        _num_item("开挖深度", "12.5m", "excavation_depth"),
        _num_item("开挖深度", "13.0m", "excavation_depth"),
    ]
    confs = xv.run_cross_validations(items)
    num = [c for c in confs if c["rule_id"] == "XV-NUM-UNIT"]
    # 仅 12.5m/13.0m 同单位 → 不进本规则；总工期纯数字 → 不进本规则
    assert num == []


def test_xv_num_unit_different_dimensions_not_compared():
    """长度与质量即使数值巧合也不互比（12.5m vs 12.5kg 不同量纲）。"""
    items = [
        _num_item("尺寸", "12.5m", "dim_m"),
        _num_item("重量", "12.5kg", "dim_kg"),
    ]
    confs = xv.run_cross_validations(items)
    assert [c for c in confs if c["rule_id"] == "XV-NUM-UNIT"] == []


# ---------------------------------------------------------------------------
# E3 · 跨模块只读桥接
# ---------------------------------------------------------------------------
async def test_bridge_returns_only_injectable_facts(ctx):
    """桥接只返回「可注入」事实（无矛盾/已确认/非模拟/未过期）。"""
    db, pid, sid = ctx
    ok = await _seed_group(db, pid, sid, "g0", "项目经理", "张伟",
                           resolved=1, conflict=0, stale=0)
    bad = await _seed_group(db, pid, sid, "g1", "工期", "365",
                            resolved=1, conflict=1, stale=0)
    sim = await _seed_group(db, pid, sid, "g2", "质保期", "两年",
                            simulated=True, resolved=0)
    out = await fe.load_resolved_facts_for_scope(db, scheme_id=sid, project_id=pid)
    names = {f["name"] for f in out}
    assert "项目经理" in names
    assert "工期" not in names          # 有矛盾，被门控排除
    assert "质保期" not in names        # 模拟值，被门控排除
    assert len(out) == 1
    assert out[0]["fact_id"] == ok


async def test_bridge_failsoft_on_bad_scope(ctx):
    """作用域缺失不抛异常，返回空列表（下游把「无事实」当「不增强」）。"""
    db, pid, sid = ctx  # 未插入任何事实
    out = await fe.load_resolved_facts_for_scope(db, scheme_id="", project_id="")
    assert out == []


async def test_bridge_field_shape(ctx):
    """桥接返回结构含统一字段，下游可直接取用。"""
    db, pid, sid = ctx
    await _seed_group(db, pid, sid, "g0", "项目经理", "张伟")
    out = await fe.load_resolved_facts_for_scope(db, scheme_id=sid, project_id=pid)
    f = out[0]
    for k in ("fact_id", "name", "value", "category", "chapter",
              "fact_attr", "source", "is_safety_critical", "fact_key",
              "group_title"):
        assert k in f
