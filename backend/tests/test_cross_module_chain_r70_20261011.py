# -*- coding: utf-8 -*-
"""六模块跨模块数据链治理护栏（2026-10-11 · R70）。

覆盖本轮**逐模块探索 → 实证 → 修复**的 7 项缺陷，全部为跨模块数据链问题：
  ① 正文生成落库把上一轮 D1 编排决策（plan_json）静默抹成空串（模块4 数据丢失）
  ② 正文终态 ``done`` 与断线重挂 checkpoint 口径分裂（模块4 → 前端）
  ③ 导出缓存指纹漏掉服务端开关 ``body_subheading_demote_with_children``（模块6）
  ④ 导出「全局事实交付门控」引用未定义名 ``scheme_row`` → 门控 100% 空转（模块5→6）
  ⑤ ``list_facts`` 信任调用方 project_id → 可越项目读共享事实（模块3）
  ⑥ ``outline_quality`` 配置读取的 fail-soft 反噬成 NameError（模块2）
  ⑦ 落库上限 ``MAX_PARSED_CHARS`` 两份副本、读侧那份是死代码（模块1）
（⑧ 作用域谓词第四份副本在 ``scheme_fingerprint`` —— 护栏已并入 R69 文件）

测试策略：能跑真行为的就跑真行为（SQLite / 真实函数调用），
否则退到 **ast.Constant / 源码结构断言**，且断言锚点选「改回旧写法必然失败」的位置。
零 AI、零网络。
"""
import ast
import inspect
import sqlite3

import pytest

BACKEND = __file__.rsplit("\\", 2)[0]


