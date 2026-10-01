"""「提取项目」(bid_analysis) 模块回归测试。

覆盖七条不变量：
1. /results 汇总的 missing_required / all_required_done 必须识别「未提取到」标记
   —— 必选项 status=success 但 content="未提取到" 时必须计为缺失
   （与 _get_missing_required、format_downstream_context、前端 _recomputeBaSummary 对齐）。
2. _get_missing_required 的 DB 回退分支（results 字典没有该必选项时查库）
   同样必须用 is_missing_result 判定，不能只看 content 非空。
3. 【P0】收尾调用 finish_task 不得传 progress 参数（其签名无该参数），
   否则全部解析项跑完后仍抛 TypeError → 前端永远收不到 completed
   （表现为「18 项结构化提取未完成」）。
4. 中断遗留的 running 解析项必须能被 clear_interrupted_items 收口，
   否则 UI 永久显示「运行中」、必选项永远判缺失。
5. 【JSON 口径】json 项（projectBasicInfo）所有字段都是「没有提及」时必须计为缺失，
   否则 all_required_done 误报 true、用户带着空白项目信息进入目录生成。
6. 【数据丢失/成本】force_rerun 只重置**本次要跑的项**；mode=item（单项重跑）
   不得被 normalize 强制补全必选项 —— 否则「重跑 1 项」变成「清空 18 项 + 跑 17 项」。
7. 【并发语义】暂停闸门必须排在获取并发信号量之前（暂停期间不得占住许可）。
8. 【口径单一来源】/items 必须返回 total/required_count/optional_count/
   markdown_count/json_count/group_count —— 前端不得再硬编码「18 项 / 17 必选」。
9. 【完成 ≠ 有内容】summary 必须提供 success_valid（完成且内容有效），
   前端「下一步：目录生成」的放行条件用 success_valid 而非 success。
10.【人工校正】PUT /results/{item_id} 覆盖 AI 输出并标记 source='manual'；
    json 项提交非法 JSON / 空 content / 伪造 item_id 分别 422 / 422 / 404；
    DELETE 回退 idle + source='ai' 且保留行；AI 重跑后 source 必须复位 'ai'。
11.【停止语义】用户主动停止 → finish_task('stopped')，不得误报 failed；
    「缺失且未停止」仍是 failed（回归护栏）。
12.【可观测性/数据丢失】切段后必须推送 text_stats（项数×段数≈模型调用数）；
    force_rerun 的清空必须排在 register_task 之后（注册失败不得清空既有结果）。

测试直接调用路由函数（与既有 test_global_facts_routes.py 风格一致），
避免 TestClient 跨事件循环持有 aiosqlite 连接导致的偶发失败。
"""
import asyncio
import inspect
import json
import re
import uuid
from pathlib import Path

import pytest

import app.routers.bid_analysis as ba
from app.services.ai.task_registry import (
    finish_task, register_task, request_control,
)
from app.services.bid_analysis_service import (
    ANALYSIS_ITEMS, REQUIRED_ITEM_IDS, MARKDOWN_MISSING_RESULT,
    get_item_def, is_missing_result,
)

_BID_ANALYSIS_SRC = Path(ba.__file__).resolve()


def _def(item_id: str) -> dict:
    return get_item_def(item_id)


async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute(
        "INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _put_item(db, pid: str, item_id: str, *,
                    status: str, content: str, error: str = "") -> None:
    """直接写一条 bid_analysis_items 行（绕过 _update_item_status 的 upsert）。"""
    def_ = _def(item_id) or {}
    pk = f"{pid}_{item_id}"
    await db.execute(
        "INSERT OR REPLACE INTO bid_analysis_items "
        "(id, project_id, scheme_id, item_id, label, output_type, required, "
        " status, content, error, sort_order) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pk, pid, "", item_id, def_.get("label", item_id),
         def_.get("output_type", "markdown"), def_.get("required", 0),
         status, content, error, def_.get("sort_order", 0)))
    await db.commit()


# ---------------------------------------------------------------------------
# 不变量 1：/results 汇总必须把 content="未提取到" 的必选项计为缺失
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_flags_required_item_with_missing_marker(db_conn):
    db = db_conn
    pid, sid = await _seed_scheme(db)
    # 必选项 schemeBasicInfo（markdown）跑完但整体无结果
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content=MARKDOWN_MISSING_RESULT)

    res = await ba.list_analysis_results(scheme_id=sid, project_id="", db=db)
    summary = res["summary"]
    label = _def("schemeBasicInfo")["label"]
    assert label in summary["missing_required"], (
        "必选项 content='未提取到' 必须计为缺失，而非当作已完成")
    assert summary["all_required_done"] is False


@pytest.mark.asyncio
async def test_results_required_item_with_real_content_not_missing(db_conn):
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="## 方案级信息\n深基坑支护方案。")

    res = await ba.list_analysis_results(scheme_id=sid, project_id="", db=db)
    label = _def("schemeBasicInfo")["label"]
    assert label not in res["summary"]["missing_required"], (
        "有真实内容的必选项不应被计为缺失")


@pytest.mark.asyncio
async def test_results_required_item_error_status_is_missing(db_conn):
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="error", content="", error="超时")

    res = await ba.list_analysis_results(scheme_id=sid, project_id="", db=db)
    label = _def("schemeBasicInfo")["label"]
    assert label in res["summary"]["missing_required"]


# ---------------------------------------------------------------------------
# 不变量 2：_get_missing_required 的 DB 回退分支同样要识别「未提取到」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_missing_required_db_branch_flags_missing_marker(db_conn):
    """results 字典里没有该必选项 → 走 DB 回退分支。

    旧实现 DB 分支只看 status/content 非空，不调 is_missing_result，
    导致 content='未提取到' 的必选项漏判为「已完成」。
    """
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content=MARKDOWN_MISSING_RESULT)

    # results 为空 → 必选项走 DB 回退分支
    missing = await ba._get_missing_required(db, pid, {})
    label = _def("schemeBasicInfo")["label"]
    assert label in missing, (
        "DB 回退分支也必须用 is_missing_result 判定，"
        "content='未提取到' 不能当作已完成")


