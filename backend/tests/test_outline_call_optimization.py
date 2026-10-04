"""目录生成 AI 调用次数优化（2026-09-21）回归测试。

方案：根目录「目录生成AI调用次数优化方案_20260921.md」
  E = ai_audit_logs.scene 场景归因 + /ai/stats by_scene 聚合 + 运行日志落盘
  B = 目录链路 5 处调用点启用 json_mode + 低温（砍 JSON 修复轮的乘性放大）
  A = 审核+修复合并（review 响应携带 fixed_outline 时直接采用，省独立修复调用）
  C = 编制要求程序化覆盖预检 + 外科式补齐（_check_requirements_coverage /
      _try_outline_patch / _merge_patch_chapters）
  D = 长方案逐章子目录多章合并调用（OUTLINE_CHAPTER_BATCH_SIZE /
      _fetch_unit_children / outline_sublevel_batch_system）

锁定的不变量：
1. 所有目录链路 collect_json_response 调用都带 json_mode=True + temperature=0.2
   + scene（五场景：outline_draft/outline_level1/outline_sublevel/outline_review/outline_fix）；
2. 审核合并修复仅在「结构合法 + 未退化」时采用，否则回退原独立修复调用；
3. 程序化预检「拿不准一律推向缺失」（多走 AI，绝不放过真实缺失）；
4. D 默认 merge_k=1 时与旧行为逐字一致（单章单元委托 _fetch_chapter_children）；
5. 批量单元缺章/失败按章回退单章调用，绝不整批判死。
"""
import inspect
import json

import pytest

from app.routers import sse_handlers as sh
from app.config import Settings


async def _push_stats():
    return None


def _mk_outline():
    return [{"title": "工程概况", "description": "x" * 100, "children": [
        {"title": "工程概况说明", "description": "y" * 100, "children": []}]}]


# ============================================================
# B：5 处调用点 json_mode / temperature / scene（源码级护栏）
# ============================================================
class TestJsonModeCallSites:
    def test_short_draft_and_level1_sites(self):
        src = inspect.getsource(sh.generate_outline)
        assert 'scene="outline_draft"' in src
        assert 'scene="outline_level1"' in src
        assert src.count("json_mode=True") >= 2

    def test_sublevel_sites(self):
        src = inspect.getsource(sh._fetch_chapter_children)
        assert src.count('scene="outline_sublevel"') == 2, "首试+重试两处都要打 scene"
        assert src.count("json_mode=True") == 2

    def test_review_and_fix_sites(self):
        src = inspect.getsource(sh._review_and_fix_outline)
        assert 'scene="outline_review"' in src
        assert 'scene="outline_fix"' in src
        assert "json_mode=True" in src
        assert "temperature=0.2" in src

    def test_patch_and_batch_sites(self):
        assert 'scene="outline_fix"' in inspect.getsource(sh._try_outline_patch)
        assert 'scene="outline_sublevel"' in inspect.getsource(sh._fetch_unit_children)