def _src_of(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _app_path(*parts: str) -> str:
    import os
    return os.path.join(BACKEND, "app", *parts)


# ===========================================================================
# ① plan_json 不得被空串抹除（模块4 · 数据丢失）
# ===========================================================================
class TestPlanJsonNotWiped:
    """D1 离散编排的编排决策必须能跨轮存活。"""

    # 与 sse_handlers._persist_section 内 UPDATE 逐字一致（改一处必须改两处）
    _SQL_FRAGMENT = "plan_json = CASE WHEN ? <> '' THEN ? ELSE plan_json END"

    def test_persist_sql_preserves_previous_plan_on_empty(self):
        src = _src_of(_app_path("routers", "sse_handlers.py"))
        assert self._SQL_FRAGMENT in src, (
            "落库 UPDATE 必须在 plan_json 为空时保留原列（避免被空串抹除）")
        assert "SET content=?, word_count=?, word_status=?" in src

    def test_no_unconditional_plan_json_assignment(self):
        """旧写法 ``plan_json=? ``（无条件覆盖）不得再出现。"""
        src = _src_of(_app_path("routers", "sse_handlers.py"))
        assert "plan_json=? \"" not in src, (
            "仍存在无条件 SET plan_json=?，空串会抹掉上一轮编排决策")

    def test_sqlite_semantics_empty_keeps_old_value(self):
        """真跑一遍该 SQL：空串保留旧值，非空覆盖。"""
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE sections (id TEXT PRIMARY KEY, content TEXT, "
            "word_count INTEGER, word_status TEXT, status TEXT, updated_at TEXT, "
            "last_generation_standard TEXT, last_generation_report TEXT, "
            "plan_json TEXT)")
        con.execute("INSERT INTO sections (id, plan_json) VALUES ('s1', '{\"keep\":1}')")
        upd = ("UPDATE sections SET content=?, word_count=?, word_status=?, "
               "status='generated', updated_at=?, "
               "last_generation_standard=?, last_generation_report=?, "
               + self._SQL_FRAGMENT + " WHERE id=?")
        # 空串 → 保留
        con.execute(upd, ("正文", 10, "ok", "t", "std", "{}", "", "", "s1"))
        assert con.execute("SELECT plan_json FROM sections WHERE id='s1'").fetchone()[0] \
            == '{"keep":1}', "空 plan_json 必须保留上一轮编排决策"
        # 非空 → 覆盖
        con.execute(upd, ("正文2", 12, "ok", "t", "std", "{}", '{"new":2}', '{"new":2}', "s1"))
        assert con.execute("SELECT plan_json FROM sections WHERE id='s1'").fetchone()[0] \
            == '{"new":2}', "非空 plan_json 必须正常覆盖"
        con.close()


# ===========================================================================
# ② 终态 done 与 checkpoint 同口径（模块4 → 前端）
# ===========================================================================
class TestTerminalDoneAlignment:
    def test_completed_done_uses_success_count(self):
        from app.routers import sse_handlers
        src = inspect.getsource(sse_handlers.generate_content)
        block = src[src.index("completed_payload = {"):]
        block = block[:block.index("yield f\"data:")]
        assert "'done': len(done_ids)" in block
        assert "'done': total" not in block

    def test_checkpoint_done_increments_only_on_success(self):
        from app.routers import sse_handlers
        src = inspect.getsource(sse_handlers.generate_content)
        assert '_ckpt["done"] = _ckpt.get("done", 0) + 1' in src


# ===========================================================================
# ③ 导出指纹必须包含服务端开关（模块6 · 缓存失效）
# ===========================================================================
class TestExportFingerprintCoversServerSwitch:
    def _fingerprint(self) -> str:
        from app.routers.export import _content_fingerprint
        prep = {
            "config": {}, "fe_codes": [], "chart_fp": [], "sections": [],
            "scheme": {}, "fe_images": {}, "image_config": {},
            "global_facts": [], "appendix_sources": [], "global_facts_status": {},
        }
        return _content_fingerprint(prep)[1]

    def test_demote_switch_changes_fingerprint(self, monkeypatch):
        """切换 body_subheading_demote_with_children 必须改变指纹（否则命中旧产物）。"""
        from app.config import settings
        monkeypatch.setattr(settings, "body_subheading_demote_with_children",
                            True, raising=False)
        fp_on = self._fingerprint()
        monkeypatch.setattr(settings, "body_subheading_demote_with_children",
                            False, raising=False)
        fp_off = self._fingerprint()
        assert fp_on != fp_off, (
            "切换正文子标题降级开关后指纹未变 → 会命中降级前的旧缓存产物")

    def test_fingerprint_source_contains_demote_key(self):
        src = _src_of(_app_path("routers", "export.py"))
        assert '"subheading_demote": _demote_on' in src


# ===========================================================================
# ④ 导出全局事实门控不得再引用未定义名（模块5→6 · 交付门控空转）
# ===========================================================================
class TestExportFactsGateDefined:
    def test_scheme_row_no_longer_referenced(self):
        # ⚠️ 判据必须锚 **ast.Name**（真实代码引用），不能锚源码全文 ——
        #    本轮修复注释里就写着这个名字，全文匹配必然自撞（§5.14 同族教训）。
        tree = ast.parse(_src_of(_app_path("routers", "export.py")))
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "scheme_row"]
        assert hits == [], (
            f"export.py 仍引用从未赋值的 scheme_row（行 {hits}）—— NameError 会被 "
            "try 吞掉，全局事实交付门控静默失效")

    def test_gate_reuses_single_source_project_lookup(self):
        from app.routers import export
        src = inspect.getsource(export.collect_export_issues)
        assert "resolve_scheme_project_id" in src, "必须走服务层单一出口反查项目"
        assert "if ba_pid:" in src, "门控必须以反查到的项目 ID 为条件"
        assert "FACT_SCOPE_PREDICATE" in src, "作用域必须走单一事实源常量"


# ===========================================================================
# ⑤ list_facts 不得信任调用方 project_id（模块3 · 越项目读）
# ===========================================================================
class TestListFactsScopeHardening:
    def test_no_direct_trust_of_caller_project_id(self):
        from app.routers import global_facts as gf
        src = inspect.getsource(gf.list_facts)
        assert "real_pid = project_id.strip()" not in src, (
            "不得直接信任调用方传入的 project_id 展开共享事实作用域")
        assert "_scheme_project_id" in src, "必须反查方案实际所属项目"

    def test_mismatch_is_overridden_and_logged(self):
        src = _src_of(_app_path("routers", "global_facts.py"))
        assert "list_facts 作用域校正" in src, (
            "传入 project_id 与反查值不一致时必须校正并留 WARNING 日志")


