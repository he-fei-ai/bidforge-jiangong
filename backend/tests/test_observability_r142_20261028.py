"""R142 · 可观测性（结构化日志/全链路追踪/运行轨迹）综合测试（2026-10-28）

覆盖本轮修复与接线：
  A · StructuredJsonFormatter 输出 18 字段合法 JSON + 敏感信息脱敏
  B · redact_sensitive 各规则（API Key / 密码 / 手机号 / 身份证 / Bearer / 邮箱）
  C · should_sample（DEBUG 按采样率，其它全量）
  D · log_event 写 system_logs（字段完整 + 脱敏 + fail-soft）
  E · query_logs 组合筛选 + 分页排序
  F · get_trace_tree 返回 orphan_spans（前端已消费，后端旧实现未返回）
  G · get_task_timeline 按 task_id 返回运行轨迹
  H · detect_orphan_spans 断链判定
  I · cleanup_old_logs 按级别保留天数清理
  J · get_metrics 聚合
  K · evaluate_alerts 告警规则
  L · Span 上下文管理器记录 span 事件与耗时
  M · AsyncStructuredFileHandler 批量写 JSONL / 降级 / 队列满丢弃
  N · cleanup_expired_logs 文件轮转清理
  O · 任务生命周期 → system_logs（register_task / set_task_status / finish_task）

风格：直调函数 + tmp_path 隔离库 + teardown `db.close()`（R138 教训⑦）。
"""
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.services import observability as obs_svc
from app.utils import observability as obs_utils


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "r142-obs.sqlite"
    await init_db()
    db = await get_conn()
    yield db
    await db.close()


@pytest.fixture
def ctx_sync(tmp_path):
    """同步版本（用于不依赖事件循环的纯函数测试，无需 DB）。"""
    return tmp_path


# ---------- A · StructuredJsonFormatter ----------

def test_formatter_emits_all_18_fields():
    fmt = obs_utils.StructuredJsonFormatter()
    rec = logging.LogRecord(name="ai", level=logging.INFO, pathname=__file__,
                            lineno=1, msg="hello %s", args=("world",),
                            exc_info=None)
    rec.event = "ai.call"
    rec.trace_id = "abc123"
    rec.duration_ms = 12.5
    rec.status = "ok"
    rec.error_code = "AI_001"
    out = json.loads(fmt.format(rec))
    # 18 个契约字段
    for f in ("timestamp", "level", "module", "event", "trace_id", "span_id",
              "parent_span_id", "request_id", "task_id", "project_id",
              "chapter_id", "duration_ms", "status", "error_code",
              "error_layer", "retryable", "message", "detail"):
        assert f in out, f"缺少字段 {f}"
    assert out["level"] == "INFO"
    assert out["module"] == "ai"
    assert out["event"] == "ai.call"
    assert out["trace_id"] == "abc123"
    assert out["message"] == "hello world"
    assert out["duration_ms"] == 12.5
    assert out["status"] == "ok"
    assert out["error_code"] == "AI_001"


def test_formatter_redacts_secret_in_message():
    fmt = obs_utils.StructuredJsonFormatter()
    rec = logging.LogRecord(name="x", level=logging.INFO, pathname=__file__,
                            lineno=1, msg="key=sk-ppJZzOiUpsZj74wZ2", args=(),
                            exc_info=None)
    out = json.loads(fmt.format(rec))
    assert "ppJZzOiUpsZj74wZ2" not in out["message"]
    assert "sk-ppJZ****" in out["message"] or "sk-ppJZ****" in out["message"]


def test_formatter_empty_optional_fields_become_none():
    fmt = obs_utils.StructuredJsonFormatter()
    rec = logging.LogRecord(name="x", level=logging.INFO, pathname=__file__,
                            lineno=1, msg="plain", args=(), exc_info=None)
    out = json.loads(fmt.format(rec))
    # 空可选字段归一为 None（除 timestamp/level/module/message）
    assert out["trace_id"] is None
    assert out["span_id"] is None
    assert out["duration_ms"] is None
    assert out["error_code"] is None


# ---------- B · redact_sensitive ----------

def test_redact_api_key_sk():
    out = obs_utils.redact_sensitive("sk-ppJZzOiUpsZj74wZ2zxf95xfxAhxed4vZbNUdrqThTo7uvRD")
    assert "ppJZzOiUpsZj74wZ2" not in out
    assert "sk-ppJZ" in out and "****" in out