@pytest.mark.asyncio
async def test_get_missing_required_results_branch_flags_missing_marker(db_conn):
    """results 字典里有该必选项 → 走 results 分支（既有正确逻辑，锁定防回归）。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    # results 分支不查 DB，直接看 results 字典
    results = {
        "schemeBasicInfo": {
            "status": "success",
            "content": MARKDOWN_MISSING_RESULT,
        }
    }
    missing = await ba._get_missing_required(db, pid, results)
    label = _def("schemeBasicInfo")["label"]
    assert label in missing


# ---------------------------------------------------------------------------
# 不变量 3【P0】：收尾 finish_task 不得传 progress 参数
# ---------------------------------------------------------------------------

def _finish_task_call_args(source: str) -> list[str]:
    """提取源码中所有 `finish_task(...)` 调用的括号内文本（跨行 + 嵌套括号）。"""
    calls: list[str] = []
    for m in re.finditer(r"finish_task\s*\(", source):
        i = m.end()          # 指向 '(' 之后
        start = i
        depth = 1
        while i < len(source) and depth:
            ch = source[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        calls.append(source[start:i - 1])
    return calls


def test_finish_task_signature_has_no_progress_param():
    """锁定 task_registry.finish_task 签名。

    若将来真的新增 progress 参数，本用例会失败并提示同步调用点 —— 这是有意为之：
    曾经 bid_analysis 单方面假设存在该参数，导致收尾 100% 抛 TypeError。
    """
    from app.services.ai.task_registry import finish_task
    assert list(inspect.signature(finish_task).parameters) == [
        "task_id", "status", "message"]


def test_no_finish_task_call_passes_progress_kwarg():
    """【P0 回归】bid_analysis 中不得有任何 finish_task 调用传 progress。

    旧实现在 4 处收尾调用上传 progress=100 → TypeError（见日志
    2026-09-17 13:34:23）→ 全部解析项跑完后仍被上报为失败、task_registry
    残留 running 僵尸 → 表现为「20 项结构化提取未完成」。
    """
    src = _BID_ANALYSIS_SRC.read_text(encoding="utf-8")
    calls = _finish_task_call_args(src)
    assert calls, "未找到 finish_task 调用（用例失配，请同步）"
    offenders = [c for c in calls if re.search(r"\bprogress\s*=", c)]
    assert not offenders, f"finish_task 无 progress 参数，违规调用：{offenders}"


def test_finish_task_uses_only_standard_terminal_statuses():
    """【P0 回归】bid_analysis 的任务终态只允许 completed/failed/stopped。

    旧实现传 status="success" / status="error"，与全库词表不一致：
    后端 `sse_handlers._TERMINAL_STATUSES`、前端 `pollTaskUntilTerminal` /
    `useSchemeLiveTask` / `TaskStatusBar.STATUS_META` 只认 completed/failed/stopped
    → 任务永远不被判定为终态（前端空转 10~20 分钟后靠「progress=100% 卡住」兜底），
    任务栏还会把原始英文 success 当标签显示。
    """
    allowed = {"completed", "failed", "stopped"}
    src = _BID_ANALYSIS_SRC.read_text(encoding="utf-8")
    bad = []
    for call in _finish_task_call_args(src):
        m = re.search(r"\bstatus\s*=\s*[\"']([^\"']+)[\"']", call)
        if m and m.group(1) not in allowed:
            bad.append(m.group(1))
    assert not bad, f"任务终态只能是 {sorted(allowed)}，发现：{bad}"


@pytest.mark.asyncio
async def test_run_with_progress_reaches_success_terminal_state(db_conn, monkeypatch):
    """【P0 回归·端到端】全链路跑完后必须正常落终态（不再抛 progress TypeError）。

    mode=custom 会被 normalize 强制补全全部必选项，因此一次调用即覆盖
    17 个必选项；AI 调用打桩为固定 JSON，避免真实网络。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)

    # 关掉 5s 提示词缓存预热等待，避免拖慢测试
    monkeypatch.setattr(ba, "PROMPT_CACHE_WARMUP_DELAY_MS", 0)

    async def fake_chat(messages, **kwargs):
        return '{"project_name": "测试项目"}'

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    cfg = ba.AnalysisConfig(mode="custom", selected_item_ids=["projectBasicInfo"])
    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)

    async def _cb(*_a, **_k):
        return None

    result = await ba._run_bid_analysis_with_progress(
        db, pid, sid, cfg, "项目名称：测试项目。", _cb, task_id)

    assert result["ok"] is True, result
    assert result["missing_required"] == []
    assert result["completed"] == result["total"] == len(REQUIRED_ITEM_IDS)

    cur = await db.execute(
        "SELECT status, progress FROM task_registry WHERE id=?", (task_id,))
    row = await cur.fetchone()
    # 任务终态必须是全库统一的 completed（而非旧实现的 success），
    # 否则前端 pollTaskUntilTerminal / TaskStatusBar 永远认不出终态
    assert row["status"] == "completed"
    assert float(row["progress"]) == 1.0


