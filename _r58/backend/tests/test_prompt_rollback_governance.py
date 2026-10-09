"""提示词版本回滚（G2）与治理开关默认关闭（G5/G6）回归测试。

✅ 2026-09-24 · 提示词模块遗留问题闭环：
  * G2：旧实现 prompt_audit_logs 只存 SHA-256，哈希无法还原正文 →
        审计只能"看到变了"，无法把改坏的提示词恢复回去。
        本文件锁定「快照写入 → 审计返回 diff/rollbackable → 回滚还原 →
        回滚本身也留痕（可再回滚）」完整链路，以及三类拒绝路径。
  * G5/G6：治理能力默认必须关闭（prompt_context_budget=0 /
        prompt_injection_defense=False），否则会改变所有现存提示词渲染结果。
"""
from __future__ import annotations

import json

import app.config as _cfg
import pytest
from app.routers.prompts import (
    reset_prompt as _reset_route,
)
from app.routers.prompts import (
    rollback_prompt,
)
from app.routers.prompts import (
    update_prompt as _update,
)
from app.services.ai.prompts._cache import reload_prompt_cache
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    get_default_prompt,
)
from app.services.ai.prompts._registry import (
    reset_prompt as _reset,
)
from fastapi import HTTPException

KEY = "outline_short_system"


@pytest.fixture(autouse=True)
def _restore_prompt():
    """每个用例结束后把被测提示词恢复出厂默认并失效缓存，防跨用例污染。"""
    yield
    _reset(KEY)
    reload_prompt_cache()


async def _update_content(db, content: str) -> dict:
    res = await _update(KEY, {"content": content}, db=db)
    assert res["ok"] is True
    return res


# ---------------------------------------------------------------------------
# 一、快照写入与审计元数据
# ---------------------------------------------------------------------------
class TestSnapshotWritten:
    async def test_update_writes_before_and_after_snapshot(self, db_conn):
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n新增要求：{custom_rule}")
        cur = await db_conn.execute(
            "SELECT snapshot_json FROM prompt_audit_logs"
            " WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1", (KEY,))
        row = await cur.fetchone()
        assert row and row["snapshot_json"], "update 审计应写入快照"
        snap = json.loads(row["snapshot_json"])
        assert snap["before"] == default
        assert snap["after"] == default + "\n新增要求：{custom_rule}"

    async def test_snapshot_is_valid_json_and_not_truncated_by_default(self, db_conn):
        """快照必须是合法 JSON（截断只作用于字符串值，不破坏 JSON 本身）。"""
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n补充：{x}")
        cur = await db_conn.execute(
            "SELECT snapshot_json FROM prompt_audit_logs"
            " WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1", (KEY,))
        snap = json.loads((await cur.fetchone())["snapshot_json"])
        assert snap.get("before_truncated") is None
        assert snap.get("after_truncated") is None

    async def test_oversized_snapshot_is_marked_truncated(self, db_conn, monkeypatch):
        """超过 prompt_audit_snapshot_max_chars 的快照必须标记 truncated（防撑爆审计表）。"""
        monkeypatch.setattr(_cfg.settings, "prompt_audit_snapshot_max_chars", 50)
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n补充：{x}")
        cur = await db_conn.execute(
            "SELECT snapshot_json FROM prompt_audit_logs"
            " WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1", (KEY,))
        snap = json.loads((await cur.fetchone())["snapshot_json"])
        assert snap.get("after_truncated") is True
        assert len(snap["after"]) <= 50
        assert snap.get("before_truncated") is True

    async def test_snapshot_disabled_still_records_hash_audit(self, db_conn, monkeypatch):
        """关闭快照（合规场景）后，哈希审计仍然写入（不丢审计行）。"""
        monkeypatch.setattr(_cfg.settings, "prompt_audit_snapshot_enabled", False)
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n补充：{y}")
        cur = await db_conn.execute(
            "SELECT snapshot_json, after_hash FROM prompt_audit_logs"
            " WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1", (KEY,))
        row = await cur.fetchone()
        assert (row["snapshot_json"] or "") == ""
        assert row["after_hash"], "哈希审计不应受快照开关影响"


# ---------------------------------------------------------------------------
# 二、审计列表：diff 与 rollbackable
# ---------------------------------------------------------------------------
class TestAuditListMetadata:
    async def test_list_returns_changes_and_rollbackable(self, db_conn):
        from app.services.audit_service import list_prompt_audit_logs
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n新增：{added_var}")
        res = await list_prompt_audit_logs(db_conn, KEY)
        item = res["items"][0]
        assert item["rollbackable"] is True
        fields = {c["field"] for c in item["changes"]}
        assert "length" in fields
        assert "added_variables" in fields
        assert "snapshot_json" not in item, "原始快照不应回传前端（减小载荷）"

    async def test_legacy_row_without_snapshot_is_not_rollbackable(self, db_conn):
        from app.services.audit_service import list_prompt_audit_logs
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,"
            "variables_before,variables_after,client_ip)"
            " VALUES ('legacy-row', ?, 'update', 'a', 'b', '[]', '[]', '')", (KEY,))
        await db_conn.commit()
        res = await list_prompt_audit_logs(db_conn, KEY)
        item = next(i for i in res["items"] if i["id"] == "legacy-row")
        assert item["rollbackable"] is False
        assert item["changes"] == []


