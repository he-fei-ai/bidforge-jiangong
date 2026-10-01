# -*- coding: utf-8 -*-
"""目录库模块全面更新回归护栏（2026-10-01，六大类危大逐型式）

锁定本次更新的八类不变量（依据：住建部令第 37 号、建办质〔2018〕31 号）：

1. 注册表 parity：BUILDERS / TEMPLATE_META / _TEMPLATE_NAMES 三表键一致；
   RULES 目标键全部存活（防死模板）；26 个新键全部有 RULES 可达（防孤儿）；
2. 九大章节骨架：26 个新 builder 一级章节严格等于标准九章顺序；深度 ≤3；
3. builder 无状态：每次调用产出全新对象（内建模板库零缓存 →
   「缓存未失效」类缺陷结构性免疫，此处锁死该性质）；
4. 细粒度路由：逐型式名命中专属模板；通用名仍落兜底模板（向后兼容锁）；
5. 编制依据四层：法律法规 / 部门规章 / 强制性规范 / 专项技术规范；
6. seed 清单静态：无重名、每分类有画像、版本递增、新条目全部纳入；
7. seed 端到端：全量插入 → 幂等跳过 → force 刷新 → stale 清理 →
   并发重入不产生重复行；
8. 自动匹配断链修复：类别码→预置中文 type 映射、逗号拆分。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid

import pytest

import app.seed_data as seed_data
from app.seed_data import CATEGORY_PROFILE, SCHEME_CATALOG, SEED_VERSION
from app.services import outline_reference as oref
from app.services.outline_templates import (
    BUILDERS, RULES, TEMPLATE_META, _TEMPLATE_NAMES,
    build_outline, get_meta, match_template,
)
from app.services.scheme_classification import HAZARD_CATEGORIES

#: 本次新增的 26 个逐型式模板键（§1 六大类拆分）
NEW_KEYS = [
    # 脚手架族（9）
    "scaffold_ground", "scaffold_attached", "scaffold_cantilever",
    "scaffold_gate", "scaffold_cuplock", "scaffold_disc", "scaffold_gondola",
    "scaffold_unload_platform", "scaffold_work_platform",
    # 模板支撑族（2）+ 起重族（2）
    "formwork_tall", "formwork_disc", "tower_crane", "construction_hoist",
    # 拆除族（3）
    "demolition_manual", "demolition_machine", "demolition_blast",
    # 其他危大族（10）
    "curtain_wall", "steel_structure", "grid_structure", "prestress",
    "manual_dig_pile", "slope", "underground_excavation", "pipe_jacking",
    "underwater", "new_tech",
]

#: 九大章节标准一级标题（与 builder 产出一致；第 9 章本仓口径为「计算书及相关图纸」）
NINE_CHAPTERS_TITLES = [
    "工程概况", "编制依据", "施工计划", "施工工艺技术", "施工安全保证措施",
    "施工管理及作业人员配备和分工", "验收要求", "应急处置措施", "计算书及相关图纸",
]


# ---------------------------------------------------------------- 1. 注册表 parity

class TestRegistryParity:
    def test_three_tables_parity(self):
        """BUILDERS / TEMPLATE_META / _TEMPLATE_NAMES 键集合必须一致。"""
        assert set(BUILDERS) == set(TEMPLATE_META), \
            "存在无 meta 的 builder 或有 meta 无 builder 的死模板"
        assert set(BUILDERS) == set(_TEMPLATE_NAMES), \
            "存在无中文名的 builder（列表/下拉将显示异常）"

    def test_rules_targets_all_alive(self):
        """RULES 的目标键必须全部在 BUILDERS 中（防死路由）。"""
        targets = {key for _kws, key in RULES}
        dead = targets - set(BUILDERS)
        assert not dead, f"RULES 指向不存在的模板键：{dead}"

    def test_new_keys_all_routed(self):
        """26 个新键必须各有至少一条 RULES 规则可达（防孤儿模板：
        注册了 builder 却永远不会被 match_template 命中）。"""
        targets = {key for _kws, key in RULES}
        orphan = [k for k in NEW_KEYS if k not in targets]
        assert not orphan, f"新模板未被任何 RULES 规则引用：{orphan}"

    def test_new_keys_registered(self):
        for k in NEW_KEYS:
            assert k in BUILDERS and k in TEMPLATE_META and k in _TEMPLATE_NAMES


# ---------------------------------------------------------------- 2. 九章骨架

class TestNineChapterSkeleton:
    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_new_builder_exactly_nine_chapters(self, key):
        """新 builder 一级章节严格等于九大章节标准（§2/§3 核心要求）。"""
        outline = BUILDERS[key]()
        titles = [n["title"] for n in outline]
        assert titles == NINE_CHAPTERS_TITLES, f"{key} 一级章节偏离九章标准：{titles}"

    def test_all_builders_at_least_nine(self):
        for key, builder in BUILDERS.items():
            assert len(builder()) >= 9, f"{key} 一级章节不足九章"

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_new_builder_has_level2_and_level3(self, key):
        """九章每章必须有二级，且至少一个三级（§2「每章节下设二三级」）。"""
        for ch in BUILDERS[key]():
            kids = ch.get("children") or []
            assert kids, f"{key}/{ch['title']} 缺二级目录"
            assert any(k.get("children") for k in kids), \
                f"{key}/{ch['title']} 缺三级目录"

    def test_max_depth_three_all_builders(self):
        """全部 builder 深度 ≤3（MAX_OUTLINE_DEPTH=3，前端按三级渲染）。"""
        def depth(nodes, d=1):
            m = d
            for n in nodes or []:
                if n.get("children"):
                    m = max(m, depth(n["children"], d + 1))
            return m
        for key, builder in BUILDERS.items():
            assert depth(builder()) <= 3, f"{key} 目录深度超过 3 级"


# ---------------------------------------------------------------- 3. builder 无状态

class TestBuilderStateless:
    """内建模板库是「每次调用现场构建」的纯函数 —— 本仓目录库无内存缓存层，
    「缓存未失效」缺陷的结构性防线：任何人给 builder 加模块级缓存/共享可变
    默认值都会打破下面的身份断言。"""

    def test_each_call_returns_fresh_objects(self):
        for key, builder in BUILDERS.items():
            a, b = builder(), builder()
            assert a is not b and a[0] is not b[0], f"{key} 两次调用共享对象"
            a[0]["title"] = "__MUTATED__"
            assert builder()[0]["title"] != "__MUTATED__", \
                f"{key} 返回值被调用间共享（可变状态泄漏）"

    def test_build_outline_via_registry(self):
        """build_outline 委托 BUILDERS 且未注册键兜底不抛错。"""
        assert len(build_outline("完全不属于任何类别的名字")) >= 9


# ---------------------------------------------------------------- 4. 细粒度路由

ROUTE_PROBES = {
    # §1 六大类逐型式：方案名 → 期望专属模板
    "落地式钢管脚手架专项施工方案": "scaffold_ground",
    "落地脚手架专项施工方案": "scaffold_ground",
    "附着式升降脚手架专项施工方案": "scaffold_attached",
    "悬挑式脚手架专项施工方案": "scaffold_cantilever",
    "门式脚手架专项施工方案": "scaffold_gate",
    "碗扣式钢管脚手架专项施工方案": "scaffold_cuplock",
    "承插型盘扣式脚手架专项施工方案": "scaffold_disc",
    "高处作业吊篮专项施工方案": "scaffold_gondola",
    "悬挑卸料平台专项施工方案": "scaffold_unload_platform",
    "移动式操作平台专项施工方案": "scaffold_work_platform",
    "高大模板支撑体系专项施工方案": "formwork_tall",
    "高大支模专项施工方案": "formwork_tall",
    "超限梁模板支撑专项施工方案": "formwork_tall",
    "承插型盘扣式模板支撑架专项施工方案": "formwork_disc",
    "塔式起重机安装拆卸专项施工方案": "tower_crane",
    "塔吊安装专项施工方案": "tower_crane",
    "施工升降机安装与拆卸专项施工方案": "construction_hoist",
    "人货电梯安装与拆卸专项施工方案": "construction_hoist",
    "人工拆除工程专项施工方案": "demolition_manual",
    "机械拆除工程专项施工方案": "demolition_machine",
    "爆破拆除工程专项施工方案": "demolition_blast",
    "预应力结构张拉专项施工方案": "prestress",
    "幕墙安装工程专项施工方案": "curtain_wall",
    "建筑幕墙安装专项施工方案": "curtain_wall",
    "钢结构安装工程专项施工方案": "steel_structure",
    "网架和索膜结构安装专项施工方案": "grid_structure",
    "人工挖孔桩工程专项施工方案": "manual_dig_pile",
    "边坡工程专项施工方案": "slope",
    "地下暗挖工程专项施工方案": "underground_excavation",
    "顶管工程专项施工方案": "pipe_jacking",
    "水下作业工程专项施工方案": "underwater",
    "新技术新工艺新材料新设备专项施工方案": "new_tech",
}

# 向后兼容锁：通用名（未指明型式）必须仍落原有兜底模板，行为与更新前一致；
# 复合名探针防跨类误伤。
COMPAT_PROBES = {
    "脚手架工程专项施工方案": "scaffold",
    "拆除专项施工方案": "demolition",
    "起重吊装专项施工方案": "lifting",
    "模板工程专项施工方案": "formwork",
    "基坑工程专项施工方案": "foundation_pit",
    "塔吊基础专项施工方案": "tower_crane",
    "深基坑支护专项施工方案": "foundation_pit",
    "土方开挖专项施工方案": "foundation_pit",
    "建筑拆除工程专项施工方案": "demolition",
    "应急预案": "emergency",
}


class TestRouting:
    @pytest.mark.parametrize("name,expected", sorted(ROUTE_PROBES.items()))
    def test_specific_type_routes_to_own_template(self, name, expected):
        assert match_template(name) == expected

    @pytest.mark.parametrize("name,expected", sorted(COMPAT_PROBES.items()))
    def test_generic_names_keep_legacy_fallback(self, name, expected):
        assert match_template(name) == expected

    def test_every_seed_scheme_name_routes(self):
        """预置清单全部名称可路由（不抛错、返回非空 key、能生成目录）。"""
        for schemes in SCHEME_CATALOG.values():
            for name in schemes:
                assert match_template(name), f"{name} 路由返回空"
                assert build_outline(name), f"{name} 生成空目录"


# ---------------------------------------------------------------- 5. 编制依据四层

class TestMetaLayers:
    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_basis_four_layers(self, key):
        """§5 分层：法律法规 / 部门规章 / 强制性规范 / 专项技术规范。"""
        basis = TEMPLATE_META[key]["basis"]
        for layer in ("法律法规：", "部门规章：", "强制性规范：", "专项技术规范："):
            assert layer in basis, f"{key} basis 缺「{layer}」层"
        assert "37 号" in basis, f"{key} 法律法规层缺住建部令第 37 号"
        assert "31 号" in basis, f"{key} 部门规章层缺建办质〔2018〕31 号"

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_applicable_and_risk(self, key):
        meta = TEMPLATE_META[key]
        assert meta["applicable"].startswith("适用"), f"{key} 适用条件未以适用范围开头"
        assert ("不适用" in meta["applicable"] or "论证" in meta["applicable"]
                or "危大" in meta["applicable"]), f"{key} 缺判定要件"
        assert meta["risk"] in ("危大工程", "超过一定规模危大工程")

    def test_get_meta_shape(self):
        m = get_meta("落地式钢管脚手架专项施工方案")
        assert m["template"] == "scaffold_ground"
        assert {"basis", "applicable", "risk"} <= set(m)


# ---------------------------------------------------------------- 6. seed 清单静态

class TestSeedCatalogStatic:
    def test_no_duplicate_scheme_names(self):
        """seed 以「{方案名}标准目录」为唯一键 —— 跨分类重名会互相覆盖 type。"""
        flat = [s for v in SCHEME_CATALOG.values() for s in v]
        dups = sorted({s for s in flat if flat.count(s) > 1})
        assert not dups, f"SCHEME_CATALOG 存在重名条目：{dups}"

    def test_every_category_has_profile(self):
        assert set(SCHEME_CATALOG) <= set(CATEGORY_PROFILE)

    def test_version_bumped_for_content_update(self):
        assert SEED_VERSION == "v3.0", "内容升级必须递增版本以刷新存量预置库"

    def test_new_entries_all_in_catalog(self):
        """§7.1 补齐全部类型：26 个新型式代表名都应在预置清单内
        （兼容探针名中的既有条目除外）。"""
        all_names = {n for v in SCHEME_CATALOG.values() for n in v}
        legacy_probe = {"落地脚手架专项施工方案", "高大支模专项施工方案",
                        "超限梁模板支撑专项施工方案", "塔吊安装专项施工方案",
                        "人货电梯安装与拆卸专项施工方案", "建筑幕墙安装专项施工方案"}
        for name in ROUTE_PROBES:
            if name in legacy_probe:
                continue
            assert name in all_names, f"新型式条目 {name} 未纳入预置清单"


# ---------------------------------------------------------------- 7. seed 端到端

@pytest.fixture
def seed_env(monkeypatch, db_conn):
    """把 seed_data 的 init_db/get_conn 重定向到内存测试库。"""
    async def _noop_init_db():
        return None

    async def _conn():
        return db_conn

    monkeypatch.setattr(seed_data, "init_db", _noop_init_db)
    monkeypatch.setattr(seed_data, "get_conn", _conn)
    return db_conn


class TestSeedEndToEnd:
    @staticmethod
    async def _count_rows(conn):
        cur = await conn.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT name) dn FROM outline_library"
            " WHERE source='预置清单'")
        r = await cur.fetchone()
        return r["c"], r["dn"]

    async def test_full_seed_idempotent_and_force(self, seed_env):
        conn = seed_env
        total = sum(len(v) for v in SCHEME_CATALOG.values())
        r1 = await seed_data.seed_catalog()
        assert r1["inserted"] == total and r1["updated"] == 0
        # 第二次：版本一致 → 全跳过（幂等，不重复插入）
        r2 = await seed_data.seed_catalog()
        assert r2["inserted"] == 0 and r2["updated"] == 0 and r2["skipped"] == total
        c, dn = await self._count_rows(conn)
        assert c == dn == total, "存在同名重复预置行"
        # force：全量刷新，行数不变
        r3 = await seed_data.seed_catalog(force=True)
        assert r3["updated"] == total and r3["inserted"] == 0
        c, _ = await self._count_rows(conn)
        assert c == total

    async def test_all_seeded_outlines_nine_chapter_and_versioned(self, seed_env):
        conn = seed_env
        await seed_data.seed_catalog()
        cur = await conn.execute(
            "SELECT name, outline_json, version, basis FROM outline_library"
            " WHERE source='预置清单'")
        rows = await cur.fetchall()
        assert rows
        for row in rows:
            assert row["version"] == SEED_VERSION
            outline = json.loads(row["outline_json"])
            assert len(outline) >= 9, f"{row['name']} 预置目录不足九章"
            assert row["basis"], f"{row['name']} 预置条目缺编制依据"

    async def test_stale_seed_entry_cleaned(self, seed_env):
        conn = seed_env
        await seed_data.seed_catalog()
        # 模拟历史遗留：不在当前清单的旧版本预置条目
        await conn.execute(
            "INSERT INTO outline_library (id, name, type, version, source,"
            " outline_json) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, "已下线旧条目标准目录", "临时设施", "v1.0",
             "预置清单", "[]"))
        await conn.commit()
        r = await seed_data.seed_catalog()
        assert r["removed"] >= 1
        cur = await conn.execute(
            "SELECT COUNT(*) c FROM outline_library WHERE name='已下线旧条目标准目录'")
        assert (await cur.fetchone())["c"] == 0

    async def test_concurrent_seed_no_duplicates(self, seed_env):
        """并发重入（同进程 gather）经重入锁串行化 → 不产生重复行。"""
        conn = seed_env
        r_a, r_b = await asyncio.gather(
            seed_data.seed_catalog(), seed_data.seed_catalog())
        total = sum(len(v) for v in SCHEME_CATALOG.values())
        assert r_a["inserted"] + r_b["inserted"] == total, \
            "两路并发各自全量插入 = 重入锁失效"
        c, dn = await self._count_rows(conn)
        assert c == dn == total


# ---------------------------------------------------------------- 8. 自动匹配断链

class TestCategoryAutoMatch:
    def test_map_covers_six_hazard_categories(self):
        codes = {c["id"] for c in HAZARD_CATEGORIES}
        assert set(oref.CATEGORY_CODE_TO_SEED_TYPES) == codes, \
            "类别码映射与六大类权威定义不一致（parity 护栏）"

    def test_mapped_types_exist_in_seed_catalog(self):
        cats = set(SCHEME_CATALOG)
        for code, types in oref.CATEGORY_CODE_TO_SEED_TYPES.items():
            for t in types:
                assert t in cats, f"{code} 映射到不存在的预置分类「{t}」"

    async def test_category_code_hits_seed_type_library(self, db_conn):
        """核心回归锁：类别码（scaffold）必须能命中 type 为中文分类名
        （脚手架）的预置库 —— 旧实现两套体系不同源恒不命中。"""
        lid = uuid.uuid4().hex
        outline = [{"title": "工程概况", "description": "d", "children": []}]
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, type, outline_json,"
            " review_status, source) VALUES (?,?,?,?,?,?)",
            (lid, "落地式钢管脚手架专项施工方案标准目录", "脚手架",
             json.dumps(outline, ensure_ascii=False), "已通过", "预置清单"))
        await db_conn.commit()
        text, hits = await oref.build_category_reference_outline(db_conn, ["scaffold"])
        assert hits == [lid] and "脚手架" in text

    async def test_multi_code_and_raw_type_backward_compat(self, db_conn):
        """多类别码一次传入均命中；原始 type 值（如中文「基坑工程」）向后兼容。"""
        for name, typ in (("A库", "脚手架"), ("B库", "基坑与土方"), ("C库", "基坑工程")):
            await db_conn.execute(
                "INSERT INTO outline_library (id, name, type, outline_json,"
                " review_status) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, name, typ,
                 json.dumps([{"title": "工程概况", "children": []}]), "已通过"))
        await db_conn.commit()
        _t, hits = await oref.build_category_reference_outline(
            db_conn, ["scaffold", "foundation_pit"])
        assert len(hits) == 2, "多类别应同时命中两型预置库"
        _t, hits = await oref.build_category_reference_outline(db_conn, ["基坑工程"])
        assert len(hits) == 1, "非映射码必须按原始 type 兜底匹配（旧行为）"

    def test_sse_handler_splits_comma_codes(self):
        """源码护栏：bid_analysis 写入的 hazard_category 是逗号连接多码，
        sse_handlers 必须 split(',') 后再传（旧代码整串当单码恒不命中）。"""
        import app.routers.sse_handlers as sh
        src = inspect.getsource(sh)
        idx = src.index("scheme_auto_match_outline")
        seg = src[idx:src.index("bump_ref_count", idx)]
        assert 'split(",")' in seg, \
            "hazard_category 逗号拆分丢失：多类别方案将再次恒不命中"