# ---------------------------------------------------------------------------
# 不变量 4：中断遗留的 running 解析项必须被收口
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_interrupted_items_marks_only_running_as_error(db_conn):
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "siteConditions", status="running", content="")
    await _put_item(db, pid, "schemeBasicInfo", status="success", content="已有内容")
    await _put_item(db, pid, "compilationBasis", status="idle", content="")

    n = await ba.clear_interrupted_items(db, pid)
    assert n == 1

    cur = await db.execute(
        "SELECT item_id, status, error FROM bid_analysis_items "
        "WHERE project_id=? ORDER BY item_id", (pid,))
    rows = {r["item_id"]: dict(r) for r in await cur.fetchall()}

    assert rows["siteConditions"]["status"] == "error"
    assert "中断" in rows["siteConditions"]["error"]
    # 非 running 的项不受影响
    assert rows["schemeBasicInfo"]["status"] == "success"
    assert rows["schemeBasicInfo"]["error"] == ""
    assert rows["compilationBasis"]["status"] == "idle"


@pytest.mark.asyncio
async def test_clear_interrupted_items_scoped_to_project(db_conn):
    """只清理目标项目，避免误伤其它项目的在途项。"""
    db = db_conn
    pid_a, _sa = await _seed_scheme(db)
    pid_b, _sb = await _seed_scheme(db)
    await _put_item(db, pid_a, "siteConditions", status="running", content="")
    await _put_item(db, pid_b, "siteConditions", status="running", content="")

    assert await ba.clear_interrupted_items(db, pid_a) == 1

    cur = await db.execute(
        "SELECT project_id, status FROM bid_analysis_items WHERE item_id='siteConditions'")
    got = {r["project_id"]: r["status"] for r in await cur.fetchall()}
    assert got[pid_a] == "error"
    assert got[pid_b] == "running", "未指定项目不得被清理"


@pytest.mark.asyncio
async def test_clear_interrupted_items_all_projects_when_no_project_id(db_conn):
    """启动恢复路径：project_id 为空 → 清理全部项目。"""
    db = db_conn
    pid_a, _sa = await _seed_scheme(db)
    pid_b, _sb = await _seed_scheme(db)
    await _put_item(db, pid_a, "siteConditions", status="running", content="")
    await _put_item(db, pid_b, "deploymentSchedule", status="running", content="")

    assert await ba.clear_interrupted_items(db) == 2

    cur = await db.execute(
        "SELECT COUNT(*) n FROM bid_analysis_items WHERE status='running'")
    assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_start_sse_missing_params_raises_400_not_500(db_conn):
    """回归（2026-09-18）：/start-sse 入口的宽异常包装曾把内层 400 参数校验
    错误伪装成 500（HTTPException 被 except Exception 捕获后重新抛 500），
    前端把「参数不对」当服务端崩溃。语义错误必须原样上抛。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await ba.start_bid_analysis_sse(
            scheme_id="", project_id="", mode="key",
            selected_item_ids="", force_rerun=False, db=db_conn)
    assert ei.value.status_code == 400, (
        f"缺参应返回 400，实测 {ei.value.status_code}：500 吞 400 复发")


@pytest.mark.asyncio
async def test_start_sse_unknown_scheme_never_becomes_500(db_conn):
    """同上：不存在的 scheme_id 按契约返回 404（项目不存在），
    关键是不得被宽异常包装吞成 500。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await ba.start_bid_analysis_sse(
            scheme_id=uuid.uuid4().hex, project_id="", mode="key",
            selected_item_ids="", force_rerun=False, db=db_conn)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# 不变量 5【JSON 口径】：整体「没有提及」的 json 必选项必须计为缺失
# ---------------------------------------------------------------------------

def test_is_missing_result_json_all_empty():
    """json 项「整体无有效信息」的判定口径。

    旧实现只认 markdown 的「未提取到」，json 项（projectBasicInfo）即便所有字段
    都是「没有提及」也被判为「已完成」。
    """
    # 全「没有提及」/空值 → 缺失
    assert is_missing_result('{"a":"没有提及","b":"没有提及"}', "json") is True
    assert is_missing_result('{"a":"没有提及","b":""}', "json") is True
    assert is_missing_result('{"a":null,"b":"无"}', "json") is True
    # 模型常包 ```json 代码块 → 围栏容错
    assert is_missing_result('```json\n{"a":"没有提及"}\n```', "json") is True
    # 任一有效值 → 不是缺失
    assert is_missing_result('{"a":"没有提及","b":"XX 项目"}', "json") is False
    assert is_missing_result('{"a":0}', "json") is False, "0 是有意义取值，不能当空"
    # 解析失败 / 非对象 → 不判缺失（交由上层容错，避免误伤）
    assert is_missing_result('{"a": "没有提及"', "json") is False
    assert is_missing_result("[1,2]", "json") is False
    # markdown 口径保持不变
    assert is_missing_result(MARKDOWN_MISSING_RESULT, "markdown") is True
    assert is_missing_result("   ", "markdown") is True
    assert is_missing_result("## 有内容", "markdown") is False


@pytest.mark.asyncio
async def test_results_flags_json_required_item_all_no_mention(db_conn):
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo", status="success",
                    content='{"project_name":"没有提及","contractor":"没有提及"}')

    res = await ba.list_analysis_results(scheme_id=sid, project_id="", db=db)
    label = _def("projectBasicInfo")["label"]
    assert label in res["summary"]["missing_required"], (
        "全「没有提及」的 json 必选项必须计为缺失，否则 all_required_done 误报 true")
    assert res["summary"]["all_required_done"] is False


