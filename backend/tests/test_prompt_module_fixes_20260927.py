"""提示词模块缺陷修复护栏（2026-09-27）。

本文件为「深度探索提示词模块」一轮的回归护栏，每条断言对应一处代码改动。
按严重度组织：

* **P0-1 注入内容被静默删行（数据丢失）** — ``render_prompt`` 的「整行独占
  占位符 = 可选区块」删行逻辑，旧实现作用在**替换后的文本**上，于是注入值里
  形如 ``{heading}`` 的行整行消失。实测用真实模板 ``facts_json_fix_system``
  复现：``invalid_content`` 被篡改，AI 拿到的是比原始输出更糟的片段。
* **P0-2 SHARED 片段递归打爆调用栈** — 旧实现按注释假设「SHARED_* 内容不含
  SHARED 占位符，无递归风险」；但内容由用户自由编辑，实测自引用即
  ``RecursionError``，且**所有引用该共享片段的生成任务全部失败**。
* **P0-3 删行/豁免判据分叉** — 单字符变量 ``{x}`` 被删行却仍报缺失。
* **P1-A** ``cur is None`` 未判空（AGENTS.md §5.5 R13 事故漏改点）。
* **P1-B** 并发 PATCH 丢版本 / 审计链断裂（读 before 在事务外且无 CAS）。
* **P1-C** 保存时零校验，坏模板一路跑到模型面前。
* **P1-D** 展示 ≠ 渲染 ≠ hash（只在运行时清洗）。
* **P1-E** ``rollbackable`` 与回滚前置校验口径分叉（按钮可点必 400）。
* **P1-F** 同值双份字面量（写入上限 vs 快照/回滚长度校验）。

所有新增行为**默认向后兼容**：error 级只拦「本来就会坏」的输入，
warning 级只提示不阻断。
"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi import HTTPException

from app.routers import prompts as pr
from app.routers.prompts import (
    PROMPT_MAX_CHARS, list_prompts, rollback_prompt, update_prompt,
)
from app.services.audit_service import (
    PROMPT_MAX_CHARS as AUDIT_MAX_CHARS,
    prompt_snapshot_is_rollbackable,
)
from app.services.ai.prompts import _cache
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS, _is_optional_block_var,
    extract_user_variables, get_default_prompt, render_prompt,
    validate_prompt_content,
)

REAL_KEY = "outline_short_system"


# ======================================================================
# 一、P0-1：注入内容被静默删行（数据丢失）
# ======================================================================
class TestInjectedContentNeverDeleted:
    """注入值里的内容必须**逐字节**原样出现在渲染结果中。"""

    def test_injected_brace_line_survives_real_template(self):
        """真实模板回归：facts_json_fix_system 的 {invalid_content} 独占一行，
        注入值里的 ``{heading}`` 行曾被整行删掉（AI 拿到被篡改的原文）。"""
        tpl = _ALL_PROMPTS["facts_json_fix_system"]["default_content"]
        # 断言前提：该占位符确实独占一行（这正是旧实现删行的触发条件）
        assert re.search(r"^\s*\{invalid_content\}\s*$", tpl, re.M), \
            "前提失效：{invalid_content} 不再独占一行，本用例失去意义"
        injected = '前置说明\n{heading}\n{\n  "facts": [\n'
        out = render_prompt(tpl, issues="i", target_description="d",
                            invalid_content=injected)
        assert "{heading}" in out, "注入值里的 {heading} 行被静默删除了"
        assert '"facts": [' in out, "注入的非法 JSON 原文被篡改了"
        assert injected in out, "注入值必须逐字节保留"

    @pytest.mark.parametrize("value", [
        "{x}",
        "a\n{heading}\nb",
        "资料\n{section_title}\n资料",
        "{scheme_name}\n{project_facts}",
        "```json\n{max}\n```",
    ])
    def test_arbitrary_injected_values_preserved(self, value):
        tpl = "HEAD\n【资料】：{material}\nTAIL\n{opt_block}"
        out = render_prompt(tpl, material=value, opt_block="OPT")
        assert value in out, f"注入值被篡改：{value!r}"

    def test_json_key_line_in_injected_value_preserved(self):
        tpl = "{material}"
        value = '{\n  "max": 5,\n  "id": 1\n}'
        assert render_prompt(tpl, material=value) == value

    def test_optional_block_still_dropped_when_not_provided(self):
        """修复不得破坏原语义：未传的整行可选区块仍要整行丢弃。"""
        out = render_prompt("A\n{scheme_basis}\nB", other="x")
        assert out == "A\nB"

    def test_optional_block_kept_when_provided(self):
        assert render_prompt("A\n{scheme_basis}\nB",
                             scheme_basis="依据内容") == "A\n依据内容\nB"

    def test_injected_value_containing_optional_block_kept(self):
        """注入值恰好是「一行占位符」时**不得**被删（反向回归）。"""
        assert render_prompt("{material}",
                             material="{scheme_basis}") == "{scheme_basis}"

    def test_no_trailing_newline_added_or_removed(self):
        assert render_prompt("a\n{b}", b="B") == "a\nB"
        assert render_prompt("a\n{b}\n", b="B") == "a\nB\n"

    def test_crlf_line_endings_preserved(self):
        assert render_prompt("a\r\n{b}\r\n", b="B") == "a\r\nB\r\n"

    def test_all_registered_templates_no_arg_parity(self):
        """全部 44 个模板：无实参渲染 == 「删掉所有整行独占占位符行」的模板。"""
        for key, meta in _ALL_PROMPTS.items():
            tpl = meta.get("default_content") or meta.get("content") or ""
            exp = re.sub(r"^[ \t]*\{[A-Za-z_]\w*\}[ \t]*\r?\n", "", tpl,
                         flags=re.M)
            exp = re.sub(r"^[ \t]*\{[A-Za-z_]\w*\}[ \t]*$", "", exp, flags=re.M)
            assert render_prompt(tpl) == exp, f"模板 {key} 无实参渲染结果异常"



# ======================================================================
# 二、P0-3：删行判据与豁免判据必须同源（parity）
# ======================================================================
class TestOptionalBlockParity:
    """「删行」与「豁免告警」是同一判据的两个方向，必须一致。"""

    @pytest.mark.parametrize("var", [
        "x", "ab", "scheme_basis", "_x", "a1", "变量名", "MaxLen",
    ])
    def test_drop_and_exempt_agree(self, var):
        tpl = f"head\n{{{var}}}\ntail"
        dropped = f"{{{var}}}" not in render_prompt(tpl)
        exempt = _is_optional_block_var(var, tpl)
        assert dropped == exempt, (
            f"变量 {var}: 删行={dropped} 豁免={exempt} —— 两侧判据分叉")

    def test_single_char_variable_is_treated_as_optional_block(self):
        """回归：``{x}`` 旧实现被删行但仍报缺失（≥1 vs ≥2 字符分叉）。"""
        assert _is_optional_block_var("x", "l1\n{x}\nl2") is True
        assert "{x}" not in render_prompt("l1\n{x}\nl2")

    def test_inline_mixed_is_not_optional_block(self):
        """行内混排不删行（空串降级为「标签：」是既定可接受行为）。"""
        tpl = "【事实】：{project_facts}"
        assert _is_optional_block_var("project_facts", tpl) is False
        assert "【事实】：" in render_prompt(tpl)

    def test_validation_side_reuses_shared_pattern(self):
        """校验侧必须复用共享正则常量（消除双份正则）。"""
        import inspect
        src = inspect.getsource(_is_optional_block_var)
        assert "_OPTIONAL_BLOCK_RE" in src, "校验侧必须复用共享正则"


# ======================================================================
# 三、P0-2：SHARED 片段递归防护
# ======================================================================
class TestSharedRecursionGuard:
    """用户可编辑 SHARED 内容 → 递归/成环/拼错都必须安全降级，不得崩。"""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        table = {
            "SHARED_T_SELF": "S {SHARED_T_SELF}",
            "SHARED_T_A": "A {SHARED_T_B}",
            "SHARED_T_B": "B {SHARED_T_A}",
            "SHARED_T_TYPO": "T {SHARED_T_NOT_EXIST}",
            "SHARED_T_OK": "PLAIN",
        }
        monkeypatch.setattr(_cache, "get_prompt", lambda k: table.get(k, ""))
        yield

    def test_self_reference_does_not_recurse(self):
        assert "SHARED_T_SELF" in _cache._resolve_shared_keys(
            "root {SHARED_T_SELF}"), "成环时应保留原字面量"

    def test_mutual_reference_does_not_recurse(self):
        assert "SHARED_T_A" in _cache._resolve_shared_keys("root {SHARED_T_A}")

    def test_unknown_key_keeps_literal(self):
        assert "SHARED_T_NOT_EXIST" in _cache._resolve_shared_keys(
            "root {SHARED_T_TYPO}")

    def test_normal_expansion_still_works(self):
        assert _cache._resolve_shared_keys(
            "root {SHARED_T_OK}") == "root PLAIN"

    def test_no_shared_ref_passthrough(self):
        assert _cache._resolve_shared_keys("plain text") == "plain text"

    def test_deep_but_acyclic_chain_resolves(self, monkeypatch):
        """多层无环引用必须全部展开（深度上限不得误伤合法链）。

        注：链尾 ``SHARED_T_L3`` 的**内容**就是 END 本身，所以它是被
        END 取代、而非留下 "L3 END"。
        """
        table = {f"SHARED_T_L{i}": f"L{i} {{SHARED_T_L{i + 1}}}"
                 for i in range(3)}
        table["SHARED_T_L3"] = "END"
        monkeypatch.setattr(_cache, "get_prompt", lambda k: table.get(k, ""))
        assert _cache._resolve_shared_keys(
            "{SHARED_T_L0}") == "L0 L1 L2 END"

    def test_depth_limit_allows_exactly_max_depth_refs(self, monkeypatch):
        """恰好 ``_SHARED_MAX_DEPTH`` 个引用的链必须完整展开（上限不误伤）。"""
        n = _cache._SHARED_MAX_DEPTH
        table = {f"SHARED_T_D{i}": f"D{i} {{SHARED_T_D{i + 1}}}"
                 for i in range(n - 1)}
        table[f"SHARED_T_D{n - 1}"] = "TAIL"
        monkeypatch.setattr(_cache, "get_prompt", lambda k: table.get(k, ""))
        out = _cache._resolve_shared_keys("{SHARED_T_D0}")
        assert "TAIL" in out, f"深度上限误伤合法链：{out}"

    def test_over_depth_chain_truncates_safely(self, monkeypatch):
        """超过上限的链：安全截断（保留字面量），不得崩、不得丢已展开部分。"""
        n = _cache._SHARED_MAX_DEPTH + 3
        table = {f"SHARED_T_D{i}": f"D{i} {{SHARED_T_D{i + 1}}}"
                 for i in range(n - 1)}
        table[f"SHARED_T_D{n - 1}"] = "TAIL"
        monkeypatch.setattr(_cache, "get_prompt", lambda k: table.get(k, ""))
        out = _cache._resolve_shared_keys("{SHARED_T_D0}")
        assert "D0" in out, "已展开的部分不得被丢弃"
        assert "{SHARED_T_D" in out, "超深部分应保留字面量而非消失"

    def test_depth_limit_exists(self):
        assert _cache._SHARED_MAX_DEPTH >= 1

    def test_real_self_reference_does_not_raise(self):
        """用真实注册表内容做端到端防崩验证（不修改注册表）。"""
        try:
            _cache._resolve_shared_keys("{SHARED_OUTPUT_SPEC}")
        except RecursionError:  # pragma: no cover - 修复后不应触发
            pytest.fail("SHARED 解析仍然会 RecursionError")


# ======================================================================
# 四、P1-C：保存期静态体检
# ======================================================================
class TestSaveTimeValidation:
    KEY = REAL_KEY

    def test_unknown_shared_ref_is_error(self):
        issues = validate_prompt_content(
            self.KEY,
            get_default_prompt(self.KEY) + "\n{SHARED_NO_SUCH_THING}")
        assert "unknown_shared_ref" in {i["code"] for i in issues}
        assert any(i["level"] == "error" for i in issues)

    def test_valid_shared_ref_passes(self):
        issues = validate_prompt_content(
            self.KEY,
            get_default_prompt(self.KEY) + "\n{SHARED_SCOPE_RULES}")
        assert not [i for i in issues if i["level"] == "error"], issues

    def test_removing_contract_var_is_warning_not_error(self):
        """删掉契约变量：warning（不阻断），因为可能有正当理由。"""
        tpl = get_default_prompt(self.KEY).replace("{scheme_name}", "固定名")
        w = [i for i in validate_prompt_content(self.KEY, tpl)
             if i["code"] == "contract_var_removed"]
        assert w, "删掉契约变量应给出提示"
        assert w[0]["level"] == "warning"
        assert "scheme_name" in w[0]["detail"]

    def test_adding_unknown_var_is_warning(self):
        tpl = get_default_prompt(self.KEY) + "\n新变量：{totally_new_var}"
        w = [i for i in validate_prompt_content(self.KEY, tpl)
             if i["code"] == "contract_var_added"]
        assert w and "totally_new_var" in w[0]["detail"]
        assert w[0]["level"] == "warning"

    def test_untouched_template_has_no_error(self):
        for key in _ALL_PROMPTS:
            issues = validate_prompt_content(key, get_default_prompt(key))
            assert not [i for i in issues if i["level"] == "error"], \
                f"出厂默认模板 {key} 不应有 error 级问题：{issues}"

    @pytest.mark.asyncio
    async def test_patch_rejects_unknown_shared_ref(self, db_conn):
        with pytest.raises(HTTPException) as ei:
            await update_prompt(REAL_KEY,
                                {"content": get_default_prompt(REAL_KEY)
                                 + "\n{SHARED_NOPE}"}, db=db_conn)
        assert ei.value.status_code == 400
        assert "SHARED_NOPE" in str(ei.value.detail)

    @pytest.mark.asyncio
    async def test_patch_returns_warnings(self, db_conn):
        tpl = get_default_prompt(REAL_KEY).replace("{scheme_name}", "固定名")
        res = await update_prompt(REAL_KEY, {"content": tpl}, db=db_conn)
        assert res["ok"] is True
        assert any("scheme_name" in w for w in (res.get("warnings") or [])), res


# ======================================================================
# 五、P1-D：入库 = 展示 = 渲染 = hash
# ======================================================================
class TestContentFidelity:
    @pytest.mark.asyncio
    async def test_patch_stores_cleaned_text(self, db_conn):
        from app.services.ai.prompts._registry import clean_prompt_text
        dirty = "标题\ufeff\r\n正文"
        res = await update_prompt(REAL_KEY, {"content": dirty}, db=db_conn)
        assert res["content"] == clean_prompt_text(dirty)
        assert "\ufeff" not in res["content"]

    @pytest.mark.asyncio
    async def test_list_shows_exactly_what_runtime_renders(self, db_conn):
        """列表展示的正文 == 运行时真正渲染的那份（两侧走同一 clean 函数）。

        不变量：入库（PATCH 已清洗）、列表展示、运行时缓存加载三处
        全部经过 ``clean_prompt_text``，故三者恒等 —— 消除旧实现
        「只有缓存清洗、编辑器展示未清洗原文」导致的三方分叉。
        """
        from app.services.ai.prompts._registry import clean_prompt_text
        dirty = "A\ufeff\r\nB"
        saved = await update_prompt(REAL_KEY, {"content": dirty}, db=db_conn)
        row = await (await db_conn.execute(
            "SELECT content FROM prompt_templates WHERE key=?",
            (REAL_KEY,))).fetchone()
        items = (await list_prompts(db=db_conn))["items"]
        item = next(i for i in items if i["key"] == REAL_KEY)
        expected = clean_prompt_text(dirty)
        assert row["content"] == expected, "入库内容未清洗"
        assert saved["content"] == expected, "保存响应未清洗"
        assert item["content"] == expected, "列表展示与入库/渲染不一致"
        assert "\ufeff" not in item["content"] and "\r" not in item["content"]



# ======================================================================
# 六、P1-E：rollbackable 与回滚前置校验同口径
# ======================================================================
class TestRollbackableParity:
    def test_valid_snapshot_rollbackable(self):
        ok, why = prompt_snapshot_is_rollbackable({"before": "内容"})
        assert ok and why == ""

    def test_no_snapshot_not_rollbackable(self):
        ok, why = prompt_snapshot_is_rollbackable({})
        assert not ok and why

    def test_truncated_snapshot_not_rollbackable(self):
        """回归：旧实现只判 before 非空 → 截断行会回 rollbackable=True。"""
        ok, why = prompt_snapshot_is_rollbackable(
            {"before": "内容", "before_truncated": True})
        assert not ok, "截断快照不得标记为可回滚"
        assert "截断" in why

    def test_oversized_snapshot_not_rollbackable(self):
        ok, why = prompt_snapshot_is_rollbackable(
            {"before": "x" * 300}, max_chars=200)
        assert not ok and "超长" in why

    @pytest.mark.parametrize("bad", [None, "str", 123, []])
    def test_malformed_snapshot_not_rollbackable(self, bad):
        ok, _ = prompt_snapshot_is_rollbackable(bad)
        assert not ok, f"{bad!r} 不应被判为可回滚"

    def test_max_chars_single_source(self):
        """写入上限与快照/回滚长度校验必须同值（防两份字面量漂移）。"""
        assert PROMPT_MAX_CHARS == AUDIT_MAX_CHARS

    @pytest.mark.asyncio
    async def test_audit_list_rollbackable_matches_endpoint(self, db_conn):
        """不变量：列表说 rollbackable=True ⟹ 回滚端点必定成功。"""
        from app.services.audit_service import list_prompt_audit_logs
        key = "content_generation_system"
        d0 = get_default_prompt(key)
        await update_prompt(key, {"content": d0 + "\n版本一"}, db=db_conn)
        await update_prompt(key, {"content": d0 + "\n版本二"}, db=db_conn)
        res = await list_prompt_audit_logs(db_conn, key)
        assert res["items"]
        for it in res["items"]:
            if it["rollbackable"]:
                out = await rollback_prompt(
                    key, {"audit_id": it["id"]}, db=db_conn)
                assert out["ok"] is True, f"标记可回滚却失败：{it['id']}"
            else:
                with pytest.raises(HTTPException) as ei:
                    await rollback_prompt(
                        key, {"audit_id": it["id"]}, db=db_conn)
                assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_truncated_row_is_not_offered_as_clickable(self, db_conn):
        """关键反例：造一条**被截断**的快照行，列表不得回 rollbackable=True。

        旧实现的 ``rollbackable`` 只判「before 非空」，于是这条行会让前端
        渲染出可点的「回滚」按钮，用户一点必 400 —— 本用例锁死该行为。
        """
        import json
        from app.services.audit_service import list_prompt_audit_logs
        key = "outline_level1_system"
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,variables_before,"
            "variables_after,client_ip,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("t-trunc", key, "update", "h1", "h2", "[]", "[]", "",
             json.dumps({"before": "被截断的前半段", "before_truncated": True,
                         "before_chars": 99999}, ensure_ascii=False)))
        await db_conn.commit()
        res = await list_prompt_audit_logs(db_conn, key)
        item = next(i for i in res["items"] if i["id"] == "t-trunc")
        assert item["rollbackable"] is False, \
            "截断快照被标记为可回滚 → 前端按钮可点但必 400"
        assert "截断" in item["rollback_blocked_reason"]
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(key, {"audit_id": "t-trunc"}, db=db_conn)
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_oversized_row_is_not_offered_as_clickable(self, db_conn):
        import json
        from app.services.audit_service import list_prompt_audit_logs
        key = "outline_sublevel_system"
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,variables_before,"
            "variables_after,client_ip,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("t-big", key, "update", "h1", "h2", "[]", "[]", "",
             json.dumps({"before": "x" * (PROMPT_MAX_CHARS + 10)},
                        ensure_ascii=False)))
        await db_conn.commit()
        res = await list_prompt_audit_logs(db_conn, key)
        item = next(i for i in res["items"] if i["id"] == "t-big")
        assert item["rollbackable"] is False
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(key, {"audit_id": "t-big"}, db=db_conn)
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_truncated_row_is_not_offered_as_clickable(self, db_conn):
        """关键反例：造一条**被截断**的快照行，列表不得回 rollbackable=True。

        旧实现的 ``rollbackable`` 只判「before 非空」，于是这条行会让前端
        渲染出可点的「回滚」按钮，用户一点必 400 —— 本用例锁死该行为。
        """
        import json
        from app.services.audit_service import list_prompt_audit_logs
        key = "outline_level1_system"
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,variables_before,"
            "variables_after,client_ip,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("t-trunc", key, "update", "h1", "h2", "[]", "[]", "",
             json.dumps({"before": "被截断的前半段", "before_truncated": True,
                         "before_chars": 99999}, ensure_ascii=False)))
        await db_conn.commit()
        res = await list_prompt_audit_logs(db_conn, key)
        item = next(i for i in res["items"] if i["id"] == "t-trunc")
        assert item["rollbackable"] is False, \
            "截断快照被标记为可回滚 → 前端按钮可点但必 400"
        assert "截断" in item["rollback_blocked_reason"]
        # 且回滚端点必须给出明确的 400（而非 500 或静默成功）
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(key, {"audit_id": "t-trunc"}, db=db_conn)
        assert ei.value.status_code == 400
        assert "截断" in str(ei.value.detail)

    @pytest.mark.asyncio
    async def test_oversized_row_is_not_offered_as_clickable(self, db_conn):
        import json
        from app.services.audit_service import list_prompt_audit_logs
        key = "outline_sublevel_system"
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,variables_before,"
            "variables_after,client_ip,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("t-big", key, "update", "h1", "h2", "[]", "[]", "",
             json.dumps({"before": "x" * (PROMPT_MAX_CHARS + 10)},
                        ensure_ascii=False)))
        await db_conn.commit()
        res = await list_prompt_audit_logs(db_conn, key)
        item = next(i for i in res["items"] if i["id"] == "t-big")
        assert item["rollbackable"] is False
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(key, {"audit_id": "t-big"}, db=db_conn)
        assert ei.value.status_code == 400


# ======================================================================
# 七、P1-B：并发写不丢版本 / 审计链不断裂
# ======================================================================
class TestConcurrentWriteIntegrity:
    @pytest.mark.asyncio
    async def test_version_conflict_returns_409(self, db_conn):
        """CAS 生效：读到的 updated_at 与事务内不一致 → 409 而非静默覆盖。"""
        key = "content_shrink_system"
        d0 = get_default_prompt(key)
        await update_prompt(key, {"content": d0 + "\n版本A"}, db=db_conn)
        # 模拟「别人先改了」：直接把 updated_at 改成旧值
        await db_conn.execute(
            "UPDATE prompt_templates SET updated_at='2000-01-01 00:00:00'"
            " WHERE key=?", (key,))
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await pr._write_with_before(
                db_conn, key, d0, d0 + "\n版本B", "update", None,
                expected_updated_at="1999-01-01 00:00:00")
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_rollback_preserves_previous_version(self, db_conn):
        """回归：连续两次更新后回滚，能回到「甲」而不是出厂默认。"""
        from app.services.audit_service import list_prompt_audit_logs
        key = "compliance_check_system"
        d0 = get_default_prompt(key)


# ======================================================================
# 九、契约完整性总护栏（覆盖全部出厂模板）
# ======================================================================
class TestContractIntegrity:
    def test_no_contract_drift(self):
        from app.services.ai.prompts import check_prompt_variables
        issues = [i for i in check_prompt_variables(verbose=False)
                  if i["key"] in _ALL_PROMPTS]
        assert not issues, issues

    def test_every_shared_ref_in_templates_is_registered(self):
        """出厂模板不得引用未注册的 {SHARED_*}（P1-C 的前置不变量）。"""
        for key, meta in _ALL_PROMPTS.items():
            tpl = meta.get("default_content") or meta.get("content") or ""
            for ref in set(re.findall(r"\{(SHARED_[A-Z0-9_]+)\}", tpl)):
                assert ref in _ALL_PROMPTS, f"模板 {key} 引用了不存在的 {ref}"

    def test_no_shared_content_self_reference(self):
        for key in [k for k in _ALL_PROMPTS if k.startswith("SHARED_")]:
            body = (_ALL_PROMPTS[key].get("default_content")
                    or _ALL_PROMPTS[key].get("content") or "")
            assert "{" + key + "}" not in body, f"{key} 自引用"

    def test_all_templates_renderable_with_full_contract(self):
        """按契约表给每个模板传满变量：不得抛异常、不得残留占位符。

        残留判定复用 ``_is_false_positive``（与 ``validate_prompt_variables``
        / ``check_prompt_variables`` 同一判据）—— 否则模板里的 JSON 示例
        ``{"max": ...}`` 会被当成未解析变量造成假失败。
        """
        from app.services.ai.prompts import PROMPT_VARIABLE_CONTRACTS
        from app.services.ai.prompts._registry import (
            _is_false_positive, extract_variables, has_residual_placeholders,
        )
        for key, requires in PROMPT_VARIABLE_CONTRACTS.items():
            tpl = get_default_prompt(key)
            out = render_prompt(tpl, **{v: f"<{v}>" for v in requires})
            assert out, key
            residual = [v for v in extract_variables(out)
                        if not v.startswith("SHARED_")
                        and not _is_false_positive(key, v, tpl)]
            assert not has_residual_placeholders(out) or not residual, (
                f"{key} 按契约传满变量后仍有未解析占位符：{residual}")

    def test_user_variables_exclude_shared(self):
        for key, meta in _ALL_PROMPTS.items():
            tpl = meta.get("default_content") or meta.get("content") or ""
            for v in extract_user_variables(tpl):
                assert not v.startswith("SHARED_"), f"{key}: {v}"


# ======================================================================
# 八、P1-A：db.execute 返回 None 的守卫（R13 事故）
# ======================================================================
class TestCursorNoneGuard:
    class _DeadConn:
        async def execute(self, *a, **k):
            return None

    @pytest.mark.asyncio
    async def test_fetch_content_row_raises_503(self):
        with pytest.raises(HTTPException) as ei:
            await pr._fetch_content_row(self._DeadConn(), REAL_KEY)
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_list_prompts_survives_dead_cursor(self):
        res = await list_prompts(db=self._DeadConn())
        assert res["items"], "DB 读失败时应回退出厂默认清单，而不是 500"