# ============================================================
# A：审核+修复合并（fixed_outline 直接采用 / 退化回退）
# ============================================================
@pytest.mark.asyncio
class TestMergedReviewFix:
    @pytest.fixture(autouse=True)
    def _isolate_nine_chapter_check(self, monkeypatch):
        """⚠️ 2026-10-02（第二十六轮补救，承接第二十五轮门控放宽）：
        程序化覆盖预检现**恒定参与**（`outline_checkpoint_check` 默认开），
        本组用例测的是 **AI 审核轮合并修复（fixed_outline）** 链路，最小目录
        夹具会被九章检查先行触发外科补齐调用。九章覆盖已由
        tests/test_outline_checkpoint_20261002.py 单独钉住，此处显式关闭属
        **测试范围界定**，不是掩盖缺陷。"""
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)

    def _mk(self, monkeypatch, review_obj, fix_obj=None):
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return review_obj, json.dumps(review_obj, ensure_ascii=False)
            return fix_obj, json.dumps(fix_obj, ensure_ascii=False)

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        return calls

    async def test_fixed_outline_adopted_without_second_call(self, monkeypatch):
        """审核轮携带合法 fixed_outline → 直接采用，不再发起独立修复调用。"""
        review = {"passed": False, "suggestions": ["补监测方案"],
                  "fixed_outline": [
                      {"title": "工程概况", "description": "", "children": []},
                      {"title": "监测方案", "description": "新增", "children": []}]}
        calls = self._mk(monkeypatch, review)
        outline, review_obj = await sh._review_and_fix_outline(
            _mk_outline(), "深基坑", True, "摘要", scheme_name="方案")
        assert len(calls) == 1, "合并修复不得再发起第二次 AI 调用"
        assert review_obj["passed"] is True
        assert review_obj["suggestions"][0] == "✅ 已根据审核意见自动修复（审核轮合并输出）"
        titles = [n["title"] for n in outline]
        assert "监测方案" in titles

    async def test_fixed_outline_preserves_original_descriptions(self, monkeypatch):
        """模型带回的是骨架截断版描述（60 字）→ 合并时必须恢复原文。"""
        review = {"passed": False, "suggestions": ["补监测"],
                  "fixed_outline": [
                      {"title": "工程概况", "description": "x" * 60, "children": [
                          {"title": "工程概况说明", "description": "y" * 60,
                           "children": []}]},
                      {"title": "监测方案", "description": "新章", "children": []}]}
        calls = self._mk(monkeypatch, review)
        outline, _ = await sh._review_and_fix_outline(
            _mk_outline(), "深基坑", True, "摘要", scheme_name="方案")
        assert len(calls) == 1
        assert outline[0]["description"] == "x" * 100, "截断描述必须恢复为原文"
        assert outline[0]["children"][0]["description"] == "y" * 100

    async def test_degraded_fixed_outline_falls_back_to_fix_call(self, monkeypatch):
        """fixed_outline 节点数远低于原目录（退化）→ 回退独立修复调用。"""
        review = {"passed": False, "suggestions": ["重写"],
                  "fixed_outline": [{"title": "示例", "children": []}]}
        # 原目录 5 节点（退化阈值 int(5*0.8)=4），fixed 仅 1 节点 → 判退化
        big_orig = [{"title": f"章{i}", "description": "d", "children": []}
                    for i in range(5)]
        fix = {"outline": [dict(n) for n in big_orig]}  # 5 节点，过退化护栏
        calls = self._mk(monkeypatch, review, fix)
        _outline, review_obj = await sh._review_and_fix_outline(
            big_orig, "深基坑", True, "摘要", scheme_name="方案")
        assert len(calls) == 2, "退化时必须回退独立修复调用"
        assert review_obj["suggestions"][0] == "✅ 已根据审核意见自动修复"

    async def test_invalid_fixed_outline_falls_back(self, monkeypatch):
        """fixed_outline 结构非法（缺 title）→ 回退独立修复调用。"""
        review = {"passed": False, "suggestions": ["补齐"],
                  "fixed_outline": [{"children": []}, {"children": []}]}
        fix = {"outline": _mk_outline()}
        calls = self._mk(monkeypatch, review, fix)
        _outline, review_obj = await sh._review_and_fix_outline(
            _mk_outline(), "深基坑", True, "摘要", scheme_name="方案")
        assert len(calls) == 2
        assert review_obj["passed"] is True

    async def test_no_fixed_outline_keeps_old_two_phase(self, monkeypatch):
        """响应不含 fixed_outline（旧模型/超预算目录）→ 两段式行为不变。"""
        review = {"passed": False, "suggestions": ["补验收"]}
        fix = {"outline": _mk_outline()}
        calls = self._mk(monkeypatch, review, fix)
        _outline, review_obj = await sh._review_and_fix_outline(
            _mk_outline(), "深基坑", True, "摘要", scheme_name="方案")
        assert len(calls) == 2
        assert review_obj["suggestions"][0] == "✅ 已根据审核意见自动修复"