@pytest.mark.asyncio
async def test_get_missing_required_json_db_branch(db_conn):
    """results 字典缺该项时的 DB 回退分支同样要识别 json 整体空。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo", status="success",
                    content='{"project_name":"没有提及"}')

    missing = await ba._get_missing_required(db, pid, {})
    assert _def("projectBasicInfo")["label"] in missing


def test_format_downstream_context_skips_empty_json_section():
    """下游摘要不得下发「只有标题、没有任何信息」的空小节。"""
    from app.services.bid_analysis_service import format_downstream_context

    empty = format_downstream_context({
        "projectBasicInfo": {
            "item_id": "projectBasicInfo", "label": "项目级基本信息",
            "output_type": "json", "status": "success",
            "content": '{"project_name":"没有提及"}',
        },
    })
    assert "项目级基本信息" not in empty

    filled = format_downstream_context({
        "projectBasicInfo": {
            "item_id": "projectBasicInfo", "label": "项目级基本信息",
            "output_type": "json", "status": "success",
            "content": '{"project_name":"没有提及","contractor":"某某公司"}',
        },
    })
    assert "项目级基本信息" in filled
    assert "某某公司" in filled


# ---------------------------------------------------------------------------
# 不变量 6【数据丢失/成本】：force_rerun 只重置本次要跑的项；mode=item 不补全必选项
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_items_for_rerun_scoped_to_given_items(db_conn):
    """【数据丢失回归】只重置传入的解析项，未选中的项内容必须保留。

    旧实现无条件重置该项目全部解析项：自定义模式（含前端「单项重新提取」）
    只跑其中一部分，其余项被清空且本轮不会重跑 → 用户已提取结果被静默删除。
    """
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo", status="success",
                    content='{"project_name":"A"}')
    await _put_item(db, pid, "schemeBasicInfo", status="success", content="存量方案级信息")

    await ba._reset_items_for_rerun(db, pid, ["schemeBasicInfo"])

    cur = await db.execute(
        "SELECT item_id, status, content FROM bid_analysis_items WHERE project_id=?",
        (pid,))
    rows = {r["item_id"]: dict(r) for r in await cur.fetchall()}
    assert rows["schemeBasicInfo"]["status"] == "idle"
    assert rows["schemeBasicInfo"]["content"] == ""
    assert rows["projectBasicInfo"]["status"] == "success", "未选中的项不得被清空"
    assert rows["projectBasicInfo"]["content"] == '{"project_name":"A"}'


@pytest.mark.asyncio
async def test_reset_items_for_rerun_without_ids_resets_all(db_conn):
    """不传 item_ids = 全部（key/full 全量重跑语义保持不变）。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo", status="success", content='{"a":"b"}')
    await _put_item(db, pid, "schemeBasicInfo", status="success", content="存量")

    await ba._reset_items_for_rerun(db, pid)

    cur = await db.execute(
        "SELECT item_id, status, content FROM bid_analysis_items WHERE project_id=?",
        (pid,))
    rows = {r["item_id"]: dict(r) for r in await cur.fetchall()}
    assert rows["projectBasicInfo"]["status"] == "idle"
    assert rows["schemeBasicInfo"]["status"] == "idle"
    # 全部 18 项都应被补齐入库
    assert len(rows) == len(ANALYSIS_ITEMS)


def test_item_mode_does_not_force_include_required_items():
    """mode=item（单项重跑）严格按勾选执行。

    custom 模式会强制补全全部必选项；若单项重跑沿用 custom，前端的「重新提取该项」
    实际会跑 17 项（AI 调用 ×17），且配合 force_rerun 会清空其余 17 项结果。
    """
    cfg = ba.AnalysisConfig(mode="item",
                            selected_item_ids=["schemeBasicInfo"]).normalize()
    assert [it["item_id"] for it in cfg.get_task_items()] == ["schemeBasicInfo"]

    # 非法 id 被过滤（防止 SQL/UI 出现幽灵项）
    cfg2 = ba.AnalysisConfig(mode="item",
                             selected_item_ids=["not_exist"]).normalize()
    assert cfg2.get_task_items() == []

    # custom 语义不变：仍强制补全必选项
    cfg3 = ba.AnalysisConfig(mode="custom",
                             selected_item_ids=["schemeBasicInfo"]).normalize()
    ids = {it["item_id"] for it in cfg3.get_task_items()}
    assert ids == set(REQUIRED_ITEM_IDS)


async def _seed_parsed_doc(db, pid: str) -> None:
    """写入一份「已解析」文档（project_documents 无 text_len 列，长度由上层计算）。"""
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, parsed_markdown, doc_category) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, "招标文件.docx", "docx",
         "# 招标文件\n项目名称：测试项目。", "招标文件"))
    await db.commit()


async def _drop_tasks_of_project(pid: str) -> None:
    """清理本用例注册的内存任务，避免污染同进程后续用例。"""
    from app.services.ai import task_registry as _tr
    for tid, state in list(_tr._tasks.items()):
        if state.get("project_id") == pid:
            await finish_task(tid, "stopped", "test cleanup")



# ---------------------------------------------------------------------------
# 不变量 8【2026-09-20】：/items 必须暴露口径明细，避免前端硬编码「18 项/17 必选」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_endpoint_exposes_breakdown_counts():
    """增减解析项时前端文案不得失真：口径必须由后端单一权威源给出。

    旧实现前端硬编码「18 项结构化提取」「17 项必选」，api/index.ts 注释甚至残留
    「20 项 / 14 项 / 15 分组」。现在由 /items 统一返回 breakdown 字段。
    """
    res = await ba.list_analysis_items()
    total = len(ANALYSIS_ITEMS)
    # ✅ 2026-09-30 第十六轮：18 项历史基线 + techScoring（技术评分要求，可选）= 19。
    assert res["total"] == total == 19
    assert res["required_count"] == len(REQUIRED_ITEM_IDS) == 17
    assert res["optional_count"] == total - len(REQUIRED_ITEM_IDS) == 2
    assert res["markdown_count"] == sum(
        1 for it in ANALYSIS_ITEMS if it.get("output_type") != "json") == 17
    assert res["json_count"] == sum(
        1 for it in ANALYSIS_ITEMS if it.get("output_type") == "json") == 2
    assert res["group_count"] == len(ba.get_groups()) == 14
    # 自洽校验：必填 + 可选 = 总数，markdown + json = 总数
    assert res["required_count"] + res["optional_count"] == res["total"]
    assert res["markdown_count"] + res["json_count"] == res["total"]