def test_redact_password_and_token():
    out = obs_utils.redact_sensitive("password=hunter2 secret")
    assert "hunter2" not in out
    out2 = obs_utils.redact_sensitive("token=abcdef1234567890")
    assert "abcdef1234567890" not in out2


def test_redact_phone_and_idcard():
    out = obs_utils.redact_sensitive("手机 13812345678 联系")
    assert "13812345678" not in out
    out2 = obs_utils.redact_sensitive("身份证 110101199003077123")
    assert "110101199003077123" not in out2


def test_redact_bearer():
    out = obs_utils.redact_sensitive("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxxx")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_recursive_dict():
    d = {"api_key": "sk-abc123456789", "nested": {"pwd": "s3cret!"}}
    out = obs_utils.redact_sensitive(d)
    assert "sk-abc123456789" not in json.dumps(out)
    assert "s3cret!" not in json.dumps(out)


# ---------- C · should_sample ----------

def test_should_sample_non_debug_always_true():
    # 非 DEBUG 级别恒采样（全量）
    assert obs_utils.should_sample("INFO") is True
    assert obs_utils.should_sample("ERROR") is True
    assert obs_utils.should_sample("WARN") is True
    assert obs_utils.should_sample("FATAL") is True


def test_should_sample_debug_by_rate(monkeypatch):
    import app.config as _cfg
    monkeypatch.setattr(_cfg.settings, "structured_log_sample_debug", 1.0)
    assert obs_utils.should_sample("DEBUG") is True
    monkeypatch.setattr(_cfg.settings, "structured_log_sample_debug", 0.0)
    assert obs_utils.should_sample("DEBUG") is False


# ---------- D · log_event → system_logs ----------

async def test_log_event_writes_row(ctx):
    db = ctx
    await obs_svc.log_event(db, level="INFO", module="parse", event="parse.start",
                            trace_id="tid1", task_id="task1", project_id="pid1",
                            message="开始解析", detail={"file": "a.pdf", "pages": 3})
    cur = await db.execute("SELECT * FROM system_logs WHERE trace_id='tid1'")
    row = dict(zip([d[0] for d in cur.description], (await cur.fetchone())))
    assert row["module"] == "parse"
    assert row["event"] == "parse.start"
    assert row["task_id"] == "task1"
    assert row["project_id"] == "pid1"
    assert row["message"] == "开始解析"
    assert json.loads(row["detail"])["file"] == "a.pdf"


async def test_log_event_redacts_secret(ctx):
    db = ctx
    await obs_svc.log_event(db, level="INFO", module="ai", event="ai.call",
                            trace_id="tid2", message="using key sk-ppJZzOiUpsZj74wZ2",
                            detail={"token": "eyJhbGciOiJIUzI1NiJ9.xxx"})
    cur = await db.execute("SELECT message, detail FROM system_logs WHERE trace_id='tid2'")
    msg, detail = await cur.fetchone()
    assert "ppJZzOiUpsZj74wZ2" not in msg
    assert "eyJhbGciOiJIUzI1NiJ9" not in detail


async def test_log_event_retryable_persists(ctx):
    db = ctx
    await obs_svc.log_event(db, level="ERROR", module="export", event="export.docx.fail",
                            error_code="EXPORT_004", error_layer="io", retryable=True,
                            message="写盘失败")
    cur = await db.execute("SELECT error_code, error_layer, retryable FROM system_logs WHERE error_code='EXPORT_004'")
    code, layer, retryable = await cur.fetchone()
    assert code == "EXPORT_004"
    assert layer == "io"
    assert retryable == 1


async def test_log_event_fail_soft_on_bad_db(ctx):
    # 传一个不支持的 db（无 execute）→ 不应抛出
    class _Bad:
        pass
    try:
        await obs_svc.log_event(_Bad(), level="INFO", module="x", event="x", message="y")
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"log_event 应 fail-soft，实际抛出 {e}")


# ---------- E · query_logs ----------

async def _seed_logs(db, n=5, trace="tr_query", task="tk1", module="mod", level="INFO"):
    for i in range(n):
        await obs_svc.log_event(db, level=level, module=module, event=f"ev{i}",
                                trace_id=trace, task_id=task, project_id="pid",
                                error_code="MOD_00%d" % i if i < 3 else "",
                                message=f"消息{i} keyword{i}")


