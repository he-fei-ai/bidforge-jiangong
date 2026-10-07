"""R48（2026-10-06）护栏：prompts 运行时指标接口（任务 2）+ 硬编码→DB 一键同步接口（任务 4）。

B1（任务 2 · ``GET /system/prompt-metrics`` / ``POST .../reset``）：
  * GET 返回结构包含 5 个顶层计数器字段 + ``registered_keys`` 非空；
  * 调一次 ``get_prompt(key)`` 后 ``render_total[key] >= 1``；
  * reset 后五个计数器全部归零。
B2（任务 4 · ``POST /admin/prompts/sync-from-code``）：
  * 空表 → 全部 key inserted；
  * 二次调用 → 全部 in_sync、inserted=0（幂等）；
  * 后台改一个 key → drift，且 DB 行**不被覆盖**；
  * force=true → drift 行被硬编码覆盖；
  * 重复调用 inserted=0。

设计：直接 async 调用路由处理函数（与本仓既有 ``test_prompt_rollback_governance``
同型），用 ``db_conn`` 内存库；计数器是进程内全局，autouse fixture 每个用例前后
各 reset 一次做隔离。
"""
from __future__ import annotations

import pytest

from app.routers.prompts import (
    get_prompt_metrics,
    reset_prompt_metrics,
    sync_prompts_from_code,
)
from app.services.ai.prompts import _metrics
from app.services.ai.prompts._cache import get_prompt
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    clean_prompt_text,
    get_default_prompt,
    register_lazy_prompts,
)

#: 一个已注册、有内容、便于做 drift 对比的真实模板 key。
TEST_KEY = "outline_short_system"


@pytest.fixture(autouse=True)
def _reset_metrics():
    """每个用例前后各清零一次进程内计数器，避免跨用例污染。"""
    _metrics.reset()
    yield
    _metrics.reset()


async def _total_registered() -> int:
    register_lazy_prompts()
    return len(_ALL_PROMPTS)


# ---------------------------------------------------------------------------
# B1：运行时指标接口
# ---------------------------------------------------------------------------
class TestPromptMetrics:
    async def test_top_level_fields_and_registered_keys(self):
        snap = await get_prompt_metrics()
        for field in ("render_total", "render_errors",
                      "token_budget_truncated", "repair_triggered",
                      "ai_failure_by_scene", "registered_keys"):
            assert field in snap, f"GET /prompt-metrics 缺顶层字段 {field}"
        assert isinstance(snap["render_total"], dict)
        assert isinstance(snap["render_errors"], dict)
        assert isinstance(snap["registered_keys"], list)
        assert snap["registered_keys"], "registered_keys 必须非空"

    async def test_render_increments_render_total(self):
        get_prompt(TEST_KEY)
        snap = await get_prompt_metrics()
        assert snap["render_total"].get(TEST_KEY, 0) >= 1, (
            "调一次 get_prompt 后 render_total[key] 应 >= 1（埋点缺失？）")

    async def test_reset_zeroes_all_counters(self):
        get_prompt(TEST_KEY)
        before = await get_prompt_metrics()
        assert before["render_total"].get(TEST_KEY, 0) >= 1
        await reset_prompt_metrics()
        snap = await get_prompt_metrics()
        assert snap["render_total"] == {}
        assert snap["render_errors"] == {}
        assert snap["token_budget_truncated"] == {}
        assert snap["repair_triggered"] == {}
        assert snap["ai_failure_by_scene"] == {}
        # registered_keys 是注册表快照，不受 reset 影响
        assert snap["registered_keys"]