# ===========================================================================
# ⑥ outline_quality 配置读取不得反噬成 NameError（模块2）
# ===========================================================================
class TestOutlineQualitySettingsFailSafe:
    def _out(self):
        return [{"id": "1", "title": "工程概况", "level": 1,
                 "children": [{"id": "1.1", "title": "编制依据", "level": 2}]}]

    def test_no_bare_settings_reference_outside_try(self):
        src = _src_of(_app_path("services", "outline_quality.py"))
        tree = ast.parse(src)
        # `from app.config import settings` 走的是 ast.alias，**不会**产生 ast.Name，
        # 故剩下的每个 Name 都是业务代码里的真实引用。
        # 但 try 块内的引用是**受保护**的（那正是集中读取配置的地方），
        # 只有落在 try 之外的才会在 import 失败时变成 NameError。
        guarded = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
                   if isinstance(n, ast.Try)]

        def _in_try(lineno: int) -> bool:
            return any(a <= lineno <= b for a, b in guarded)

        names = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and n.id == "settings"
                 and not _in_try(n.lineno)]
        assert not names, (
            f"outline_quality.py 在 try 保护范围外仍于 {names} 行裸引用 settings；"
            "配置导入失败时 except 分支不绑定该名字 → NameError，"
            "目录质量校验整体崩溃（fail-soft 反噬）")

    def test_settings_import_failure_does_not_crash(self, monkeypatch):
        """``from app.config import settings`` **本身失败**时不得抛 NameError。

        这正是旧实现的崩溃路径：`except` 只设置了 check_fmt 而没绑定 settings，
        下方的 `getattr(settings, …)` 随即 NameError。
        （注意：仅让属性访问失败是**不够**的 —— `getattr(x, k, default)` 会吞掉
        AttributeError 返回默认值，旧写法照样不崩；必须让 import 失败。）
        """
        import sys
        import types

        from app.services import outline_quality as oq

        fake = types.ModuleType("app.config")  # 没有 settings 属性
        monkeypatch.setitem(sys.modules, "app.config", fake)
        rep = oq.check_outline_continuity(self._out())  # 不得抛 NameError
        assert isinstance(rep, dict) and "ok" in rep
        assert "generic_titles" in rep, "配置不可用时泛化标题检测应回退为开启"

    def test_generic_title_check_still_active_by_default(self):
        from app.services import outline_quality as oq
        rep = oq.check_outline_continuity(self._out())
        # 返回结构：issues 的各类键被 **展开到顶层**，同时有 issue_counts 汇总
        assert "generic_titles" in rep, (
            "泛化标题检测默认必须仍在跑（配置读取集中化后不得被跳过）")
        assert "generic_titles" in rep.get("issue_counts", {}), (
            "issue_counts 必须包含 generic_titles 计数")


# ===========================================================================
# ⑦ MAX_PARSED_CHARS 单一事实源（模块1）
# ===========================================================================
class TestParsedCharsSingleSource:
    def test_alias_points_to_write_side(self):
        from app.routers import global_facts as gf
        from app.routers import global_facts_docs as gfd
        assert gf.MAX_PARSED_CHARS == gfd.MAX_PARSED_CHARS
        assert gf.MAX_PARSED_CHARS >= 400_000

    def test_literal_defined_only_in_docs(self):
        def _defs(path):
            n = 0
            for node in ast.walk(ast.parse(_src_of(path))):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "MAX_PARSED_CHARS":
                            n += 1
            return n

        assert _defs(_app_path("routers", "global_facts.py")) == 0
        assert _defs(_app_path("routers", "global_facts_docs.py")) == 1