# ---------------------------------------------------------------------------
# 不变量 9【2026-09-20】：summary 必须区分「完成」与「完成且有有效内容」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_success_valid_excludes_missing_marker(db_conn):
    """前端「下一步：目录生成」放行条件必须用 success_valid，不能用 success。

    旧实现只看 success>0 就放行：一个 content='未提取到' 的完成项也会被当成
    可用成果，用户带着空项目信息进入目录生成。
    """
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content=MARKDOWN_MISSING_RESULT)
    await _put_item(db, pid, "siteConditions",
                    status="success", content="## 施工条件\n场地平整，无管线。")
    await _put_item(db, pid, "resourceAllocation",
                    status="error", content="", error="超时")

    summary = ba._compute_results_summary(await ba._load_results_rows(db, pid))
    assert summary["success"] == 2
    assert summary["success_valid"] == 1, (
        "content='未提取到' 的完成项不得计入 success_valid")
    assert summary["errors"] == 1
    assert summary["manual_count"] == 0


@pytest.mark.asyncio
async def test_summary_success_valid_accepts_json_with_real_values(db_conn):
    """json 项只要有任一真实值即算有效（与 is_missing_result 同口径）。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo",
                    status="success", content='{"project_name": "某某项目"}')
    summary = ba._compute_results_summary(await ba._load_results_rows(db, pid))
    assert summary["success_valid"] == 1

@pytest.mark.asyncio
async def test_start_sse_item_mode_keeps_other_results(db_conn):
    """【端到端】mode=item + force_rerun 只重置目标项，其余结果原样保留。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _seed_parsed_doc(db, pid)
    await _put_item(db, pid, "projectBasicInfo", status="success",
                    content='{"project_name":"A"}')
    await _put_item(db, pid, "schemeBasicInfo", status="success", content="存量")

    resp = await ba.start_bid_analysis_sse(
        scheme_id=sid, project_id="", mode="item",
        selected_item_ids=json.dumps(["schemeBasicInfo"]),
        force_rerun=True, db=db)
    assert resp is not None
    await _drop_tasks_of_project(pid)

    cur = await db.execute(
        "SELECT item_id, status, content FROM bid_analysis_items WHERE project_id=?",
        (pid,))
    rows = {r["item_id"]: dict(r) for r in await cur.fetchall()}
    assert rows["schemeBasicInfo"]["status"] == "idle"
    assert rows["projectBasicInfo"]["status"] == "success"
    assert rows["projectBasicInfo"]["content"] == '{"project_name":"A"}'


@pytest.mark.asyncio
async def test_start_sse_item_mode_with_no_valid_items_returns_400(db_conn):
    """归一化后无可执行项 → 400（避免跑出「0/0 完成」的假成功）。"""
    from fastapi import HTTPException

    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _seed_parsed_doc(db, pid)

    with pytest.raises(HTTPException) as ei:
        await ba.start_bid_analysis_sse(
            scheme_id=sid, project_id="", mode="item",
            selected_item_ids=json.dumps(["not_exist"]),
            force_rerun=False, db=db)
    assert ei.value.status_code == 400
    await _drop_tasks_of_project(pid)


# ---------------------------------------------------------------------------
# 不变量 7【并发语义】：暂停闸门必须排在获取并发信号量之前
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_item_status_clears_stale_error_on_success(db_conn):
    """重跑成功后必须清掉上一轮的 error，否则前端会把陈旧报错当本次失败原因。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo", status="error", content="", error="超时")

    await ba._update_item_status(db, pid, "schemeBasicInfo", "success", "新内容", "")

    cur = await db.execute(
        "SELECT status, content, error FROM bid_analysis_items "
        "WHERE project_id=? AND item_id='schemeBasicInfo'", (pid,))
    row = dict(await cur.fetchone())
    assert row["status"] == "success"
    assert row["content"] == "新内容"
    assert row["error"] == ""


@pytest.mark.asyncio
async def test_update_item_status_running_keeps_previous_content(db_conn):
    """running 不覆盖既有内容/报错（在途项保留上一轮结果供对照，勿改）。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo", status="error", content="旧内容", error="上次超时")

    await ba._update_item_status(db, pid, "schemeBasicInfo", "running", "", "")

    cur = await db.execute(
        "SELECT status, content, error FROM bid_analysis_items "
        "WHERE project_id=? AND item_id='schemeBasicInfo'", (pid,))
    row = dict(await cur.fetchone())
    assert row["status"] == "running"
    assert row["content"] == "旧内容"
    assert row["error"] == "上次超时"


@pytest.mark.asyncio
async def test_pause_gate_does_not_hold_semaphore_while_paused(db_conn):
    """暂停期间不得占用并发许可。

    旧实现先 `async with semaphore` 再在内部 `await wait_resume`：暂停时在途项
    把许可全部占住，恢复前任何后续解析项都启动不了（与 sse_handlers.guarded_gen
    的历史缺陷同源）。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)
    assert request_control(task_id, "pause") is True

    sem = asyncio.Semaphore(1)
    ran: list[str] = []

    async def _body():
        ran.append("ran")

    waiter = asyncio.create_task(ba.run_with_pause_gate(task_id, sem, _body))
    await asyncio.sleep(0.05)
    assert ran == [], "暂停态不得执行任务体"
    assert sem.locked() is False, "暂停期间不得占用并发许可（闸门必须在信号量之外）"

    assert request_control(task_id, "resume") is True
    await asyncio.wait_for(waiter, timeout=2)
    assert ran == ["ran"]
    assert sem.locked() is False, "执行完必须释放许可"

    await finish_task(task_id, "completed", "done")


@pytest.mark.asyncio
async def test_pause_gate_skips_stopped_task(db_conn):
    """已停止的任务不得再启动新项，也不得占用许可。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)
    assert request_control(task_id, "stop") is True

    sem = asyncio.Semaphore(1)
    ran: list[str] = []

    async def _body():
        ran.append("ran")

    await ba.run_with_pause_gate(task_id, sem, _body)
    assert ran == []
    assert sem.locked() is False

    await finish_task(task_id, "stopped", "done")