# ============================================================
# C：程序化覆盖预检 + 外科式补齐
# ============================================================
class TestRequirementsCoverage:
    @pytest.fixture(autouse=True)
    def _isolate_nine_chapter_check(self, monkeypatch):
        """⚠️ 2026-10-02（第二十六轮补救）：本组用例测**编制要求条目匹配规则**
        （双向子串/片段/公共子串），需隔离第二十五轮新增的恒定九章检查
        （已由 tests/test_outline_checkpoint_20261002.py 单独钉住）。"""
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)

    def test_split_items_strips_numbering(self):
        items = sh._split_requirement_items(
            "1. 工程概况及特点\n2）施工部署与进度计划\n- 安全保证措施\n短\n")
        assert items == ["工程概况及特点", "施工部署与进度计划", "安全保证措施"]

    def test_covered_by_lcs(self):
        outline = [{"title": "施工进度计划及保证措施", "children": []}]
        covered, missing = sh._check_requirements_coverage(
            "施工进度计划", outline)
        assert covered and missing == []

    def test_covered_by_segment(self):
        """并列条目按片段匹配：「计算书」片段（≥3字）命中「计算书及相关图纸」。"""
        outline = [{"title": "计算书及相关图纸", "children": []}]
        covered, _ = sh._check_requirements_coverage(
            "计算书、相关图纸", outline)
        assert covered

    def test_uncertain_pushes_to_missing(self):
        """保守方向：只有 <4 字公共子串且无 ≥3 字片段命中时判缺失（多走 AI，
        绝不因误判覆盖而放过真实缺失）。「计算书」前后被长修饰包裹时
        （公共子串仅 3 字）仍判缺失 → 走外科补齐/AI 审核。"""
        outline = [{"title": "计算书及相关图纸", "children": []}]
        covered, missing = sh._check_requirements_coverage(
            "深基坑支护设计计算书", outline)
        assert not covered and missing

    def test_missing_reported(self):
        outline = [{"title": "工程概况", "children": []}]
        covered, missing = sh._check_requirements_coverage(
            "监测方案与预警值设置", outline)
        assert not covered and missing == ["监测方案与预警值设置"]

    def test_dangerous_required_chapters(self):
        """危大工程：10 个必备章关键词逐一检查，缺「监测」即报缺失。"""
        outline = [{"title": t, "children": []} for t in (
            "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
            "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸")]
        covered, missing = sh._check_requirements_coverage("", outline, True)
        assert not covered
        assert any("监测" in m for m in missing)


def _dangerous_full_outline():
    return [{"title": t, "children": []} for t in (
        "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
        "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸", "监测方案")]


@pytest.mark.asyncio
class TestProgrammaticPrecheck:
    async def test_covered_skips_ai_review(self, monkeypatch):
        """程序化检查全过 → 0 次 AI 调用，review_mode=programmatic。"""
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(messages)
            return {"passed": True, "suggestions": []}, "{}"

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        outline, review = await sh._review_and_fix_outline(
            _dangerous_full_outline(), "深基坑", True, "摘要",
            requirements="1. 工程概况\n2. 监测方案")
        assert calls == [], "全过时不得发起任何 AI 调用"
        assert review["passed"] is True
        assert review["review_mode"] == "programmatic"

    async def test_missing_triggers_surgical_patch(self, monkeypatch):
        """有缺失 → 仅 1 次外科式小调用，章节按 insert_after 合并。"""
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(kwargs)
            assert validate_fn is sh._outline_patch_validate_fn
            assert kwargs.get("scene") == "outline_fix"
            obj = {"new_chapters": [{
                "title": "监测方案", "description": "监测点布置与预警",
                "insert_after": "安全保证措施",
                "children": [{"title": "监测点布置", "children": []}]}]}
            return obj, json.dumps(obj, ensure_ascii=False)

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        base = [{"title": "工程概况", "children": []},
                {"title": "安全保证措施", "children": []}]
        outline, review = await sh._review_and_fix_outline(
            base, "房建", False, "摘要", requirements="监测方案与预警")
        assert len(calls) == 1, "外科式补齐只应发起 1 次 AI 调用"
        assert review["review_mode"] == "programmatic+surgical"
        titles = [n["title"] for n in outline]
        assert titles.index("监测方案") == titles.index("安全保证措施") + 1

    async def test_patch_failure_falls_back_to_full_review(self, monkeypatch):
        """外科式补齐失败 → 回退完整 AI 审核链路（旧行为）。"""
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(validate_fn)
            if len(calls) == 1:
                raise RuntimeError("AI 挂了")   # 外科式补齐失败
            return {"passed": True, "suggestions": []}, "{}"

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        base = [{"title": "工程概况", "children": []}]
        _outline, review = await sh._review_and_fix_outline(
            base, "房建", False, "摘要", requirements="监测方案与预警")
        assert len(calls) == 2, "补齐失败必须回退到完整 AI 审核"
        assert review["passed"] is True
        assert "review_mode" not in review

    async def test_mode_always_keeps_ai_review(self, monkeypatch):
        """outline_review_mode=always 时即使程序化全过也走 AI 审核（旧行为）。"""
        monkeypatch.setattr(sh, "OUTLINE_REVIEW_MODE", "always")
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(messages)
            return {"passed": True, "suggestions": []}, "{}"

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        await sh._review_and_fix_outline(
            _dangerous_full_outline(), "深基坑", True, "摘要",
            requirements="工程概况")
        assert len(calls) == 1, "always 模式必须照旧走 AI 审核"

    async def test_no_requirements_keeps_ai_review(self, monkeypatch):
        """未填编制要求：无可程序化判定依据 → 照旧 AI 审核。"""
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(messages)
            return {"passed": True, "suggestions": []}, "{}"

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        await sh._review_and_fix_outline(
            _dangerous_full_outline(), "深基坑", True, "摘要", requirements="")
        assert len(calls) == 1

    async def test_merge_patch_appends_when_no_match(self):
        outline = [{"title": "工程概况", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "监测方案", "insert_after": "不存在的章", "children": []}])
        assert [n["title"] for n in merged] == ["工程概况", "监测方案"]
        assert merged[0]["id"] == "1" and merged[1]["id"] == "2"