# ===========================================================================
# ⑧ 跨模块桥接的安全关键标记必须与写侧/列表同口径（模块3 → 模块5）
# ===========================================================================
class TestSafetyCriticalBridgeParity:
    """``load_resolved_facts_for_scope`` 是预检/审核/合规/一致性修复的统一入口，
    ``is_safety_critical`` 若只用库列，历史行与手工行（列默认 0）会系统性漏标。"""

    def test_bridge_uses_heuristic_not_only_column(self):
        from app.services import facts_extractor as fe
        src = inspect.getsource(fe.load_resolved_facts_for_scope)
        # 旧写法：直接 bool(r.get("is_safety_critical") or 0)
        assert '"is_safety_critical": bool(r.get("is_safety_critical") or 0)' not in src
        assert "_bridge_safety_critical(" in src, "必须走统一口径的判定函数"

    def test_bridge_takes_heuristic_as_authoritative_floor(self):
        """启发式命中而库列为 0 时，结果必须仍为 True（漏标即安全漏报）。"""
        from app.services.facts_extractor import _bridge_safety_critical

        # 库列为 0、但名称命中安全关键白名单 → 必须判 True
        row = {"is_safety_critical": 0, "title": "基坑开挖深度",
               "content": "基坑开挖深度 8.5m", "fact_key": "", "id": "f1"}
        assert _bridge_safety_critical(row, "基坑开挖深度", "8.5m") is True, (
            "启发式命中的安全关键事实不得因库列为 0 而被漏标")

    def test_bridge_falls_back_to_column_when_heuristic_raises(self):
        """启发式异常时回退库列，绝不丢掉已有信号（fail-soft 方向正确）。"""
        from app.services.facts_extractor import _bridge_safety_critical

        class _Boom(dict):
            def get(self, k, d=None):
                if k == "title":
                    raise RuntimeError("boom")
                return dict.get(self, k, d)

        row = _Boom({"is_safety_critical": 1, "content": "", "fact_key": "", "id": "f2"})
        assert _bridge_safety_critical(row, "", "") is True

    def test_bridge_matches_list_facts_predicate_shape(self):
        """两侧必须都是「启发式 or 列」，而不是一侧 or、一侧只看列。"""
        from app.routers import global_facts as gf
        from app.services import facts_extractor as fe
        gf_src = inspect.getsource(gf.list_facts)
        fe_src = inspect.getsource(fe._bridge_safety_critical)
        assert "is_safety_critical_name(" in gf_src
        assert "is_safety_critical_name(" in fe_src
        assert "or col_val" in fe_src, "桥接必须以库列作为补充信号（or 关系）"


# ===========================================================================
# ⑨ 进度模型常量单一事实源（技术债 P0-1）
# ===========================================================================
class TestProgressModelSingleSource:
    """sse_handlers 曾把已抽到 content_progress 的 5 个常量又各定义一份。"""

    _NAMES = ("_SECTION_PHASE_MAX", "_STAGE_FILL_MAX", "_STAGE_LABELS",
              "_STAGE_MODEL", "_STATS_PUSH_INTERVAL")

    def test_no_local_redefinition_in_sse_handlers(self):
        tree = ast.parse(_src_of(_app_path("routers", "sse_handlers.py")))
        bad = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name) and t.id in self._NAMES]
        assert bad == [], f"sse_handlers 不得再自行定义进度常量：{bad}"

    def test_values_are_identical_objects(self):
        """必须是**同一个对象**（别名），不是「值相等的两份」。"""
        from app.routers import sse_handlers as sh
        from app.services import content_progress as cp
        pairs = [(sh._STAGE_MODEL, cp.STAGE_MODEL),
                 (sh._STAGE_FILL_MAX, cp.STAGE_FILL_MAX),
                 (sh._STAGE_LABELS, cp.STAGE_LABELS),
                 (sh._SECTION_PHASE_MAX, cp.SECTION_PHASE_MAX),
                 (sh._STATS_PUSH_INTERVAL, cp.STATS_PUSH_INTERVAL)]
        for a, b in pairs:
            assert a == b, "两侧取值必须相等"
        assert sh._STAGE_MODEL is cp.STAGE_MODEL
        assert sh._STAGE_LABELS is cp.STAGE_LABELS

    def test_stage_model_shape_preserved(self):
        """收敛不得改变模型形状（span 合计 1.0、阶段间 base+span 连续）。"""
        from app.routers.sse_handlers import _STAGE_MODEL
        assert sum(m["span"] for m in _STAGE_MODEL.values()) == pytest.approx(1.0)
        assert (_STAGE_MODEL["persist"]["base"]
                + _STAGE_MODEL["persist"]["span"]) == pytest.approx(1.0)


