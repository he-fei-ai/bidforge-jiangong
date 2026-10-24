"""P4 — task_state 持久 task_key 用例（create / checkpoint / load / resume）。

映射依据：易标 ``persistent_task={task_key, mode:create|resume}`` 与
``checkpointPersistentTask`` 字段；本仓复用 task_registry（不建新表）。

测试策略：
- 用 conftest 的 ``db_conn`` 内存库（它已 patch app.db 与 task_registry 的
  get_conn），再补 patch 本模块自身的 ``get_conn``/``get_read_conn``/
  ``release_read_conn`` 导入期绑定（conftest 的模块清单不含本新模块）；
- 断点写读全部以 DB 行内容为准（不信任内存返回值）。
"""
import json
import uuid

import app.services.agent.task_state as ts
import pytest
import pytest_asyncio
from app.services.agent.task_state import (
    AGENT_CHECKPOINT_KIND,
    checkpoint_agent_task,
    create_agent_task,
    load_agent_checkpoint,
    resume_agent_task,
)


@pytest_asyncio.fixture
async def agent_db(db_conn, monkeypatch):
    """在 db_conn 内存库上补齐本模块的导入期绑定 patch。"""

    async def fake_conn():
        return db_conn

    async def fake_release(_conn):
        return None

    monkeypatch.setattr(ts, "get_conn", fake_conn)
    monkeypatch.setattr(ts, "get_read_conn", fake_conn)
    monkeypatch.setattr(ts, "release_read_conn", fake_release)
    return db_conn


async def _read_checkpoint(conn, task_id: str) -> dict:
    cur = await conn.execute(
        "SELECT checkpoint_json FROM task_registry WHERE id=?", (task_id,))
    row = await cur.fetchone()
    assert row is not None, f"任务行不存在：{task_id}"
    return json.loads(row[0])


async def _read_status(conn, task_id: str) -> str:
    cur = await conn.execute(
        "SELECT status FROM task_registry WHERE id=?", (task_id,))
    row = await cur.fetchone()
    assert row is not None, f"任务行不存在：{task_id}"
    return row[0]


# ── create ──────────────────────────────────────────────────────────────

async def test_create_写入易标断点字段并注册(agent_db):
    tid = await create_agent_task(
        "outline-task-1", project_id="p1", scheme_id="s1",
        phase="initial-outline", stage_index=0,
        workflow_stage="initial-outline", prompted_stage="",
    )
    assert isinstance(tid, str) and len(tid) == 36  # uuid4
    cur = await agent_db.execute(
        "SELECT task_type, project_id, scheme_id, status FROM task_registry "
        "WHERE id=?", (tid,))
    row = dict(await cur.fetchone())
    assert row["task_type"] == ts.DEFAULT_TASK_TYPE
    assert row["project_id"] == "p1"
    assert row["scheme_id"] == "s1"
    assert row["status"] == "running"
    ck = await _read_checkpoint(agent_db, tid)
    assert ck["kind"] == AGENT_CHECKPOINT_KIND
    assert ck["task_key"] == "outline-task-1"
    assert ck["status"] == "running"
    assert ck["agent_connection"] == "running"
    assert ck["phase"] == "initial-outline"
    assert ck["stage_index"] == 0
    assert ck["workflow_stage"] == "initial-outline"
    assert ck["prompted_stage"] == ""
    assert ck["compaction_pending"] is None
    # 内存运行态同步建立（暂停/停止控制依赖）
    from app.services.ai.task_registry import _tasks
    assert tid in _tasks


async def test_create_空task_key拒绝(agent_db):
    with pytest.raises(ValueError):
        await create_agent_task("")
    with pytest.raises(ValueError):
        await create_agent_task("   ")


async def test_create_extra不覆盖身份字段(agent_db):
    tid = await create_agent_task("k-extra", extra={
        "kind": "outline_result", "task_key": "evil", "note": "自定义"})
    ck = await _read_checkpoint(agent_db, tid)
    assert ck["kind"] == AGENT_CHECKPOINT_KIND
    assert ck["task_key"] == "k-extra"
    assert ck["note"] == "自定义"


# ── checkpoint ──────────────────────────────────────────────────────────

async def test_checkpoint_合并写与身份保护(agent_db):
    tid = await create_agent_task("k-merge", phase="score-planning")
    merged = await checkpoint_agent_task(tid, {
        "stage_index": 2,
        "workflow_stage": "score-planning",
        "prompted_stage": "initial-outline",
        # 身份字段尝试改写：必须被静默保护
        "kind": "outline_result",
        "task_key": "evil",
    })
    assert merged["stage_index"] == 2
    assert merged["kind"] == AGENT_CHECKPOINT_KIND
    assert merged["task_key"] == "k-merge"
    ck = await _read_checkpoint(agent_db, tid)
    # 合并语义：原有字段保留
    assert ck["phase"] == "score-planning"
    assert ck["status"] == "running"
    assert ck["stage_index"] == 2
    assert ck["workflow_stage"] == "score-planning"
    assert ck["prompted_stage"] == "initial-outline"
    assert ck["kind"] == AGENT_CHECKPOINT_KIND
    assert ck["task_key"] == "k-merge"