# ---------------------------------------------------------------------------
# 不变量 10【2026-09-20】：人工校正是提取结果错误的唯一可靠出口
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_edit_writes_content_and_marks_source_manual(db_conn):
    """PUT /results/{item_id}：覆盖 AI 输出，标记 source='manual'，回同口径 summary。

    背景：AI 抽错关键参数时旧实现唯一出路是整项重跑（贵且不稳定），而抽错的值
    会继续被 sse_handlers 的 format_downstream_context 传给目录/正文生成。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="## AI 抽出来的错误内容")

    res = await ba.update_single_result(
        "schemeBasicInfo", {"content": "## 人工修正：基坑支护方案"},
        scheme_id=sid, project_id="", db=db)

    item = res["item"]
    assert item["content"] == "## 人工修正：基坑支护方案"
    assert item["status"] == "success"
    assert item["source"] == "manual"
    assert item["error"] == ""
    assert res["summary"]["manual_count"] == 1
    # 人工修正后该项不再缺失
    assert _def("schemeBasicInfo")["label"] not in res["summary"]["missing_required"]


@pytest.mark.asyncio
async def test_manual_edit_clears_previous_error(db_conn):
    """上一轮的 error 不允许挂在人工校正后的结果上。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="error", content="", error="AI 调用超时")
    res = await ba.update_single_result(
        "schemeBasicInfo", {"content": "人工兜底内容"},
        scheme_id=sid, project_id="", db=db)
    assert res["item"]["error"] == ""
    assert res["item"]["status"] == "success"


@pytest.mark.asyncio
async def test_manual_edit_rejects_invalid_json_for_json_item(db_conn):
    """json 解析项必须提交合法 JSON 对象/数组，否则下游会拿到坏数据。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "projectBasicInfo",
                    status="success", content='{"project_name": "x"}')

    with pytest.raises(ba.HTTPException) as ei:
        await ba.update_single_result(
            "projectBasicInfo", {"content": "这不是 JSON"},
            scheme_id=sid, project_id="", db=db)
    assert ei.value.status_code == 422

    # 裸字符串虽是合法 JSON 但不是对象/数组，同样拒绝
    with pytest.raises(ba.HTTPException) as ei2:
        await ba.update_single_result(
            "projectBasicInfo", {"content": '"只是字符串"'},
            scheme_id=sid, project_id="", db=db)
    assert ei2.value.status_code == 422

    # 未通过校验的请求不得污染已提取的结果
    cur = await db.execute(
        "SELECT content, source FROM bid_analysis_items WHERE project_id=?", (pid,))
    row = dict(await cur.fetchone())
    assert row["content"] == '{"project_name": "x"}'
    assert row["source"] != "manual"


@pytest.mark.asyncio
async def test_manual_edit_rejects_empty_content(db_conn):
    """空内容请走 DELETE，不允许用 PUT 静默清空（会造成「看起来成功了却是空」）。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="原有内容")
    with pytest.raises(ba.HTTPException) as ei:
        await ba.update_single_result(
            "schemeBasicInfo", {"content": "   "},
            scheme_id=sid, project_id="", db=db)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_manual_edit_unknown_item_returns_404(db_conn):
    """伪造 item_id 必须 404，不得写入幽灵行。"""
    db = db_conn
    _pid, sid = await _seed_scheme(db)
    with pytest.raises(ba.HTTPException) as ei:
        await ba.update_single_result(
            "nonexistentItem", {"content": "x"},
            scheme_id=sid, project_id="", db=db)
    assert ei.value.status_code == 404