# ============================================================
# D：长方案逐章子目录多章合并调用
# ============================================================
class TestBatchConfig:
    def test_settings_defaults(self):
        s = Settings()
        assert s.outline_chapter_batch_size == 1, "默认必须保持旧行为（每章一次调用）"
        assert s.outline_review_mode == "auto"

    def test_module_constant_follows_settings(self):
        assert sh.OUTLINE_CHAPTER_BATCH_SIZE == max(
            1, int(Settings().outline_chapter_batch_size))
        assert sh.OUTLINE_REVIEW_MODE == Settings().outline_review_mode

    def test_batch_validator(self):
        assert sh._sublevel_batch_validate_fn({"chapters": [
            {"chapter_id": "1", "outline": []}]}) == []
        assert sh._sublevel_batch_validate_fn({"chapters": []})
        assert sh._sublevel_batch_validate_fn({"chapters": [{"chapter_id": "1"}]})
        assert sh._sublevel_batch_validate_fn({})


@pytest.mark.asyncio
class TestFetchUnitChildren:
    def _mk(self, monkeypatch, ai_results, stopped_fn=None):
        """fake collect：按序消费 ai_results（dict=返回 / Exception=抛出）。"""
        calls = []

        def fake_collect(prompt, validate_fn=None, timeout=None, **kwargs):
            calls.append({"prompt": prompt, "kwargs": kwargs})
            result = ai_results.pop(0) if ai_results else Exception("exhausted")
            if isinstance(result, Exception):
                raise result

            async def _coro():
                return result, "raw"
            return _coro()

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        monkeypatch.setattr(sh, "is_stopped", stopped_fn or (lambda task_id: False))
        return calls

    async def test_single_chapter_unit_delegates(self, monkeypatch):
        """merge_k=1：单章单元委托 _fetch_chapter_children（行为与旧版一致）。"""
        outline = [{"title": "1.1", "children": []}]
        calls = self._mk(monkeypatch, [{"outline": outline}])
        status, per = await sh._fetch_unit_children(
            [(0, {"title": "第1章"})], batch_prompt=[{"role": "system", "content": "x"}],
            single_prompts=None, task_id="t", sem=None, timeout=1,
            push_stats=_push_stats)
        assert status == "ok"
        assert per == [("ok", outline)]
        assert calls[0]["kwargs"]["scene"] == "outline_sublevel"

    async def test_multi_chapter_unit_one_call(self, monkeypatch):
        """merge_k>1：2 章合并为 1 次批量调用（chapter_id 归位）。"""
        batch_obj = {"chapters": [
            {"chapter_id": "1", "outline": [{"title": "1.1", "children": []}]},
            {"chapter_id": "2", "outline": [{"title": "2.1", "children": []}]}]}
        calls = self._mk(monkeypatch, [batch_obj])
        status, per = await sh._fetch_unit_children(
            [(0, {"title": "A"}), (1, {"title": "B"})],
            batch_prompt=[{"role": "system", "content": "batch"}],
            single_prompts=[[{"role": "system", "content": "s1"}],
                            [{"role": "system", "content": "s2"}]],
            task_id="t", sem=None, timeout=1, push_stats=_push_stats)
        assert status == "ok"
        assert len(calls) == 1, "2 章应合并为 1 次 AI 调用"
        assert calls[0]["kwargs"]["scene"] == "outline_sublevel"
        assert per[0] == ("ok", [{"title": "1.1", "children": []}])
        assert per[1] == ("ok", [{"title": "2.1", "children": []}])

    async def test_missing_chapter_falls_back_to_single(self, monkeypatch):
        """批量响应缺章 → 该章回退单章调用（不丢章、不整批判死）。"""
        batch_obj = {"chapters": [
            {"chapter_id": "1", "outline": [{"title": "1.1", "children": []}]}]}
        single_obj = {"outline": [{"title": "2.1", "children": []}]}
        calls = self._mk(monkeypatch, [batch_obj, single_obj])
        status, per = await sh._fetch_unit_children(
            [(0, {"title": "A"}), (1, {"title": "B"})],
            batch_prompt=[{"role": "system", "content": "batch"}],
            single_prompts=[[{"role": "system", "content": "s1"}],
                            [{"role": "system", "content": "s2"}]],
            task_id="t", sem=None, timeout=1, push_stats=_push_stats)
        assert status == "ok"
        assert len(calls) == 2, "批量 1 次 + 缺章单章兜底 1 次"
        assert per[0][0] == "ok" and per[1][0] == "ok"
        assert per[1][1] == [{"title": "2.1", "children": []}]

    async def test_batch_failure_falls_back_per_chapter(self, monkeypatch):
        """批量整体失败（首试+重试）→ 逐章回退单章调用。"""
        calls = self._mk(monkeypatch, [
            Exception("boom1"), Exception("boom2"),          # 批量两试全挂
            {"outline": [{"title": "1.1", "children": []}]},  # 单章兜底1
            {"outline": [{"title": "2.1", "children": []}]},  # 单章兜底2
        ])
        status, per = await sh._fetch_unit_children(
            [(0, {"title": "A"}), (1, {"title": "B"})],
            batch_prompt=[{"role": "system", "content": "batch"}],
            single_prompts=[[{"role": "system", "content": "s1"}],
                            [{"role": "system", "content": "s2"}]],
            task_id="t", sem=None, timeout=1, push_stats=_push_stats)
        assert status == "ok"
        assert len(calls) == 4
        assert all(s == "ok" for s, _ in per)

    async def test_stopped_unit_returns_stopped(self, monkeypatch):
        """停止语义：进入单元前已停止 → unit_status=stopped，不发起 AI 调用。"""
        calls = self._mk(monkeypatch, [], stopped_fn=lambda task_id: True)
        status, per = await sh._fetch_unit_children(
            [(0, {"title": "A"}), (1, {"title": "B"})],
            batch_prompt=[], single_prompts=None,
            task_id="t", sem=None, timeout=1, push_stats=_push_stats)
        assert status == "stopped" and per == []
        assert calls == [], "停止后不得启动 AI 调用"