async def test_query_logs_filter_trace(ctx):
    db = ctx
    await _seed_logs(db, trace="tr_only", task="tk_x", module="m", level="INFO")
    res = await obs_svc.query_logs(db, trace_id="tr_only")
    assert res["total"] == 5
    assert all(it["trace_id"] == "tr_only" for it in res["items"])


async def test_query_logs_filter_level_module_keyword(ctx):
    db = ctx
    await _seed_logs(db, trace="tr_kw", task="tk_k", module="modk", level="WARN")
    res = await obs_svc.query_logs(db, module="modk", level="WARN", keyword="keyword1")
    assert res["total"] >= 1
    assert all(it["module"] == "modk" and it["level"] == "WARN" for it in res["items"])


async def test_query_logs_filter_error_code(ctx):
    db = ctx
    await _seed_logs(db, trace="tr_ec", task="tk_e", module="m", level="ERROR")
    res = await obs_svc.query_logs(db, error_code="MOD_001")
    assert res["total"] >= 1
    assert all(it["error_code"] == "MOD_001" for it in res["items"])


async def test_query_logs_pagination_order(ctx):
    db = ctx
    await _seed_logs(db, trace="tr_pg", task="tk_p", module="m", level="INFO")
    res = await obs_svc.query_logs(db, trace_id="tr_pg", limit=2, offset=0)
    assert len(res["items"]) == 2
    assert res["total"] == 5


# ---------- F · get_trace_tree + orphan_spans ----------

async def test_trace_tree_returns_orphan_spans(ctx):
    db = ctx
    await obs_svc.log_event(db, module="a", event="span.root", trace_id="tr_orph",
                            span_id="s_root", parent_span_id="", message="root")
    await obs_svc.log_event(db, module="a", event="span.child", trace_id="tr_orph",
                            span_id="s_child", parent_span_id="s_root", message="child")
    await obs_svc.log_event(db, module="a", event="span.orphan", trace_id="tr_orph",
                            span_id="s_orphan", parent_span_id="s_missing", message="orphan")
    res = await obs_svc.get_trace_tree(db, "tr_orph")
    assert res["count"] == 3
    # 前端链路视图已消费 orphan_spans；旧实现未返回 ⇒ 断链提示永不出现
    assert "orphan_spans" in res
    assert "s_orphan" in res["orphan_spans"]


# ---------- G · get_task_timeline ----------

async def test_task_timeline(ctx):
    db = ctx
    await obs_svc.log_event(db, module="task_registry", event="task.created",
                            task_id="tk_tl", status="running", message="created")
    await obs_svc.log_event(db, module="task_registry", event="task.completed",
                            task_id="tk_tl", status="completed", message="done")
    res = await obs_svc.get_task_timeline(db, "tk_tl")
    assert res["count"] == 2
    assert res["items"][0]["event"] == "task.created"
    assert res["items"][1]["event"] == "task.completed"


# ---------- H · detect_orphan_spans ----------

def test_detect_orphan_spans():
    items = [
        {"span_id": "a", "parent_span_id": ""},
        {"span_id": "b", "parent_span_id": "a"},
        {"span_id": "c", "parent_span_id": "zzz"},
    ]
    orphans = obs_svc.detect_orphan_spans(items)
    assert "c" in orphans and "b" not in orphans and "a" not in orphans


def test_detect_orphan_spans_empty():
    assert obs_svc.detect_orphan_spans([]) == []


# ---------- I · cleanup_old_logs ----------

async def test_cleanup_old_logs_by_level(ctx):
    db = ctx
    # 直接写老时间戳的 DEBUG 行与较新的 INFO 行
    await db.execute(
        "INSERT INTO system_logs (level,module,event,trace_id,message,timestamp)"
        " VALUES ('DEBUG','m','e','tr_old_d','old debug',"
        " datetime('now','localtime','-30 days'))")
    await db.execute(
        "INSERT INTO system_logs (level,module,event,trace_id,message,timestamp)"
        " VALUES ('ERROR','m','e','tr_old_e','old error',"
        " datetime('now','localtime','-30 days'))")
    await db.execute(
        "INSERT INTO system_logs (level,module,event,trace_id,message,timestamp)"
        " VALUES ('INFO','m','e','tr_new_i','new info',"
        " datetime('now','localtime','-1 days'))")
    await db.commit()
    n = await obs_svc.cleanup_old_logs(db, retention_days=30)
    cur = await db.execute("SELECT COUNT(*) FROM system_logs")
    remaining = (await cur.fetchone())[0]
    # DEBUG 保留 7 天 ⇒ 30 天前的 DEBUG 被删；ERROR 保留 90 天 ⇒ 保留；INFO 30 天 ⇒ 保留
    assert n >= 1
    assert remaining >= 2
    cur = await db.execute("SELECT COUNT(*) FROM system_logs WHERE level='DEBUG'")
    assert (await cur.fetchone())[0] == 0