# ===========================================================================
# ⑩ 总检指纹只算一次（技术债 P1-4）
# ===========================================================================
class TestOverviewFingerprintComputedOnce:
    def test_compute_accepts_fingerprint_param(self):
        from app.routers import compliance as cpl
        sig = inspect.signature(cpl._readiness_overview_compute)
        assert "fingerprint" in sig.parameters
        # 默认 None → 既有调用方（review_autofix 三处）行为逐字不变
        assert sig.parameters["fingerprint"].default is None

    def test_overview_passes_it_down(self):
        from app.routers import compliance as cpl
        src = inspect.getsource(cpl.readiness_overview)
        assert "fingerprint=fingerprint" in src, (
            "readiness_overview 必须把已算好的指纹传下去（否则仍是两次全表扫描）")

    def test_persisted_fingerprint_reuses_param(self):
        from app.routers import compliance as cpl
        src = inspect.getsource(cpl._readiness_overview_compute)
        assert "fingerprint is not None" in src, (
            "落库指纹必须优先复用传入值，杜绝「缓存键用 A、落库用 B」的口径分裂")


# ===========================================================================
# ⑪ AI 源读取失败必须可见（技术债 P1-6）
# ===========================================================================
class TestFailedSourcesSurfaced:
    def test_failed_sources_in_payload(self):
        from app.routers import compliance as cpl
        src = inspect.getsource(cpl._readiness_overview_compute)
        assert '"failed_sources": failed_sources' in src

    def test_all_five_ai_sources_report_failure(self):
        """六路来源中五路走 try/except，每一路失败都要上报，不得只打日志。"""
        src = _src_of(_app_path("routers", "compliance.py"))
        for name in ("export_check", "compliance", "consistency",
                     "consistency_scan", "expert_review"):
            assert f'failed_sources.append("{name}")' in src, (
                f"{name} 读取失败未上报，前端无从得知该来源未参与评分")

    def test_stale_ai_sources_semantics_untouched(self):
        """stale_ai_sources 仍只表示「正文已变 → 结论过期」，不得混入失败。"""
        src = _src_of(_app_path("routers", "compliance.py"))
        assert 'stale_ai_sources.append("compliance")' in src
        # 同一个来源会分别进入两个键，语义不重叠
        assert "failed_sources: list[str] = []" in src


# ===========================================================================
# ⑫ 预检引擎故障条目显式化（技术债 P1-8）
# ===========================================================================
class TestPreflightEngineErrorFlagged:
    def test_engine_error_fields_present(self):
        src = _src_of(_app_path("services", "preflight_engine.py"))
        assert '"engine_error": True' in src
        assert '"checker": checker.__name__' in src

    def test_rule_id_stays_empty_for_dedup(self):
        """异常条目必须保持 rule_id 为空 —— 空 rule_id **不参与去重**，
        多个检查项同时异常时才会逐条保留（改成固定规则号会被塌缩成一条）。"""
        from app.services import preflight_engine as pe
        src = inspect.getsource(pe.run_preflight)
        assert '"rule_id": ""' in src
        assert "if not rid:" in src, "空 rule_id 保留全部的分支必须还在"

    def test_failure_is_logged_with_stack(self):
        src = _src_of(_app_path("services", "preflight_engine.py"))
        assert "执行异常（降级为 engine_error 条目" in src
        assert "exc_info=True" in src