@pytest.mark.asyncio
async def test_clear_single_result_resets_to_idle_and_ai_source(db_conn):
    """DELETE：撤销人工校正（或放弃某项），回到 idle + source='ai'，行本身保留。

    不删行的原因：行是「解析项槽位」，删掉会破坏 sort_order 展示顺序与
    force_rerun 的 upsert 语义（_reset_items_for_rerun 会重新 INSERT）。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="待清空")

    res = await ba.clear_single_result(
        "schemeBasicInfo", scheme_id=sid, project_id="", db=db)
    item = res["item"]
    assert item["content"] == ""
    assert item["status"] == "idle"
    assert item["error"] == ""
    assert item["source"] == "ai"
    cur = await db.execute(
        "SELECT id FROM bid_analysis_items WHERE project_id=?", (pid,))
    assert (await cur.fetchone()) is not None
    # 清空后该项回到缺失
    assert _def("schemeBasicInfo")["label"] in res["summary"]["missing_required"]
    assert res["summary"]["manual_count"] == 0


@pytest.mark.asyncio
async def test_ai_rerun_resets_manual_source_back_to_ai(db_conn):
    """source 契约：人工校正后再由 AI 重跑，source 必须复位为 'ai'。

    否则前端会持续显示「已人工校正」徽标，用户无从分辨当前内容是自己改过的、
    还是 AI 重跑出来的 —— 而这恰恰是该徽标要传达的信息。
    """
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="人工校正内容")
    await db.execute(
        "UPDATE bid_analysis_items SET source='manual' WHERE project_id=?", (pid,))
    await db.commit()

    await ba._update_item_status(db, pid, "schemeBasicInfo", "success",
                                 "AI 重跑内容", "")
    cur = await db.execute(
        "SELECT source, content FROM bid_analysis_items WHERE project_id=?", (pid,))
    row = dict(await cur.fetchone())
    assert row["source"] == "ai"
    assert row["content"] == "AI 重跑内容"

    # force_rerun 的 idle 重置同样复位
    await ba._reset_items_for_rerun(db, pid, ["schemeBasicInfo"])
    cur = await db.execute(
        "SELECT source, status, content FROM bid_analysis_items WHERE project_id=?", (pid,))
    row = dict(await cur.fetchone())
    assert row["source"] == "ai"
    assert row["status"] == "idle"
    assert row["content"] == ""



async def _seed_doc(db, pid: str, text: str = "项目名称：某某项目。") -> None:
    """塞一份已解析文档，满足 _start_sse_inner 的前置校验。"""
    await db.execute(
        "INSERT INTO project_documents "
        "(id, project_id, file_name, file_type, parsed_markdown, doc_category, file_size) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"doc_{uuid.uuid4().hex[:8]}", pid, "招标文件.docx", "docx", text, "招标文件", len(text)))
    await db.commit()


# ---------------------------------------------------------------------------
# 不变量 11【2026-09-20】：用户「停止」不得被报告成「失败」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_is_reported_as_stopped_not_failed(db_conn, monkeypatch):
    """停止后未启动的项在 DB 里是 idle → _get_missing_required 全判缺失。

    旧实现「缺失即 failed」，用户主动点停止看到的是
    「必填解析项未完成：项目级基本信息、方案级基本信息…」+ 前端弹
    「提取完成，但必选项未全部完成」，任务栏显示 failed。
    现在：停止 → status='stopped'，result.stopped=True，缺失仍如实上报供下次补跑。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    monkeypatch.setattr(ba, "PROMPT_CACHE_WARMUP_DELAY_MS", 0)
    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)
    assert request_control(task_id, "stop") is True

    # 单项模式（可选的 resourceAllocation）：停止后 run_with_pause_gate 直接返回
    cfg = ba.AnalysisConfig(mode="item", selected_item_ids=["resourceAllocation"])

    async def _noop_cb(*_a, **_k):
        return None

    result = await ba._run_bid_analysis_with_progress(
        db, pid, sid, cfg, "项目名称：某某项目。", _noop_cb, task_id)

    assert result["stopped"] is True
    assert result["ok"] is False
    assert result["completed"] == 0
    assert result["total"] == 1
    assert result["missing_required"], "停止后未跑的必选项仍应如实上报为缺失"

    cur = await db.execute(
        "SELECT status, progress, message FROM task_registry WHERE id=?", (task_id,))
    row = dict(await cur.fetchone())
    assert row["status"] == "stopped", "用户主动停止不得被上报为 failed"
    assert float(row["progress"]) == 1.0
    assert "已停止" in row["message"]


@pytest.mark.asyncio
async def test_missing_required_without_stop_still_reports_failed(db_conn, monkeypatch):
    """回归护栏：停止语义修复不得误伤正常的失败路径。

    只有「缺失且未被停止」才是 failed —— 这条路径是前端判断「是否补跑」的依据。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    monkeypatch.setattr(ba, "PROMPT_CACHE_WARMUP_DELAY_MS", 0)
    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)

    cfg = ba.AnalysisConfig(mode="item", selected_item_ids=["resourceAllocation"])

    async def _noop_cb(*_a, **_k):
        return None

    result = await ba._run_bid_analysis_with_progress(
        db, pid, sid, cfg, "项目名称：某某项目。", _noop_cb, task_id)

    assert result["stopped"] is False
    assert result["ok"] is False
    cur = await db.execute(
        "SELECT status, message FROM task_registry WHERE id=?", (task_id,))
    row = dict(await cur.fetchone())
    assert row["status"] == "failed"
    assert "必填解析项未完成" in row["message"]


@pytest.mark.asyncio
async def test_stats_callback_reports_extraction_scale(db_conn, monkeypatch):
    """切段完成后必须推送「提取规模」：项数 × 段数 ≈ 模型调用次数。

    超长文档此前对用户完全不可见（50 万字 = 32 段 × 18 项 = 576 次调用），
    只能等十几分钟后从账单里发现额度被烧光。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    monkeypatch.setattr(ba, "PROMPT_CACHE_WARMUP_DELAY_MS", 0)

    seen: list[dict] = []

    async def _stats(stats):
        seen.append(stats)

    async def _noop_cb(*_a, **_k):
        return None

    task_id = await register_task("bid_analysis", project_id=pid, scheme_id=sid)
    cfg = ba.AnalysisConfig(mode="item", selected_item_ids=["resourceAllocation"])

    async def fake_chat(messages, **kwargs):
        return "未提取到"

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    await ba._run_bid_analysis_with_progress(
        db, pid, sid, cfg, "x" * 40000, _noop_cb, task_id, stats_callback=_stats)

    assert len(seen) == 1, "切段完成后必须且仅推送一次提取规模"
    s = seen[0]
    assert s["total_chars"] == 40000
    assert s["item_count"] == 1
    assert s["segment_count"] >= 3, f"40000 字应切成多段，实际 {s['segment_count']}"
    assert s["est_model_calls"] == s["item_count"] * s["segment_count"]
    assert s["chunk_size"] == ba.DEFAULT_CHUNK_SIZE


# ---------------------------------------------------------------------------
# 增强分段策略（2026-09-23 吸收 OpenBidKit userTextSplitter 成熟思路）
#   不变量：① 绝不切断 Markdown 代码围栏；② 无换行长文按句末标点断开；
#   ③ 保留 chunk_size/overlap/终止契约（向后兼容）。
# ---------------------------------------------------------------------------
def _svc():
    from app.services import bid_analysis_service as svc
    return svc