# ---------- J · get_metrics ----------

async def test_get_metrics_basic(ctx):
    db = ctx
    await obs_svc.log_event(db, module="a", event="span.x", trace_id="tr_m1",
                            span_id="s1", duration_ms=10, status="ok", message="ok")
    await obs_svc.log_event(db, module="a", event="span.y", trace_id="tr_m1",
                            span_id="s2", parent_span_id="s1", duration_ms=200,
                            status="ok", message="ok2")
    await obs_svc.log_event(db, module="b", event="err", trace_id="tr_m2",
                            error_code="B_1", level="ERROR", message="bad")
    m = await obs_svc.get_metrics(db, days=7)
    assert m["total"] >= 3
    assert m["latency"]["count"] >= 2
    assert m["latency"]["p99"] >= 200
    assert any(mod["module"] == "a" for mod in m["modules"])
    assert any(e["code"] == "B_1" for e in m["top_errors"])


# ---------- K · evaluate_alerts ----------

async def test_evaluate_alerts_error_rate(ctx):
    db = ctx
    for i in range(10):
        await obs_svc.log_event(db, module="m", event="ok", trace_id="tr_ok%d" % i,
                                level="INFO", message="ok")
        await obs_svc.log_event(db, module="m", event="bad", trace_id="tr_bad%d" % i,
                                level="ERROR", message="bad")
    res = await obs_svc.evaluate_alerts(db, days=1, error_rate_threshold=0.1,
                                        p99_threshold_ms=99999, broken_rate_threshold=0.5)
    assert any(a["type"] == "error_rate" for a in res["alerts"])


# ---------- L · Span ----------

async def test_span_records_event(ctx):
    db = ctx
    # Span 写日志到结构化文件（logger），system_logs 由测试直接校验 formatter 路径
    with obs_utils.span("test.span", kind="db") as sp:
        assert sp.span_id
        assert sp.parent == ""
    # 进入 Span 后 span_id 被设置；退出后恢复
    assert obs_utils.context_snapshot()["span_id"] == ""


def test_span_nested_parent():
    with obs_utils.span("outer", kind="internal"):
        outer_id = obs_utils.context_snapshot()["span_id"]
        assert outer_id
        with obs_utils.span("inner", kind="internal"):
            inner = obs_utils.context_snapshot()
            assert inner["span_id"]
            assert inner["parent_span_id"] == outer_id


# ---------- M · AsyncStructuredFileHandler ----------

async def test_async_file_handler_writes_jsonl(tmp_path):
    handler = obs_utils.AsyncStructuredFileHandler(tmp_path, batch_size=2, flush_interval=0.05)
    rec = logging.LogRecord(name="m", level=logging.INFO, pathname=__file__,
                            lineno=1, msg="hello", args=(), exc_info=None)
    handler.emit(rec)
    handler.emit(rec)
    await asyncio.sleep(0.2)
    handler.close()
    await asyncio.sleep(0.1)
    files = list(tmp_path.glob("*.jsonl"))
    assert files, "应写出 JSONL 文件"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    assert json.loads(lines[0])["message"] == "hello"


async def test_async_file_handler_queue_overflow_drops(tmp_path):
    handler = obs_utils.AsyncStructuredFileHandler(
        tmp_path, queue_size=5, batch_size=2, flush_interval=5.0)
    # 塞满队列（worker 因 flush_interval 长不消费）
    for i in range(20):
        rec = logging.LogRecord(name="m", level=logging.INFO, pathname=__file__,
                                lineno=1, msg="m%d" % i, args=(), exc_info=None)
        handler.emit(rec)
    assert handler.dropped_count >= 1
    handler.close()