# ===========================================================================
# ⑬ 重试函数不得残留不可达尾巴（技术债 P2-13）
# ===========================================================================
class TestRetryDbLockedNoDeadTail:
    def test_no_unreachable_final_call(self):
        # ⚠️ 判据必须锚 **ast.Return**（真实代码），不能锚源码全文 ——
        #    本轮修复注释里逐字写着 `return await factory()`，全文匹配必自撞
        #    （与 export.py 的 scheme_row 同一类陷阱，§5.14 同族）。
        from app.routers import sse_checkpoint as sc
        tree = ast.parse(inspect.getsource(sc._retry_db_locked))
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Return)
               and isinstance(n.value, ast.Await)
               and isinstance(n.value.value, ast.Call)
               and isinstance(n.value.value.func, ast.Name)
               and n.value.value.func.id == "factory"]
        # 循环体内的 return 是正常成功路径，只禁止**循环之后**的尾巴
        loop_line = next(n.end_lineno for n in ast.walk(tree)
                         if isinstance(n, ast.For))
        tail = [ln for ln in bad if ln > loop_line]
        assert tail == [], (
            f"重试循环之后仍存在 return await factory()（行 {tail}）—— "
            "与 docstring「总尝试次数 = 1 + max_retries」矛盾，且该行不可达")

    def test_attempts_count_matches_docstring(self):
        from app.routers import sse_checkpoint as sc
        src = inspect.getsource(sc._retry_db_locked)
        assert "for i in range(attempts + 1)" in src
        assert "总尝试次数 = 1 + max_retries" in src


# ===========================================================================
# ⑭ 目录深度：硬上限 vs 规范告警线的语义边界（技术债 P2-15）
# ===========================================================================
class TestOutlineDepthSemantics:
    def test_hard_cap_is_single_source(self):
        from app.services.outline_utils import MAX_OUTLINE_DEPTH
        assert MAX_OUTLINE_DEPTH == 3

        def _defs(rel):
            n = 0
            for node in ast.walk(ast.parse(_src_of(_app_path(*rel)))):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "MAX_OUTLINE_DEPTH":
                            n += 1
            return n

        assert _defs(("services", "outline_utils.py")) == 1
        assert _defs(("services", "outline_reorganize.py")) == 0, (
            "outline_reorganize 曾重新定义过该常量，不得再出现第二份")

    def test_alert_line_is_not_below_hard_cap(self):
        """规范告警线必须 ≥ 落库硬上限，否则合法目录会被判违规（误报）。"""
        from app.services.outline_utils import MAX_OUTLINE_DEPTH
        from app.services.outline_quality import OUTLINE_MAX_DEPTH
        assert OUTLINE_MAX_DEPTH >= MAX_OUTLINE_DEPTH, (
            f"告警线 {OUTLINE_MAX_DEPTH} 低于硬上限 {MAX_OUTLINE_DEPTH}："
            "落库允许的深度会被规范检查判为越界")

    def test_known_limitation_documented(self):
        """告警在裁剪后调用时恒不触发 —— 这一已知限制必须写在常量注释里。"""
        src = _src_of(_app_path("services", "outline_quality.py"))
        assert "恒不触发" in src, (
            "必须记录：OUTLINE_MAX_DEPTH 告警仅在裁剪前的原始目录上才有意义")


# ===========================================================================
# ⑮ 审核进度三字段语义（技术债 P1-10）
# ===========================================================================
class TestReviewProgressSemantics:
    def test_progress_is_alias_of_approved_progress(self):
        """progress 是 approved_progress 的**遗留别名**（通过率），
        不是「评审进度」；审完率必须看 reviewed_progress。"""
        from app.routers import review as rv
        src = inspect.getsource(rv.review_summary)
        assert "round(approved / total * 100, 1)" in src
        assert src.count("round(approved / total * 100, 1)") == 2, (
            "progress 与 approved_progress 必须同为通过率口径（遗留别名关系）")
        assert "round(reviewed / total * 100, 1)" in src

    def test_reviewed_all_means_all_reviewed_not_all_approved(self):
        from app.routers import review as rv
        src = inspect.getsource(rv.review_summary)
        assert "reviewed_all = total > 0 and reviewed == total" in src
        assert "reviewed = total - counts.get(\"\", 0)" in src


