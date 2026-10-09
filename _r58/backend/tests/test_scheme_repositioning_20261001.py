"""专项施工方案定位切换护栏（2026-10-01 · 第二十一轮）

软件定位由「招投标编写软件」切换为「建筑工程专项施工方案编写软件」。本文件是
这次切换的**单一护栏**，防止任一侧回退：

==============================  =====================================================
分组                                    锁定什么
==============================  =====================================================
TestTechScoringOffline            「技术评分要求」提取项已下线（清单/分组/提示词/
                                  字段字典四出口同时失效），不得回流
TestBidResponseDomainGate         「招标响应域」硬门禁下线 —— 软开关
                                  ``bid_response_domain_enabled`` 不得再复活它
TestHazardCategoryCoverage        六大类危大工程子类覆盖用户清单全项（本轮补齐 7 个）
TestNineChaptersIntegrity         九大章节完整，且 ``source_items`` 不拆链
TestAntiRebidRedlines             提示词与导出红线：专项施工方案 ≠ 投标文件
==============================  =====================================================

⚠️ 每个断言都必须能**反向验证**：还原被护栏锁住的实现后应定向失败。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from app.services import bid_analysis_service as basvc
from app.services import scheme_classification as sc

REPO = pathlib.Path(__file__).resolve().parent.parent   # → backend/
APP = REPO / "app"

# 用户清单原文（《软件重新定位：专项施工方案编写软件》第四节）。
# 每个子类名给出**识别关键词**而非精确名称 —— 部文与产品清单的表述不完全
# 一致（如部文写「塔式起重机」、清单写「塔机」），护栏按语义命中而非字面相等。
HAZARD_SPEC = {
    "基坑工程": ["基坑支护", "土方开挖", "基坑监测"],
    "模板工程及支撑体系": ["模板支撑", "高大模板", "盘扣式模板支撑"],
    "起重吊装及起重机械安装拆卸工程": ["起重吊装", "塔机", "施工升降机"],
    "脚手架工程": ["落地式", "附着式", "悬挑式", "门式", "碗扣式", "盘扣式",
                   "吊篮", "卸料平台", "操作平台"],
    "拆除、爆破工程": ["人工拆除", "机械拆除", "爆破"],
    "其他危大工程": ["幕墙", "钢结构", "网架", "预应力", "暗挖", "顶管",
                     "水下作业", "人工挖孔桩", "边坡", "新技术"],
}


# =========================================================================
# 1. 技术评分要求提取项已下线
# =========================================================================
class TestTechScoringOffline:
    def test_registry_has_no_orphan_prompts(self):
        """清单与提示词必须严格同源 —— 防「幽灵入口」。

        只删 ``ANALYSIS_ITEMS`` 而漏删 ``_ITEM_PROMPTS``，会出现清单里没有该项、
        按 id 却能取到提示词的状态：前端看不到，脚本却能触发一次注定失败的调用。
        反向验证：把 techScoring 提示词加回去（但不动清单）→ 本例失败。
        """
        item_ids = {i["item_id"] for i in basvc.ANALYSIS_ITEMS}
        prompt_keys = set(basvc._ITEM_PROMPTS.keys())
        assert prompt_keys == item_ids, (
            f"清单与提示词不同源：多余={sorted(prompt_keys - item_ids)}，"
            f"缺失={sorted(item_ids - prompt_keys)}")

    def test_item_maps_cover_all_registered_items(self):
        """id → 定义/域 的两张索引表必须覆盖注册表里的全部条目。

        新增或下线解析项时最容易漏改这两张表：`_ITEM_MAP` 漏改 → 按 id 查
        不到定义（脚本 500）；`_ITEM_DOMAIN` 漏改 → 域归属错判（可能把一个
        scheme 项当成其它域处理）。这里用**差集**双向校验。
        """
        registered = {
            i["item_id"] for v in basvc.EXTRACTION_DOMAINS.values() for i in v
        }
        assert registered, "注册表为空？"
        assert set(basvc._ITEM_MAP.keys()) == registered, (
            f"_ITEM_MAP 差集：缺={sorted(registered - set(basvc._ITEM_MAP))}，"
            f"多={sorted(set(basvc._ITEM_MAP) - registered)}")
        assert set(basvc._ITEM_DOMAIN.keys()) == registered, (
            f"_ITEM_DOMAIN 差集：缺={sorted(registered - set(basvc._ITEM_DOMAIN))}，"
            f"多={sorted(set(basvc._ITEM_DOMAIN) - registered)}")
        for item_id, domain in basvc._ITEM_DOMAIN.items():
            assert item_id in {
                i["item_id"] for i in basvc.EXTRACTION_DOMAINS.get(domain, [])
            }, f"{item_id} 声称属于 {domain} 域，但该域没有这个条目"

    def test_tech_scoring_literal_not_in_module_constants(self):
        """静态护栏：模块级常量里不得再有 "techScoring" 字符串字面量。

        注释允许保留（记录下线原因），所以只看 AST 的 Constant 节点。
        反向验证：在 ANALYSIS_ITEMS 或 _ITEM_PROMPTS 里加回 techScoring → 失败。
        """
        src = (APP / "services" / "bid_analysis_service.py").read_text("utf-8")
        tree = ast.parse(src)
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value == "techScoring"
        }
        assert not literals, "techScoring 字符串字面量已回流"

    def test_scheme_domain_contract_counts(self):
        """scheme 域口径：18 项 / 17 必选 / 13 分组。

        ⚠️ 历史口径锚点：techScoring 下线前是 19 / 17 / 2 / 14。
        改动解析项时必须同步这里，否则护栏静默失效。
        """
        assert len(basvc.ANALYSIS_ITEMS) == 18
        assert len(basvc.REQUIRED_ITEM_IDS) == 17
        assert len(basvc.GROUPS) == 13
        assert not any("评分" in g.get("label", "") for g in basvc.GROUPS), (
            "分组名不得再出现评分语义")
# =========================================================================
# 2. 招标响应域硬门禁
# =========================================================================
class TestBidResponseDomainGate:
    @pytest.mark.asyncio
    async def test_items_bid_response_always_404(self, monkeypatch):
        """无论开关怎么写，招标响应域清单都必须 404，且 detail 带定位说明。"""
        from app.config import settings
        from app.routers import bid_analysis as ba
        for flag in (False, True):
            monkeypatch.setattr(settings, "bid_response_domain_enabled",
                                flag, raising=False)
            with pytest.raises(Exception) as exc:
                await ba.list_analysis_items("bid_response")
            assert getattr(exc.value, "status_code", 0) == 404
            assert "专项施工方案" in (exc.value.detail or "")

    @pytest.mark.asyncio
    async def test_single_item_of_retired_domain_404(self):
        """即使调用方知道 item_id，招标响应域的项也不得读。"""
        from app.routers import bid_analysis as ba
        for item_id in ("projectInfo", "techRequirements", "businessScoring",
                        "partAInfo", "responseFileRequirements"):
            with pytest.raises(Exception) as exc:
                await ba.get_analysis_item(item_id)
            assert getattr(exc.value, "status_code", 0) == 404, item_id

    @pytest.mark.asyncio
    async def test_domains_endpoint_marks_retired(self):
        """/domains 必须回 retired 标记，让前端隐藏而非渲染必 404 的选项。"""
        from app.routers import bid_analysis as ba
        res = await ba.list_extraction_domains()
        by_name = {d["domain"]: d for d in res["domains"]}
        assert by_name["scheme"]["enabled"] is True
        br = by_name["bid_response"]
        assert br["enabled"] is False
        assert br["retired"] is True
        assert "专项施工方案" in br["retired_reason"]

    @pytest.mark.asyncio
    async def test_start_sse_gate_rejects_retired_domain(self, db_conn):
        """SSE 入口必须最先做门禁（不查文档、不建任务即拒绝）。"""
        from app.routers import bid_analysis as ba
        from app.services.ai.task_registry import _tasks
        _tasks.clear()
        with pytest.raises(Exception) as exc:
            await ba._start_sse_inner(db_conn, "", "no_such_project", "key",
                                      "", False, domain="bid_response")
        assert getattr(exc.value, "status_code", 0) == 404
        assert not _tasks, "门禁必须在建任务之前，不能留下孤儿任务"

    @staticmethod
    def _deco_attr(node) -> str:
        """取装饰器的方法名（`@router.get(...)` 的装饰器节点是 Call，要下钻一层）。"""
        if isinstance(node, ast.Call):
            node = node.func
        return node.attr if isinstance(node, ast.Attribute) else ""

    def test_every_domain_endpoint_is_gated(self):
        """静态护栏：**路由端点**暴露 domain 参数时必须过统一门禁。

        这是本仓反复出现的「同一判据多处各自实现」模式的防御：门禁漏设一个
        入口，招投标域就能从那个入口复活。用 AST 扫全函数体（含嵌套 def），
        避免「函数体被整段删掉」让文本匹配漏判。

        满足条件有两种（都要接受，否则护栏过严会逼人写绕开门禁的代码）：
        1. 端点**直接**调用 ``_assert_domain_available``；
        2. 端点把 domain 传给一个**已门禁的 helper**（如 SSE 包装函数转调
           ``_start_sse_inner``，门禁在 helper 内最前面）。
        ⚠️ 只强制**路由端点**（`@router.*`）：模块内部 helper 不经 FastAPI
        到达，且可能承载合法的多域数据读写，不纳入本约束。
        """
        tree = ast.parse((APP / "routers" / "bid_analysis.py").read_text("utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        def _called_names(fn):
            return {n.func.id for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

        direct_gate = {fn.name for fn in fns
                       if "_assert_domain_available" in _called_names(fn)}

        gated, ungated = [], []
        for fn in fns:
            has_domain = any(a.arg == "domain"
                             for a in fn.args.args + fn.args.kwonlyargs)
            if not has_domain:
                continue
            is_endpoint = any(
                any(prefix in self._deco_attr(d) for prefix in
                    ("get", "post", "put", "delete", "patch"))
                for d in fn.decorator_list)
            if not is_endpoint:
                continue
            names = _called_names(fn)
            covered = (bool(names & direct_gate)) or (fn.name in direct_gate)
            (gated if covered else ungated).append(fn.name)
        assert not ungated, f"暴露 domain 参数的路由端点未过门禁：{ungated}"
        assert len(gated) >= 3, f"门禁覆盖面异常窄：{gated}"
        assert len(direct_gate) >= 3, f"直接门禁的函数过少：{sorted(direct_gate)}"

    def test_gate_helper_fails_closed_on_unknown_input(self):
        """非字符串 domain（如单测直传 Query 对象）不得抛 AttributeError。"""
        from app.routers import bid_analysis as ba
        ba._assert_domain_available(None)
        ba._assert_domain_available("")
        ba._assert_domain_available("scheme")
        ba._assert_domain_available(object())

# =========================================================================
# 3. 六大类危大工程覆盖度
# =========================================================================
class TestHazardCategoryCoverage:
    def test_six_big_categories_exact(self):
        """六大类 id 与名称必须与部文附件一一致，顺序不得漂移。"""
        cats = [(c["id"], c["name"]) for c in sc.HAZARD_CATEGORIES]
        assert cats == [
            ("foundation_pit", "基坑工程"),
            ("formwork", "模板工程及支撑体系"),
            ("hoisting", "起重吊装及起重机械安装拆卸工程"),
            ("scaffold", "脚手架工程"),
            ("demolition", "拆除、爆破工程"),
            ("other", "其他危大工程"),
        ]

    @pytest.mark.parametrize("cat_name", list(HAZARD_SPEC))
    def test_each_required_subtype_is_recognizable(self, cat_name):
        """用户清单要求的每个子类必须能被关键词识别命中。

        覆盖度判据用「关键词命中」而非「子类名称精确相等」：部文表述
        （塔式起重机 / 盘扣式钢管脚手架）与产品清单（塔机 / 盘扣式）不完全
        一致，只锁名称会让护栏形同虚设。
        """
        cat = next(c for c in sc.HAZARD_CATEGORIES if c["name"] == cat_name)
        all_kw = []
        for sub in cat["subs"]:
            all_kw.extend(str(k) for k in sub.get("keywords") or ())
            all_kw.append(sub["name"])
        for want in HAZARD_SPEC[cat_name]:
            assert any(want in kw or kw in want for kw in all_kw if len(kw) >= 2), (
                f"「{cat_name}」缺少子类：{want}；现有关键词={all_kw}")

    def test_new_subtypes_wired_end_to_end(self):
        """本轮补齐的 7 个子类必须能真正确定方案名称 → 危大判定。"""
        cases = [
            ("人工挖孔桩专项施工方案", "ot_bored_pile"),
            ("基坑监测专项施工方案", "fp_monitoring"),
            ("碗扣式钢管脚手架专项施工方案", "sc_cuplock"),
            ("盘扣式钢管脚手架专项施工方案", "sc_disc"),
            ("盘扣式模板支撑体系专项施工方案", "fw_disc"),
            ("塔式起重机安装拆卸专项施工方案", "ho_tower_crane"),
            ("施工升降机安装拆卸专项施工方案", "ho_construction_hoist"),
        ]
        for name, want_sub in cases:
            hits = sc.classify_scheme_name(name)
            got = {h["sub_id"] for h in hits}
            assert want_sub in got, f"{name} 未命中 {want_sub}，实际={got}"

    def test_threshold_keys_all_resolvable(self):
        """每个子类的 threshold 键必须存在（或为 None），否则判定静默失效。"""
        for cat in sc.HAZARD_CATEGORIES:
            for sub in cat["subs"]:
                key = sub.get("threshold")
                if key is None:
                    continue
                assert key in sc.HAZARD_THRESHOLDS, (
                    f"子类 {sub['id']} 的 threshold 键 {key!r} 不在 HAZARD_THRESHOLDS")
                r = sc.evaluate_hazard_level(key, {})
                assert isinstance(r["is_hazardous"], bool)
                assert isinstance(r["missing_params"], list)

    def test_non_parameter_subs_are_conservatively_hazardous(self):
        """threshold=None 的子类必须保守判危大（不漏判是安全红线）。"""
        assert any(
            sub.get("threshold") is None
            for cat in sc.HAZARD_CATEGORIES for sub in cat["subs"]), (
            "阈值表里没有任何非参数型子类，本例将失去意义")
        r = sc.evaluate_hazard_level(None, {})
        assert r["is_hazardous"] is True


# =========================================================================
# 4. 九大章节完整性
# =========================================================================
class TestNineChaptersIntegrity:
    def test_chapter_numbers_contiguous(self):
        assert [c["chapter"] for c in sc.NINE_CHAPTERS] == list(range(1, 10))

    def test_chapter_keys_unique_and_nonempty(self):
        keys = [c["key"] for c in sc.NINE_CHAPTERS]
        assert len(keys) == len(set(keys))
        assert all(k.strip() for k in keys)
        assert all(c["title"].strip() for c in sc.NINE_CHAPTERS)

    def test_source_items_never_dangling(self):
        """chapter.source_items 必须都是真实存在的提取项 id（拆链护栏）。

        这是与「techScoring 下线」直接相关的护栏：若九大章节里引用了已下线
        的 item_id，章节字段映射会静默丢一列来源（AI 不报错，只是内容变少）。
        """
        item_ids = {i["item_id"] for i in basvc.ANALYSIS_ITEMS}
        for ch in sc.NINE_CHAPTERS:
            for sid in ch["source_items"]:
                assert sid in item_ids, (
                    f"第{ch['chapter']}章引用了不存在的提取项 {sid}（拆链）")

    def test_every_chapter_has_at_least_one_source_item(self):
        """每个章节都必须至少绑定一个真实提取项（空映射 = 该章永远无内容）。"""
        for ch in sc.NINE_CHAPTERS:
            assert ch["source_items"], f"第{ch['chapter']}章 source_items 为空"

    def test_chapter1_covers_all_hazard_categories(self):
        """第一章（工程概况）的 category_fields 必须覆盖六大类危大。

        category_fields 按危大类别给出差异化字段；漏一个类别 → 该类方案
        的工程概况退化成通用字段，危大特征参数（基坑深度/架体高度…）无从
        提取。六大类是部文附件一的固定集合，正好可以逐项断言。
        """
        ch1 = next(c for c in sc.NINE_CHAPTERS if c["chapter"] == 1)
        covered = set(ch1.get("category_fields") or {})
        missing = {c["id"] for c in sc.HAZARD_CATEGORIES} - covered
        assert not missing, f"第一章缺少的危大类别字段：{sorted(missing)}"

    def test_every_chapter_has_base_fields(self):
        for ch in sc.NINE_CHAPTERS:
            assert ch["base_fields"], f"第{ch['chapter']}章 base_fields 为空"

    def test_category_fields_fail_soft_on_unknown_category(self):
        """未知危大类别不得让字段映射抛异常（fail-soft，不静默丢字段）。"""
        for ch in sc.NINE_CHAPTERS:
            fields = sc.required_fields_for_chapter(ch["key"], "no_such_category")
            assert isinstance(fields, list)

# =========================================================================
# 5. 防回退红线：专项施工方案 ≠ 投标文件
# =========================================================================
class TestAntiRebidRedlines:
    @staticmethod
    def _read(rel: str) -> str:
        return (REPO / rel).read_text("utf-8")

    def test_shared_scope_rules_present(self):
        """目录/正文共享的文件性质红线不得删除（全链路唯一的语义锚点）。

        删除它不会让任何用例失败，但生成的专项方案会重新出现「招标文件与
        技术标准要求」这类整章（2026-09-19 实测取证过 19 处「招标」），
        在评审时被判文不对题 —— 所以必须有一条护栏锁住它。
        """
        src = self._read("app/services/ai/prompts/_shared.py")
        assert "SHARED_SCOPE_RULES" in src
        assert "文件性质红线" in src
        for phrase in ("专项施工方案", "不是投标", "严禁设置投标场景章节",
                       "严禁引用投标术语", "严禁照抄投标件内容"):
            assert phrase in src, f"红线缺失：{phrase}"

    def test_shared_redline_content_is_intact(self):
        """共享红线常量本身的四要素不得被删（唯一事实源本身要有人守）。"""
        from app.services.ai.prompts._shared import SHARED_SCOPE_RULES
        for phrase in ("专项施工方案", "不是投标", "严禁设置投标场景章节",
                       "严禁引用投标术语", "严禁照抄投标件内容", "设计文件"):
            assert phrase in SHARED_SCOPE_RULES, f"红线缺失：{phrase}"

    @pytest.mark.parametrize("rel", [
        "app/services/ai/prompts/outline.py",
        "app/services/ai/prompts/content.py",
    ])
    def test_prompt_modules_document_the_redline(self, rel):
        """两个提示词模块都必须**引用**红线（代码或常量二选一）。

        旧版这里断言「源码里出现 招标文件/投标文件」——那条判据在正文侧
        改成引用常量后必然失败（源码里只有常量名）。判「是否引用」才是稳定
        不变量：无论最终以哪种形式落地，红线都不能从这条链路上消失。
        """
        src = self._read(rel)
        assert "SHARED_SCOPE_RULES" in src, f"{rel} 未引用共享红线"

    # ---- 正文侧：三份措辞 → 单一来源（L-1 收口）--------------------------
    #: 正文两个模板里必须都注入红线
    _CONTENT_KEYS = ("content_generation_system", "content_continue_system")

    def test_content_prompt_imports_shared_constant(self):
        """content.py 必须以**运行时占位符**引用共享红线，且不得再导入期值拷贝。

        ✅ R38（2026-10-03）改写断言方向：原断言锁的是
        ``from ..._shared import SHARED_SCOPE_RULES_BRIEF`` + ``<<SCOPE_RULES>>``，
        即「导入期把出厂值拷进模板」这一实现形态 —— 而该形态本身就是 R38
        修掉的缺陷（用户在提示词编辑器改共享规则时，目录侧 6 个模板生效、
        正文侧 2 个模板完全不变且无告警）。锁实现形态的护栏会阻止修复，
        故改为锁**不变量**。

        ⚠️ 判据一律走 **AST**，不对源码做文本匹配：说明性注释里必然要写
        ``<<SCOPE_RULES>>`` 来记录这段历史，纯文本匹配会被注释自身误伤
        （AGENTS.md §5.7「注释含关键词误伤字面量判定」同构陷阱）。
        """
        import ast
        path = APP / "services" / "ai" / "prompts" / "content.py"
        tree = ast.parse(path.read_text("utf-8"))

        # (1) 不得再 import 红线常量（注册表是唯一事实源）
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module \
                    and node.module.endswith("prompts._shared"):
                imported += [a.name for a in node.names]
        assert "SHARED_SCOPE_RULES_BRIEF" not in imported, (
            "正文侧不得再 import 红线常量（导入期值拷贝 = 用户改不动）")

        # (2) 不得再有把红线常量 replace 进模板的调用
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "replace"):
                continue
            args = [a for a in node.args if isinstance(a, ast.Constant)]
            names = [n for n in ast.walk(node) if isinstance(n, ast.Name)]
            assert not ({"SHARED_SCOPE_RULES_BRIEF"} <= {n.id for n in names}), (
                "line %d：不得在导入期把出厂红线值拷贝进模板"
                "（DB 覆盖将永不生效）" % node.lineno)

        # (3) 注册进模板的正文必须保留运行时占位符
        templates = [n.args[3] for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_reg" and len(n.args) >= 4]
        assert templates, "未解析到任何 _reg 模板（AST 结构变了？）"
        inline_brief = [
            t.lineno for t in templates
            if isinstance(t, ast.Constant) and isinstance(t.value, str)
            and "专项施工方案是**指导现场施工的技术文件**" in t.value
        ]
        assert not inline_brief, (
            "content.py 的模板里又内联了红线正文，行号：%s" % inline_brief)

    def test_scope_rules_brief_is_registered_prompt(self):
        """红线简档必须注册为可编辑提示词（否则编辑器里根本看不到它）。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert "SHARED_SCOPE_RULES_BRIEF" in _ALL_PROMPTS, \
            "简档未注册 → 用户无法在提示词编辑器看到/修改正文侧红线"
        meta = _ALL_PROMPTS["SHARED_SCOPE_RULES_BRIEF"]
        assert meta.get("category") == "共享规则"

    def test_content_prompt_has_no_inline_copy(self):
        """静态护栏：content.py 的**字符串常量**里不得再内联红线正文。

        ⚠️ 必须用 AST 只取 Constant 节点，不能对源码做文本匹配：本文件顶部
        的说明注释里就写着「文件性质红线」这几个字，纯文本匹配会把注释当成
        内联副本而恒失败（AGENTS.md §5.7 记录的「注释含关键词误伤字面量
        判定」同构陷阱）。内联副本一定是**注册进模板的字符串**，注释不是。
        """
        import ast
        path = APP / "services" / "ai" / "prompts" / "content.py"
        tree = ast.parse(path.read_text("utf-8"))

        # 只取「真正会下发给模型的文本」= _reg(key, category, label, template)
        # 的第 4 个位置参数。
        # ⚠️ 不能简单取全部 ast.Constant —— docstring 与注释同样落在这个节点
        #   类型里（本文件的 _wrap_fuzzy_fill docstring 就写着「文件性质红线
        #   简档」），会把说明文字误判成内联副本而恒失败。
        templates = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_reg"):
                continue
            if len(node.args) >= 4:
                templates.append(node.args[3])
        assert templates, "未解析到任何 _reg 模板（AST 结构变了？）"

        inline = [
            t.lineno for t in templates
            if isinstance(t, ast.Constant) and isinstance(t.value, str)
            and ("文件性质红线" in t.value or "本方案是指导" in t.value)
        ]
        assert not inline, (
            "content.py 的注册模板里又内联了红线正文，行号：%s" % inline)

    @pytest.mark.parametrize("key", _CONTENT_KEYS)
    def test_rendered_content_prompt_carries_redline(self, key):
        """注册后的模板必须真的带上红线，且占位符已被替换干净。

        断言**渲染后**的模板而不是源码：占位符替换发生在注册期，只看源码
        会把「常量引用了但替换漏了」这种最危险的情况漏放过去。
        """
        # ✅ R38：必须用**运行时**出口（_cache.get_prompt，DB 优先 + 共享片段
        #    解析），不能用 _registry.get_prompt —— 后者只返回注册表里的裸模板，
        #    不解析 {SHARED_*}，对 outline 侧本来就不展开（{SHARED_SCOPE_RULES}
        #    一直是原样返回）。生产代码一律走前者（render() 即转发到 _cache）。
        from app.services.ai.prompts._cache import get_prompt
        from app.services.ai.prompts._shared import SHARED_SCOPE_RULES_BRIEF
        text = get_prompt(key) or ""
        assert "<<SCOPE_RULES>>" not in text, f"{key} 的占位符未被替换"
        assert "{SHARED_SCOPE_RULES_BRIEF}" not in text, \
            f"{key} 的运行时共享占位符未解析（正文会收到裸占位符）"
        assert "SHARED_SCOPE_RULES_BRIEF"[:20] in text or \
            "文件性质红线" in text, f"{key} 渲染后没有红线内容"
        assert SHARED_SCOPE_RULES_BRIEF in text, \
            f"{key} 渲染后红线内容与出厂简档不一致"
        for term in ("招标文件", "投标文件", "设计文件"):
            assert term in text, f"{key} 渲染后缺少红线要素：{term}"

    def test_scope_rules_cover_same_banned_terms(self):
        """完整版与简档的**禁用术语集合必须一致**。

        拆成两档（正文每章下发一次，完整版纯 token 成本）后最大的风险是
        改一档忘另一档 → 弱模型看到两份不同的禁用清单会挑宽松的执行。
        这里锁住「同一术语集合」，措辞可以各自优化。
        """
        from app.services.ai.prompts._shared import (
            SHARED_SCOPE_RULES,
            SHARED_SCOPE_RULES_BRIEF,
        )
        terms = ("招标文件", "投标文件", "评标办法", "评分标准",
                 "废标条件", "投标须知", "商务条款")
        for t in terms:
            assert t in SHARED_SCOPE_RULES, f"完整版红线缺术语：{t}"
            assert t in SHARED_SCOPE_RULES_BRIEF, f"简档红线缺术语：{t}"
        for t in ("设计文件与合同依据", "经审批的施工组织设计", "施工合同约定"):
            assert t in SHARED_SCOPE_RULES and t in SHARED_SCOPE_RULES_BRIEF, (
                f"替代写法不一致：{t}")

    def test_export_blocks_bidding_terms(self):
        """导出体检必须继续拦截投标场景用语（否则 AI 幻觉混进出稿）。"""
        src = self._read("app/routers/export.py")
        assert "bidding_terms" in src
        for term in ("招标文件", "投标文件", "评标办法", "评分标准", "废标条件"):
            assert term in src, f"导出拦截词缺失：{term}"

    def test_no_cover_pages_of_tender_document(self):
        """导出不得生成投标文件封面 / 投标函 / 报价表。

        实测结论：这些模板在代码中**从未存在**（它们只出现在用户上传的
        招标文件解析产物里，不在导出模块）。本例是防回退护栏 ——
        若有人照参考软件把投标封面加回来，必须先确认定位是否允许。
        """
        src = self._read("app/routers/export.py")
        for term in ("投标函", "报价表", "投标保证金", "投标文件封面"):
            assert term not in src, f"导出出现了投标文件模板：{term}"