def test_cleanup_expired_logs(tmp_path):
    old = tmp_path / "2020-01-01.jsonl"
    new = tmp_path / (time.strftime("%Y-%m-%d") + ".jsonl")
    old.write_text("x\n", encoding="utf-8")
    new.write_text("y\n", encoding="utf-8")
    removed = obs_utils.cleanup_expired_logs(
        tmp_path, retention_days={"ERROR": 90, "INFO": 30, "DEBUG": 7})
    assert removed == 1
    assert not old.exists()
    assert new.exists()


# ---------- O · 任务生命周期 → system_logs ----------

async def test_task_lifecycle_writes_system_logs(ctx):
    """register_task → created；finish_task → completed 应写 system_logs。"""
    from app.services.ai import task_registry as tr
    db = ctx
    pid = "pid" + uuid.uuid4().hex[:8]
    sid = "sid" + uuid.uuid4().hex[:8]
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "s"))
    await db.commit()
    tid = await tr.register_task("outline", project_id=pid, scheme_id=sid)
    assert tid
    # 内存态可查
    cur = await db.execute("SELECT COUNT(*) FROM system_logs WHERE task_id=?", (tid,))
    n = (await cur.fetchone())[0]
    assert n >= 1, "register_task 应写 task.created 到 system_logs"
    await tr.finish_task(tid, "completed", "done")
    await asyncio.sleep(0.05)
    cur = await db.execute(
        "SELECT event, status FROM system_logs WHERE task_id=? ORDER BY id", (tid,))
    events = await cur.fetchall()
    assert any(r[0] == "task.completed" for r in events)


async def test_task_control_writes_status_events(ctx):
    from app.services.ai import task_registry as tr
    db = ctx
    pid = "pid" + uuid.uuid4().hex[:8]
    sid = "sid" + uuid.uuid4().hex[:8]
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "s"))
    await db.commit()
    tid = await tr.register_task("content", project_id=pid, scheme_id=sid)
    assert tr.request_control(tid, "pause") is True
    await tr.set_task_status(tid, "paused")
    await asyncio.sleep(0.05)
    cur = await db.execute(
        "SELECT event FROM system_logs WHERE task_id=? AND event='task.paused'", (tid,))
    assert await cur.fetchone() is not None


# ---------- P · HTTP 请求根 span ----------

async def test_record_http_request_root_span(ctx):
    db = ctx
    await obs_svc.record_http_request(db, trace_id="tr_http", method="GET",
                                      path="/api/v1/projects", status_code=200,
                                      duration_ms=12.5, client="127.0.0.1")
    res = await obs_svc.get_trace_tree(db, "tr_http")
    assert res["count"] == 1
    it = res["items"][0]
    assert it["event"] == "http.request"
    assert it["module"] == "http"
    assert it["status"] == "200"
    assert it["request_id"] == "tr_http"
    assert json.loads(it["detail"])["path"] == "/api/v1/projects"


def test_middleware_skip_guard_in_test_context():
    """pytest 上下文下 _should_skip_obs_db_write 必须为 True（避免污染生产库）。"""
    from app.middleware import _should_skip_obs_db_write
    assert _should_skip_obs_db_write() is True


# ---------- Q · 错误码规范 ----------

def test_make_error_code_module_prefix():
    assert obs_svc.make_error_code("parse", 1) == "PARSE_001"
    assert obs_svc.make_error_code("outline", 2) == "OUTLINE_002"
    assert obs_svc.make_error_code("content", 3) == "CONTENT_003"
    assert obs_svc.make_error_code("export", 4) == "EXPORT_004"
    assert obs_svc.make_error_code("chart", 5) == "CHART_005"


def test_make_error_code_unknown_module_falls_back():
    # 未知模块回落到大写模块名（fail-soft）
    assert obs_svc.make_error_code("mystery", 9) == "MYSTERY_009"


# ---------- R · 异步批量缓冲器（SystemLogBuffer） ----------

async def test_system_log_buffer_enqueue_flush(ctx):
    db = ctx
    buf = obs_svc.SystemLogBuffer(batch_size=3, flush_interval=0.05)
    assert buf.start() is True
    for i in range(4):
        buf.enqueue(level="INFO", module="m", event="buf.e%d" % i,
                    trace_id="tr_buf", task_id="tk_buf", message="msg%d" % i)
    await asyncio.sleep(0.4)
    buf.stop()
    await asyncio.sleep(0.2)
    cur = await db.execute("SELECT COUNT(*) FROM system_logs WHERE trace_id='tr_buf'")
    n = (await cur.fetchone())[0]
    assert n >= 1, "缓冲器应把事件批量写入 system_logs"