# ===========================================================================
# ⑯ SSE 事件名口径：stats / ping 必须被全部消费点一致接受（技术债 P2-11）
# ===========================================================================
class TestStatsPingProtocolParity:
    def test_backend_can_emit_stats(self):
        """后端确实会推 stats（task_registry.broadcast）—— 不是死事件。"""
        src = _src_of(_app_path("services", "ai", "task_registry.py"))
        assert '"event": "stats"' in src

    def test_all_frontend_consumers_accept_both(self):
        """同一页面三处 SSE 消费点的事件白名单必须一致，
        否则 stats 只在某些入口被静默丢弃（症状只在一个入口出现，极难排查）。"""
        import os
        fe = os.path.join(BACKEND, "..", "frontend", "src")
        page = os.path.join(fe, "pages", "SchemeWorkbenchPage.tsx")
        with open(page, encoding="utf-8") as f:
            src = f.read()
        both = src.count('evt.event === "stats" || evt.event === "ping"')
        # 目录分支 + 正文分支 + facts 分支 = 3 处（钩子里的 useContentGeneration 另算）
        assert both >= 3, (
            f"仍有消费点只认 ping 不认 stats（当前双认处数={both}）")
        assert 'evt.event === "ping")' not in src.replace(
            'evt.event === "stats" || evt.event === "ping")', ""), (
            "存在只匹配 ping 的分支，stats 到达时被静默丢弃")


# ===========================================================================
# 端到端：六模块关键常量/判据「单一事实源」总闸
# ===========================================================================
class TestSingleSourceCrossModule:
    def test_scope_predicate_defined_once(self):
        """作用域谓词只允许在 facts_extractor 定义一次（R69 + R70 第四份副本）。"""
        hits = []
        import os
        app_dir = _app_path()
        for root, _, files in os.walk(app_dir):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                tree = ast.parse(_src_of(p))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if isinstance(t, ast.Name) and \
                                    t.id in ("FACT_SCOPE_PREDICATE",
                                             "FACT_SCOPE_SHARED_FRAGMENT"):
                                hits.append((os.path.relpath(p, app_dir), t.id))
        assert len(hits) == 2, f"作用域谓词定义处应为 2（半句+全范围），实际：{hits}"
        assert all("facts_extractor" in h[0] for h in hits), hits

    def test_every_predicate_call_site_passes_project_id(self):
        """``FACT_SCOPE_PREDICATE`` = ``(scheme_id=? OR (project_id=? AND …))``，
        第二个占位符必须是**真实 project_id**。凡把它接在原先「project_id=
        (SELECT … WHERE id=?)」子查询写法的调用点后面、却沿用旧参数元组的，
        都会让共享半句恒不成立（静默丢掉项目共享事实）。全仓扫描防再犯。
        """
        # ⚠️ 判据必须锚 **ast.Tuple**（真实调用实参），不能锚源码子串 ——
        #    本轮修复注释里就描述了这个错误写法，子串扫描必自撞
        #    （与 export.py 的 scheme_row、_retry_db_locked 的死代码尾巴同族）。
        offenders = []
        import os
        app_dir = _app_path()
        for root, _, files in os.walk(app_dir):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py") or fn == "facts_extractor.py":
                    continue
                p = os.path.join(root, fn)
                src = _src_of(p)
                if "FACT_SCOPE_PREDICATE" not in src:
                    continue
                tree = ast.parse(src)
                for n in ast.walk(tree):
                    if not isinstance(n, ast.Tuple):
                        continue
                    elts = n.elts
                    if (len(elts) == 2 and all(isinstance(e, ast.Name) for e in elts)
                            and all(e.id == "scheme_id" for e in elts)):
                        offenders.append((os.path.relpath(p, app_dir), n.lineno))
        assert offenders == [], (
            f"以下位置把 scheme_id 当 project_id 传给作用域谓词：{offenders}")

    def test_all_modules_share_predicate_values(self):
        from app.services.facts_extractor import (FACT_SCOPE_PREDICATE,
                                                  FACT_SCOPE_SHARED_FRAGMENT)
        from app.routers.global_facts import _FACT_SCOPE_PREDICATE
        assert _FACT_SCOPE_PREDICATE == FACT_SCOPE_PREDICATE
        assert FACT_SCOPE_SHARED_FRAGMENT in FACT_SCOPE_PREDICATE