async def test_checkpoint_任务不存在抛错(agent_db):
    with pytest.raises(ValueError):
        await checkpoint_agent_task(str(uuid.uuid4()), {"stage_index": 1})


async def test_checkpoint_非dict拒绝(agent_db):
    tid = await create_agent_task("k-bad-fields")
    with pytest.raises(ValueError):
        await checkpoint_agent_task(tid, [("stage_index", 1)])  # type: ignore[arg-type]


# ── load ────────────────────────────────────────────────────────────────

async def test_load_同key取最新一次运行(agent_db):
    old_id = await create_agent_task("k-latest", scheme_id="s1")
    await checkpoint_agent_task(old_id, {"stage_index": 1})
    # 同 scheme 同类型再次 create：防僵尸会把旧行标记 failed
    new_id = await create_agent_task("k-latest", scheme_id="s1")
    await checkpoint_agent_task(new_id, {"stage_index": 3})
    assert new_id != old_id
    assert await _read_status(agent_db, old_id) == "failed"
    ck = await load_agent_checkpoint("k-latest")
    assert ck is not None
    assert ck["stage_index"] == 3


async def test_load_无果返回None(agent_db):
    assert await load_agent_checkpoint("根本不存在的key") is None
    assert await load_agent_checkpoint("") is None


async def test_load_坏JSON行不抛错(agent_db):
    # 历史行可能有非 JSON 的 checkpoint_json（json_valid 守卫）
    await agent_db.execute(
        "INSERT INTO task_registry (id, task_type, checkpoint_json) "
        "VALUES (?,?,?)", ("raw-1", "outline_generation", "not-json"))
    await agent_db.commit()
    assert await load_agent_checkpoint("whatever") is None


async def test_load_其他kind不串台(agent_db):
    # sse_checkpoint 的成果行（kind=outline_result）即使带同名 task_key 也不认
    await agent_db.execute(
        "INSERT INTO task_registry (id, task_type, checkpoint_json) "
        "VALUES (?,?,?)", ("sse-1", "outline_generation",
                           json.dumps({"kind": "outline_result",
                                       "task_key": "k-cross"})))
    await agent_db.commit()
    assert await load_agent_checkpoint("k-cross") is None


# ── resume ──────────────────────────────────────────────────────────────

async def test_resume_断点保真与状态复位(agent_db):
    old_id = await create_agent_task("k-resume", scheme_id="s1",
                                     phase="children_generation",
                                     stage_index=1,
                                     workflow_stage="score-planning",
                                     prompted_stage="score-planning")
    # 模拟跑到一半并进入等待态
    await checkpoint_agent_task(old_id, {
        "stage_index": 5,
        "workflow_stage": "children_generation",
        "prompted_stage": "score-planning",
        "status": "waiting-outline-selection",
        "agent_connection": "idle",
        "compaction_pending": "initial-outline",
        "自定义断点": "保留我",
    })
    new_id, ck = await resume_agent_task("k-resume", scheme_id="s1")
    # 新行注册，旧行（同 scheme 同类型 running）由防僵尸收尾
    assert new_id != old_id
    assert await _read_status(agent_db, old_id) == "failed"
    assert await _read_status(agent_db, new_id) == "running"
    # 断点保真：阶段推进字段原样带入
    assert ck["task_key"] == "k-resume"
    assert ck["stage_index"] == 5
    assert ck["workflow_stage"] == "children_generation"
    assert ck["prompted_stage"] == "score-planning"
    assert ck["phase"] == "children_generation"
    assert ck["compaction_pending"] == "initial-outline"
    assert ck["自定义断点"] == "保留我"
    # 运行时状态复位
    assert ck["status"] == "running"
    assert ck["agent_connection"] == "running"
    # 落库一致
    assert await _read_checkpoint(agent_db, new_id) == ck


async def test_resume_无历史断点抛错(agent_db):
    with pytest.raises(ValueError):
        await resume_agent_task("从未创建过")


async def test_resume_extra不覆盖身份与断点阶段(agent_db):
    old_id = await create_agent_task("k-protect", scheme_id="s1",
                                     stage_index=2,
                                     workflow_stage="leaf_allocation")
    assert old_id
    new_id, ck = await resume_agent_task(
        "k-protect", scheme_id="s1",
        extra={"task_key": "evil", "stage_index": 99})
    # extra 允许推进断点，但身份字段不可覆盖
    assert ck["task_key"] == "k-protect"
    assert ck["stage_index"] == 99
    assert ck["workflow_stage"] == "leaf_allocation"