async def test_system_log_buffer_drops_on_full(ctx):
    db = ctx
    buf = obs_svc.SystemLogBuffer(batch_size=100, flush_interval=60.0, queue_size=5)
    assert buf.start() is True
    for i in range(20):
        buf.enqueue(level="INFO", module="m", event="buf.drop", message="m%d" % i)
    assert buf.dropped_count >= 1, "队满应丢弃并计数（限速防拖垮主流程）"
    buf.stop()
    await asyncio.sleep(0.1)


async def test_bulk_log_events_batch_and_redact(ctx):
    db = ctx
    n = await obs_svc.bulk_log_events(db, [
        {"level": "INFO", "module": "m", "event": "b.1", "trace_id": "tr_bb",
         "message": "hello", "detail": {"api_key": "sk-abc123456789"}},
        {"level": "ERROR", "module": "m", "event": "b.2", "trace_id": "tr_bb",
         "error_code": "AI_001", "retryable": True, "message": "bad"},
    ])
    assert n == 2
    cur = await db.execute("SELECT message, detail FROM system_logs WHERE trace_id='tr_bb' AND event='b.1'")
    msg, detail = await cur.fetchone()
    assert "sk-abc123456789" not in msg
    assert "sk-abc123456789" not in (detail or "")
    cur = await db.execute("SELECT retryable, error_code FROM system_logs WHERE event='b.2'")
    retryable, code = await cur.fetchone()
    assert retryable == 1 and code == "AI_001"


async def test_bulk_log_events_empty(ctx):
    db = ctx
    n = await obs_svc.bulk_log_events(db, [])
    assert n == 0


# ---------- S · AI 调用事件 → system_logs（_log_audit 接线） ----------

async def test_ai_call_event_wired_to_buffer(ctx):
    """provider_factory._log_audit 应把 AI 调用事件非阻塞入 system_logs。"""
    from app.services.ai import provider_factory as pf
    db = ctx
    buf = obs_svc.SystemLogBuffer(batch_size=2, flush_interval=0.05)
    assert buf.start() is True
    # 临时把单例替换为受控实例（避免污染全局）
    orig = obs_svc.system_log_buffer
    obs_svc.system_log_buffer = buf
    try:
        await pf._log_audit("test-provider", "test-model", "chat", 0.25, True,
                            prompt_tokens=100, completion_tokens=50,
                            cached_tokens=10, scene="outline_draft")
        await asyncio.sleep(0.5)
    finally:
        obs_svc.system_log_buffer = orig
        buf.stop()
    await asyncio.sleep(0.2)
    cur = await db.execute("SELECT * FROM system_logs WHERE module='ai' AND event='ai.call'")
    row = await cur.fetchone()
    assert row is not None, "AI 调用应写入 system_logs（ai.call）"
    d = dict(zip([dd[0] for dd in cur.description], row))
    assert d["message"].startswith("AI chat")
    assert "test-provider" in d["message"]
    assert d["status"] == "ok"


# ---------- T · middleware 未处理异常 → system_logs（GEN_500） ----------

async def test_middleware_error_writes_error_span(ctx):
    """未处理异常应写入 system_logs（event=error、error_code=GEN_500、带 trace_id）。"""
    import app.middleware as mw
    from app.middleware import RequestLogMiddleware

    async def boom_app(scope, receive, send):
        raise RuntimeError("boom-test")

    wrapped = RequestLogMiddleware(boom_app)
    orig = mw._should_skip_obs_db_write
    mw._should_skip_obs_db_write = lambda: False
    try:
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "method": "GET", "path": "/api/v1/projects",
                 "headers": [], "client": ("127.0.0.1", 1)}
        with pytest.raises(RuntimeError):
            await wrapped(scope, receive, send)
        await asyncio.sleep(0.4)
        cur = await ctx.execute(
            "SELECT * FROM system_logs WHERE event='error' AND error_code='GEN_500'")
        row = await cur.fetchone()
        assert row is not None, "未处理异常应写入 system_logs（GEN_500 / error_layer=api）"
        d = dict(zip([dd[0] for dd in cur.description], row))
        assert d["error_layer"] == "api"
        assert d["trace_id"], "error span 应携带 trace_id"
    finally:
        mw._should_skip_obs_db_write = orig