# ============================================================
# E：scene 审计归因（provider_factory / json_response / usage）
# ============================================================
@pytest.mark.asyncio
class TestSceneAudit:
    async def test_log_audit_writes_scene(self, db_conn):
        """_log_audit 写入的 scene 必须随批量落库持久化。"""
        import app.services.ai.provider_factory as pf
        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = 1e18  # 阻止 10s 兜底自动 flush
        await pf._log_audit("deepseek", "deepseek-chat", "chat", 1.5, True,
                            prompt_tokens=120, completion_tokens=45,
                            scene="outline_review")
        await pf.flush_audit_buffer()
        cur = await db_conn.execute(
            "SELECT scene, prompt_tokens FROM ai_audit_logs")
        row = await cur.fetchone()
        assert row["scene"] == "outline_review"
        assert row["prompt_tokens"] == 120

    async def test_scene_column_exists(self, db_conn):
        """迁移/建表必须包含 scene 列。"""
        cur = await db_conn.execute("PRAGMA table_info(ai_audit_logs)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "scene" in cols

    async def test_collect_json_response_forwards_scene(self, monkeypatch):
        """collect_json_response 必须把 scene 透传给 chat_with_fallback。"""
        from app.services.ai import json_response as jr
        captured = []

        async def fake_chat(messages, **kwargs):
            captured.append(kwargs)
            return '{"outline": []}'

        monkeypatch.setattr(jr, "chat_with_fallback", fake_chat)
        obj, _raw = await jr.collect_json_response(
            [{"role": "system", "content": "x"}], scene="outline_draft")
        assert captured and captured[0].get("scene") == "outline_draft"

    async def test_stats_by_scene(self, db_conn):
        """/ai/stats 返回 by_scene 聚合（仅统计打了 scene 标记的 chat 调用）。"""
        from app.routers.ai_config import usage as usage_mod
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " prompt_tokens, completion_tokens, cached_tokens, duration, success, scene)"
            " VALUES ('sc1','deepseek','m','chat',100,50,0,1.0,1,'outline_draft')")
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " prompt_tokens, completion_tokens, cached_tokens, duration, success, scene)"
            " VALUES ('sc2','deepseek','m','chat',10,5,0,0.5,1,'outline_draft')")
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " duration, success, scene)"
            " VALUES ('sc3','deepseek','m','chat',0.5,1,'')")
        await db_conn.commit()
        res = await usage_mod.ai_stats(days=30, db=db_conn)
        assert "by_scene" in res
        row = [r for r in res["by_scene"] if r["scene"] == "outline_draft"]
        assert row and row[0]["calls"] == 2 and row[0]["tokens"] == 165