# ---------------------------------------------------------------------------
# B2：硬编码 → DB 一键同步
# ---------------------------------------------------------------------------
class TestSyncFromCode:
    async def test_empty_table_inserts_all_registered(self, db_conn):
        n = await _total_registered()
        res = await sync_prompts_from_code(force=False, db=db_conn)
        assert res["summary"]["inserted"] == n
        assert res["summary"]["in_sync"] == 0
        assert res["summary"]["drift"] == 0
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM prompt_templates")
        row = await cur.fetchone()
        assert row["c"] == n, "空表同步后 DB 行数应等于注册模板数"

    async def test_second_call_all_in_sync_no_duplicate_insert(self, db_conn):
        n = await _total_registered()
        await sync_prompts_from_code(force=False, db=db_conn)
        res2 = await sync_prompts_from_code(force=False, db=db_conn)
        assert res2["summary"]["inserted"] == 0, "二次调用不应再插入"
        assert res2["summary"]["in_sync"] == n
        assert res2["summary"]["drift"] == 0
        assert res2["summary"]["overwritten"] == 0
        cur = await db_conn.execute("SELECT COUNT(*) AS c FROM prompt_templates")
        row = await cur.fetchone()
        assert row["c"] == n, "二次调用不得产生重复行"

    async def test_drift_not_overwritten_without_force(self, db_conn):
        await sync_prompts_from_code(force=False, db=db_conn)
        # 模拟用户在后台改过该 key：直接往 DB 写一个与硬编码不同的内容
        mutated = clean_prompt_text(get_default_prompt(TEST_KEY)) + "\n# 用户后台手工修改"
        await db_conn.execute(
            "UPDATE prompt_templates SET content=? WHERE key=?", (mutated, TEST_KEY))
        await db_conn.commit()
        res = await sync_prompts_from_code(force=False, db=db_conn)
        items = [d for d in res["details"] if d["key"] == TEST_KEY]
        assert items and items[0]["status"] == "drift"
        assert res["summary"]["drift"] >= 1
        # drift 行必须回传三方哈希/时间戳
        for k in ("code_hash", "db_hash", "db_modified_at"):
            assert k in items[0], f"drift 明细缺 {k}"
        # 关键：DB 行不得被硬编码覆盖
        cur = await db_conn.execute(
            "SELECT content FROM prompt_templates WHERE key=?", (TEST_KEY,))
        row = await cur.fetchone()
        assert row["content"] == mutated, (
            "force=false 时 drift 行不得被硬编码覆盖（会丢失用户后台编辑）")

    async def test_force_overwrites_drift(self, db_conn):
        await sync_prompts_from_code(force=False, db=db_conn)
        mutated = clean_prompt_text(get_default_prompt(TEST_KEY)) + "\n# 用户后台手工修改"
        await db_conn.execute(
            "UPDATE prompt_templates SET content=? WHERE key=?", (mutated, TEST_KEY))
        await db_conn.commit()
        res = await sync_prompts_from_code(force=True, db=db_conn)
        items = [d for d in res["details"] if d["key"] == TEST_KEY]
        assert items and items[0]["status"] == "overwritten"
        assert res["summary"]["overwritten"] >= 1
        cur = await db_conn.execute(
            "SELECT content FROM prompt_templates WHERE key=?", (TEST_KEY,))
        row = await cur.fetchone()
        assert row["content"] == clean_prompt_text(get_default_prompt(TEST_KEY)), (
            "force=true 时 drift 行应被硬编码覆盖")

    async def test_idempotent_repeated_call_inserted_zero(self, db_conn):
        await sync_prompts_from_code(force=False, db=db_conn)
        # 制造一处 drift → force 覆盖 → 再跑一次
        mutated = clean_prompt_text(get_default_prompt(TEST_KEY)) + "\n# 临时改动"
        await db_conn.execute(
            "UPDATE prompt_templates SET content=? WHERE key=?", (mutated, TEST_KEY))
        await db_conn.commit()
        await sync_prompts_from_code(force=True, db=db_conn)
        res3 = await sync_prompts_from_code(force=False, db=db_conn)
        assert res3["summary"]["inserted"] == 0
        assert res3["summary"]["drift"] == 0
        assert res3["summary"]["in_sync"] == await _total_registered()