def test_split_never_cuts_inside_code_fence():
    """跨越理想断点的代码围栏不得被切断：每个 ``` / ~~~ 必须完整落在某一段内。"""
    svc = _svc()
    # 构造：正文 + 一段很长的、跨过 chunk 边界的代码围栏 + 尾部正文
    prose = ("正文句子。" * 4000)                      # 约 16000 字，把围栏顶到切分点附近
    fence = "\n```python\n" + ("print('x')\n" * 800) + "```\n"  # 一个完整的大代码块
    tail = "结论" * 2000
    text = prose + fence + tail
    segments = svc.split_for_analysis(text, chunk_size=4000, overlap=0)
    joined = "\n".join(segments)
    # 围栏开闭标记总数在切分前后守恒（未被从中间切断而丢失配对）
    assert joined.count("```") == text.count("```"), "代码围栏的开闭标记不能被切断丢失"
    # 每一段内部的 ``` 数量必须是偶数（不存在被劈开的半个围栏块）
    for i, seg in enumerate(segments):
        assert seg.count("```") % 2 == 0, f"第 {i} 段内含被切断的半个代码块"


def test_split_falls_back_to_sentence_boundary_without_newlines():
    """整段无换行的超长文本：应在句末标点处断开，而非从句中硬切。"""
    svc = _svc()
    sentence = "这是一段没有换行的连续中文说明文字用于测试边界回退。"  # 以句号结尾
    text = sentence * 600                              # 无换行、只有句末标点
    segments = svc.split_for_analysis(text, chunk_size=2000, overlap=0)
    assert len(segments) > 1
    # 除最后一段外，每段都应以句末标点收尾（说明按句子边界断开，未截断句子）
    for seg in segments[:-1]:
        s = seg.strip()
        assert s and s[-1] in "。！？", f"段尾未按句末标点断开：...{s[-8:]}"


def test_split_preserves_overlap_and_termination_contract():
    """回归护栏：chunk_size<=overlap 仍前进不死循环；重叠语义保留。"""
    svc = _svc()
    segs = svc.split_for_analysis("甲" * 200, 40, 500)  # 旧实现会死循环的入参
    assert len(segs) > 1 and all(segs)
    # overlap>0 时相邻段应有重叠（普通自然文本）
    body = ("段落内容。\n" * 3000)
    with_ov = svc.split_for_analysis(body, chunk_size=1000, overlap=300)
    no_ov = svc.split_for_analysis(body, chunk_size=1000, overlap=0)
    assert len(with_ov) >= len(no_ov), "保留重叠不会减少段数"


def test_collect_fence_ranges_basics():
    """围栏区间收集：闭合配对成段、未闭合延伸到文末、行内 ``` 不算。"""
    svc = _svc()
    text = "前\n```\ncode\n```\n后"
    ranges = svc._collect_fence_ranges(text)
    assert len(ranges) == 1
    s, e = ranges[0]
    assert text[s:e].startswith("```") and text[s:e].endswith("```")
    # 未闭合围栏：延伸到文末
    open_ranges = svc._collect_fence_ranges("a\n```\nunclosed tail")
    assert open_ranges and open_ranges[0][1] == len("a\n```\nunclosed tail")
    # 无围栏
    assert svc._collect_fence_ranges("普通正文，无围栏。") == []



# ---------------------------------------------------------------------------
# 不变量 12【2026-09-20】：force_rerun 的「清空」不得先于「注册任务」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_rerun_register_failure_keeps_existing_results(db_conn, monkeypatch):
    """register_task 抛异常时，已提取的结果必须完好无损。

    旧顺序是「先 _reset_items_for_rerun 清空结果，再 register_task」：注册一旦
    抛错（DB 只读 / 迁移失败 / 内存态异常），清空已经落库且本轮不会重跑 ——
    用户一次点击「开始提取」就丢掉全部 18 项既有成果。
    新顺序（register → clear_interrupted → reset）下清空发生在任务生命周期内。
    """
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await _seed_doc(db, pid)
    await _put_item(db, pid, "schemeBasicInfo",
                    status="success", content="宝贵成果，不可丢失")

    async def boom(*_a, **_k):
        raise RuntimeError("DB 只读")

    monkeypatch.setattr(ba, "register_task", boom)

    with pytest.raises(RuntimeError):
        await ba._start_sse_inner(db, sid, "", "key", "", True)

    cur = await db.execute(
        "SELECT content, status, source FROM bid_analysis_items WHERE project_id=?",
        (pid,))
    row = dict(await cur.fetchone())
    assert row["content"] == "宝贵成果，不可丢失", (
        "register_task 失败不得清空已提取结果")
    assert row["status"] == "success"


@pytest.mark.asyncio
async def test_sse_entry_wires_stats_callback_and_text_stats_event(db_conn):
    """源码级护栏：SSE 入口必须把 stats_callback 透传进执行体，并发出 text_stats。

    直接消费 StreamingResponse 需要打桩整条 AI 链路；这里沿用本文件既有的
    源码扫描风格（见 _finish_task_call_args）锁定接线不回归。
    """
    src = _BID_ANALYSIS_SRC.read_text(encoding="utf-8")
    assert "type\": \"text_stats\"" in src, "缺少 text_stats 事件类型"
    assert "async def stats_callback(stats" in src, "缺少 stats_callback 定义"
    # 定位 run_inner 里的调用点（第一个匹配是 _start_sse_inner 内的 run_inner）
    m = re.search(
        r"result = await _run_bid_analysis_with_progress\((.*?)\)\s*\n\s*"
        r'await queue\.put\(\{"type": "completed"', src, re.S)
    assert m, "未找到 run_inner 中的 _run_bid_analysis_with_progress 调用（用例失配，请同步）"
    call = m.group(1)
    assert "stats_callback=stats_callback" in call, (
        "SSE 入口未把 stats_callback 透传给执行体 —— 提取规模将不再推送")