# ---------------------------------------------------------------------------
# 三、回滚端点
# ---------------------------------------------------------------------------
class TestRollbackEndpoint:
    async def test_rollback_restores_previous_content(self, db_conn):
        default = get_default_prompt(KEY)
        changed = default + "\n新增要求：{custom_rule}"
        await _update_content(db_conn, changed)
        cur = await db_conn.execute(
            "SELECT content FROM prompt_templates WHERE key=?", (KEY,))
        assert (await cur.fetchone())["content"] == changed

        cur = await db_conn.execute(
            "SELECT id FROM prompt_audit_logs WHERE prompt_key=?"
            " ORDER BY rowid DESC LIMIT 1", (KEY,))
        audit_id = (await cur.fetchone())["id"]

        res = await rollback_prompt(KEY, {"audit_id": audit_id}, db=db_conn)
        assert res["ok"] is True
        assert res["content"] == default, "回滚应恢复到变更前的出厂默认"
        assert res["removed_variables"] == ["custom_rule"]
        cur = await db_conn.execute(
            "SELECT content FROM prompt_templates WHERE key=?", (KEY,))
        assert (await cur.fetchone())["content"] == default

    async def test_rollback_writes_audit_and_is_itself_rollbackable(self, db_conn):
        """回滚本身也留痕，且可被再次回滚（可撤销链）。"""
        from app.services.audit_service import list_prompt_audit_logs
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n新增：{v1}")
        cur = await db_conn.execute(
            "SELECT id FROM prompt_audit_logs WHERE prompt_key=?"
            " ORDER BY rowid DESC LIMIT 1", (KEY,))
        first_audit = (await cur.fetchone())["id"]
        await rollback_prompt(KEY, {"audit_id": first_audit}, db=db_conn)
        logs = await list_prompt_audit_logs(db_conn, KEY)
        rb = next(i for i in logs["items"] if i["action"] == "rollback")
        assert rb["rollbackable"] is True, "回滚审计行也应带快照（可再回滚）"

    async def test_rollback_missing_audit_id_rejected(self, db_conn):
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(KEY, {}, db=db_conn)
        assert ei.value.status_code == 400

    async def test_rollback_unknown_audit_rejected(self, db_conn):
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(KEY, {"audit_id": "no-such-row"}, db=db_conn)
        assert ei.value.status_code == 404

    async def test_rollback_audit_of_other_key_rejected(self, db_conn):
        """跨 key 取审计行必须拒绝（防止把 A 模板的内容写进 B 模板）。"""
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs (id,prompt_key,action,snapshot_json)"
            " VALUES ('other-key-row', 'content_generation_system', 'update', ?)",
            (json.dumps({"before": "别的模板的内容"}, ensure_ascii=False),))
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(KEY, {"audit_id": "other-key-row"}, db=db_conn)
        assert ei.value.status_code == 404

    async def test_rollback_without_snapshot_rejected(self, db_conn):
        """历史行（只有哈希）→ 400 明确提示。"""
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash)"
            " VALUES ('hash-only', ?, 'update', 'a', 'b')", (KEY,))
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(KEY, {"audit_id": "hash-only"}, db=db_conn)
        assert ei.value.status_code == 400
        assert "哈希" in ei.value.detail

    async def test_rollback_truncated_snapshot_rejected(self, db_conn):
        """截断的快照不是完整提示词，写回去等于制造损坏模板 → 必须拒绝。"""
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs (id,prompt_key,action,snapshot_json)"
            " VALUES ('trunc-row', ?, 'update', ?)",
            (KEY, json.dumps({"before": "截断", "before_truncated": True},
                             ensure_ascii=False)))
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await rollback_prompt(KEY, {"audit_id": "trunc-row"}, db=db_conn)
        assert ei.value.status_code == 400
        assert "截断" in ei.value.detail

    async def test_rollback_updates_runtime_registry(self, db_conn):
        """回滚提交成功后，运行时注册表必须同步更新（与保存/重置同口径）。

        说明：断言内存注册表 _ALL_PROMPTS[key]["content"] 而非 _cache.get_prompt() ——
        后者按 app.db.DB_PATH 直连真实库文件，测试的内存库写入对它不可见
        （见 tests/test_prompt_cache_db_path.py 的说明）。回滚端点对运行时
        的实际影响面就是 update_prompt()（写内存注册表）+ reload_prompt_cache()。
        """
        default = get_default_prompt(KEY)
        await _update_content(db_conn, default + "\n新增：{v2}")
        assert "新增" in _ALL_PROMPTS[KEY]["content"], "保存后注册表应已是新内容"
        cur = await db_conn.execute(
            "SELECT id FROM prompt_audit_logs WHERE prompt_key=?"
            " ORDER BY rowid DESC LIMIT 1", (KEY,))
        audit_id = (await cur.fetchone())["id"]
        await rollback_prompt(KEY, {"audit_id": audit_id}, db=db_conn)
        assert _ALL_PROMPTS[KEY]["content"] == default, \
            "回滚后运行时注册表必须立刻看到旧内容"


# ---------------------------------------------------------------------------
# 四、治理开关默认关闭（G5 / G6 向后兼容护栏）
# ---------------------------------------------------------------------------
class TestGovernanceDefaultOff:
    def test_budget_off_by_default(self):
        assert _cfg.settings.prompt_context_budget == 0

    def test_injection_defense_off_by_default(self):
        assert _cfg.settings.prompt_injection_defense is False

    def test_snapshot_on_by_default(self):
        assert _cfg.settings.prompt_audit_snapshot_enabled is True

    def test_strict_variables_off_by_default(self):
        assert _cfg.settings.prompt_strict_variables is False

    def test_sse_budget_helper_returns_text_unchanged_when_off(self):
        from app.routers.sse_handlers import _apply_prompt_context_budget
        text = "【全局事实变量（唯一可信数据源）】：\n" + "开挖深度10.5m。" * 500
        assert _apply_prompt_context_budget(text) == text

    def test_sse_guard_helper_returns_text_unchanged_when_off(self):
        from app.routers.sse_handlers import _guard_external_material
        text = "【项目概述】：正常资料内容"
        assert _guard_external_material(text) == text

    def test_sse_budget_helper_activates_when_enabled(self, monkeypatch):
        from app.routers.sse_handlers import _apply_prompt_context_budget
        monkeypatch.setattr(_cfg.settings, "prompt_context_budget", 200)
        text = ("【项目概述】：" + "很长很长的资料。" * 500
                + "\n【全局事实变量（唯一可信数据源）】：\n" + "开挖深度10.5m。" * 100)
        out = _apply_prompt_context_budget(text)
        assert len(out) < len(text), "开启后超长上下文应被削减"
        assert "开挖深度10.5m" in out, "全局事实是最关键上下文，应被保留"

    def test_sse_guard_helper_activates_when_enabled(self, monkeypatch):
        from app.routers.sse_handlers import _guard_external_material
        monkeypatch.setattr(_cfg.settings, "prompt_injection_defense", True)
        out = _guard_external_material("【项目概述】：正常资料内容")
        assert out.startswith("【以下为外部资料原文"), "开启后应加资料边界围栏"

    def test_facts_and_knowledge_segments_are_labelled(self):
        """正文 user 上下文的全局事实/知识库必须用【标签】形式（预算分配器的前提）。

        ✅ 2026-09-28（T-1 收口后修复）：正文 user 上下文装配自 sse_handlers
        下沉到 ``services/content_runtime.build_chapter_user_content`` 后，
        旧断言（``inspect.getsource(sse_handlers)`` 必须含标签字面量）失去
        锚点、必然失败。改为**行为级 parity 断言**：
          1) 真实装配函数产出的 user 上下文必须包含事实/知识库两个标签；
          2) 两个标签必须被预算分配器 ``CONTEXT_PRIORITY`` 识别为高优先级段
             （而非默认最低优先级 9）—— 否则开启 ``prompt_context_budget`` 后
             该段会被最先削减。
        """
        from app.services.content_runtime import build_chapter_user_content
        from app.services.prompt_governance import _DEFAULT_PRIORITY, segment_priority_of
        user_content = build_chapter_user_content(
            scheme={"name": "基坑支护专项方案", "type": "危大工程专项方案"},
            project_brief="（项目概述摘要）",
            content_scope="土方开挖与支护",
            parent_chain="1、工程概况",
            parent_points=["1、工程概况：场地与基坑概况"],
            sibling_lines="1.1 工程概况；1.2 编制依据",
            section_number="1.1",
            leaf={"title": "工程概况", "description": "基坑规模与周边环境", "id": 1},
            word_budget=1200,
            word_budget_hint="",
            prev_sibling_summary="",
            facts_text="- **开挖深度**: 10.5m；-** 支护形式**: 排桩加锚索",
            eff_standard="precise",
            knowledge_text="企业管理制度与工艺要点。",
        )
        assert "【全局事实变量（唯一可信数据源）】" in user_content
        assert "【项目知识库素材】" in user_content
        # parity：标签必须被预算分配器识别（非默认最低优先级），否则开预算会被最先砍
        assert segment_priority_of("全局事实变量（唯一可信数据源）") == 0
        assert segment_priority_of("项目知识库素材") == 3
        assert segment_priority_of("全局事实变量（唯一可信数据源）") != _DEFAULT_PRIORITY
