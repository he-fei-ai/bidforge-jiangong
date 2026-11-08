"""pytest 公共 fixture

核心策略：
- 使用 aiosqlite 内存数据库，每个测试函数独立连接，互不干扰
- monkeypatch app.db.get_conn 及所有已导入的引用，使被测代码拿到测试连接
- autouse fixture 在每个测试前重置模块级全局状态（缓存、订阅者）
"""

import os
import tempfile
import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from app.schema_sql import SCHEMA_SQL

# ✅ 图表缓存目录隔离（2026-10-09 · NTFS 共享冲突根治）：
#    默认图表渲染缓存位于 `data/_exports/charts`，与常驻后端服务(uvicorn)共享。
#    同一目录下 PNG 缓存文件被并发读写/替换时，Windows 会报共享冲突，被 Python
#    映射成 `[Errno 13] Permission denied`（而非真实的 ACL 拒绝）。将测试进程的
#    图表缓存重定向到独立临时目录，彻底消除与常驻服务/跨机器遗留缓存文件的争用。
#    仅在未显式指定时设置，CI/手动可仍用 BIDFORGE_CHART_CACHE_DIR 覆盖。
_TEST_CHART_CACHE = os.path.join(tempfile.gettempdir(), "bidforge_test_chart_cache")
os.environ.setdefault("BIDFORGE_CHART_CACHE_DIR", _TEST_CHART_CACHE)


@pytest.fixture(autouse=True, scope="session")
def _isolate_chart_cache_dir():
    """双保险：确保图表缓存单例命中隔离目录。

    部分模块以 `from app.config import CHARTS_DIR` 在导入期绑定取值，
    本 fixture 在 session 起点强制对齐 config.CHARTS_DIR 并重建 ChartCache
    单例，避免个别导入期绑定的引用仍指向生产目录。
    """
    import app.config as _cfg

    _cfg.CHARTS_DIR = Path(os.environ["BIDFORGE_CHART_CACHE_DIR"]).resolve()
    try:
        import app.services.ai.mermaid_renderer as _mr

        _mr._chart_cache = _mr.ChartCache()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _restore_app_db_path():
    """R74（G4）：集中治理 `DB_PATH` 裸赋值泄漏（跨用例污染的同族根因）。

    全仓约 20 个测试文件用 `_appdb.DB_PATH = tmp_path/...` 切隔离库，
    其中大部分**不还原** —— 同一 pytest 进程里后续所有直接读
    DB_PATH 的用例都会看到已失效的临时路径。不逐改 20 个文件（跨面风
    险大），而是在每条用例结束后把模块级 DB_PATH 复位到开局值；
    app.db.get_conn 已有路径失配重连逻辑（db.py `_conn_path != DB_PATH`），
    复位变量即自动换回正确连接。
    """
    import app.db as _appdb

    original = _appdb.DB_PATH
    yield
    if _appdb.DB_PATH != original:
        _appdb.DB_PATH = original


# ✅ 2026-11-08（写池计数守恒探针）：`_db_pool_total` 是获取闸门
#    （`total < _POOL_MAX_SIZE` 才允许新建写连接），而它只增不减 —— 任何一条
#    「预留后未配对回退」的路径都会让有效容量永久缩水，累计到上限后获取路径
#    永久 `wait()` 且无连接可归还（死锁，P1）。历史证据：全量顺序下
#    `test_lifespan_shutdown_writes_failed_before_close_db` 的
#    `assert db._db_pool_total == 0` 曾收到 1，单跑却绿 —— 说明是**前序用例**
#    留下的漂移。本探针逐用例比对「总量 = 池内 + 在途」三方守恒，漂移即点名。
#
#    为使后续用例能独立判定（否则首个泄漏者之后全员连坐红），检查失败后把本
#    用例遗留的漂移量从计数里剔除；这是测试隔离，不是生产回退路径。
@pytest.fixture(autouse=True)
def _pool_accounting_invariant(request):
    import app.db as _appdb

    yield
    total = _appdb._db_pool_total
    pool_n = len(_appdb._db_pool)
    borrowed_n = len(_appdb._db_pool_borrowed)
    held = pool_n + borrowed_n
    if total != held:
        _appdb._db_pool_total = held
        pytest.fail(
            "写池计数漂移（用例 %s）：_db_pool_total=%d，但池内 %d + 在途 %d = %d"
            % (request.node.nodeid, total, pool_n, borrowed_n, held),
            pytrace=False,
        )


@pytest_asyncio.fixture
async def db_conn(monkeypatch):
    """内存 SQLite 连接，初始化完整 schema，并 patch get_conn

    - :memory: 每次连接独立，无磁盘 IO，测试快
    - row_factory = aiosqlite.Row，与生产一致（dict(row) 可用）
    - patch app.db.get_conn 以及 provider_factory / task_registry 中
      已绑定的 get_conn 引用，确保被测代码拿到的是测试连接
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    # ✅ 与生产 init_db() 对齐：再跑一遍兼容性迁移补历史遗留列
    #    （如 sections.*_json 图表列 —— SCHEMA_SQL 未定义、仅由迁移补列）。
    #    否则测试库缺列：读这些列的代码（charts.list_charts）在生产能跑、
    #    在测试里却 `no such column`，测试环境与生产 schema 失真。
    from app.db import _migrate

    await _migrate(conn)
    await conn.commit()

    async def fake_get_conn():
        return conn

    # ✅ 2026-11-08（夹具缺口）：`45b697d` 起 `_release_read_conn` 对「非本池借出」
    #    的连接一律 `close()`，而本夹具只 patch 了 get_read_conn —— 被测代码从
    #    get_read_conn（已 patch 为本内存连接）拿到 conn 后，finally 里调真实
    #    release_read_conn，会把本夹具**唯一的**内存连接关掉，后续查询随即
    #    `ValueError: no active connection`（段一 #1/#2 的确定性根因）。
    #
    #    修法选择：补 patch release_read_conn（no-op），而非把夹具连接塞进
    #    `_read_pool_borrowed`。判断依据（已用 git grep 枚举全仓调用点）：
    #    生产 `release_read_conn` 的所有调用点
    #      outline_library.py:78,102 / sse_checkpoint.py:159 /
    #      sse_handlers.py:7213,7226,7265 / system.py:138,252,313 /
    #      outline_integrity.py:219
    #    全部与同函数内的 `conn = await get_read_conn()`（或 read_db 依赖）配对，
    #    **不存在跨池归还**；即生产入口只有 get_read_conn 这一条。补 patch 与
    #    生产真实路径等价、可接受。若改为把夹具连接加入 `_read_pool_borrowed`，
    #    真实 release 会把这条内存连接 append 进真实读池，造成池计数漂移与
    #    跨事件循环污染（测试结束后连接仍挂在生产池里），反而偏离生产语义。
    async def fake_release_read_conn(_conn):
        return None

    # ✅ P10 修复配套（2026-09-23）：provider_factory 的审计批量落库、
    #    save_ai_config 多语句事务均走 write_tx_conn（独立写池连接）。若不在
    #    此处一并 patch 为内存连接，这些写操作会落到真实库而非本测试的
    #    :memory: 库，导致 test_perf_bottlenecks 等断言“落库后可见”的用例失败。
    #    异常时回滚，与生产 write_tx_conn 语义保持一致。
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_write_tx_conn():
        try:
            yield conn
        except BaseException:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise

    # patch app.db 模块本身
    import app.db

    monkeypatch.setattr(app.db, "get_conn", fake_get_conn)
    monkeypatch.setattr(app.db, "write_tx_conn", fake_write_tx_conn)
    # 函数内局部 `from app.db import release_read_conn`（如 outline_integrity）只认
    # app.db 模块属性，必须在此处一并 patch。
    monkeypatch.setattr(app.db, "release_read_conn", fake_release_read_conn)

    # patch 各业务模块中 from app.db import get_conn 绑定的引用
    import app.services.ai.provider_factory as _pf

    monkeypatch.setattr(_pf, "get_conn", fake_get_conn)
    monkeypatch.setattr(_pf, "write_tx_conn", fake_write_tx_conn)

    import app.services.ai.task_registry as _tr

    monkeypatch.setattr(_tr, "get_conn", fake_get_conn)

    # ✅ G12-7（2026-09-20 · 测试隔离修复）：`from app.db import get_conn` 是
    #    **导入期绑定**，只 patch app.db 对"已持有函数引用"的模块无效。
    #    sse_handlers / repair_record / system / outline_library 等模块的 DB 访问
    #    （_save_task_checkpoint、目录部分成果 checkpoint、僵尸任务兜底等）会落到
    #    真实库而非本测试的内存库，表现为**顺序相关**的偶发失败
    #    （单文件跑全绿、全量跑 KeyError）。统一在下方循环里一并 patch。

    # ✅ BUG 修复（测试隔离，2026-09-21）：`from app.db import get_conn` 是
    #    **导入期绑定** —— 只 patch app.db 对"已持有函数引用"的模块无效。
    #    若某测试文件在模块顶层 `import app.routers.sse_handlers`，该模块在
    #    整个 session 内都持有原函数，其写操作（如目录部分成果 checkpoint）
    #    会落到真实库而非本测试的内存库，表现为**顺序相关**的偶发失败：
    #    test_outline_deep_audit 之后跑 test_outline_stopped_recovery 必失败
    #    （checkpoint_json 读回仍是 "{}"，断言 ckpt["kind"] 抛 KeyError）；
    #    单独跑该文件却全绿。这里把所有存在模块级绑定的模块一并 patch。
    import importlib

    for _mod_name in (
        "app.routers.sse_handlers",
        "app.routers.sse_checkpoint",
        "app.services.repair_record",
        "app.routers.system",
        "app.routers.outline_library",
    ):
        try:
            _mod = importlib.import_module(_mod_name)
        except Exception:  # 模块缺失/导入失败不应影响其它用例
            continue
        if hasattr(_mod, "get_conn"):
            monkeypatch.setattr(_mod, "get_conn", fake_get_conn)
        if hasattr(_mod, "get_read_conn"):
            monkeypatch.setattr(_mod, "get_read_conn", fake_get_conn)
        # `from app.db import release_read_conn` 同为导入期绑定：不 patch 的话，
        # finally 里的真实 release 会把本夹具的内存连接 close 掉（见上方长注释）。
        if hasattr(_mod, "release_read_conn"):
            monkeypatch.setattr(_mod, "release_read_conn", fake_release_read_conn)

    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def reset_provider_config_cache():
    """每个测试前重置 provider_factory 的 _config_cache

    _config_cache 是模块级全局 dict，若不重置，前一个测试写入的缓存
    会影响后续测试的缓存命中/过期判定。
    """
    import app.services.ai.provider_factory as pf

    pf._config_cache["data"] = None
    pf._config_cache["ts"] = 0.0
    pf._fallback_cache["data"] = None
    pf._fallback_cache["ts"] = 0.0
    # ✅ 2026-09-23：场景模型路由缓存（ai_scene_routes）同为进程级全局，
    #    前一个用例配置的路由会让后续用例的 chat_with_fallback 选错配置。
    pf._scene_route_cache["data"] = None
    pf._scene_route_cache["ts"] = 0.0
    # ✅ 2026-09-23：当前生效环境缓存（多环境）同为进程级全局，
    #    前一个用例切换过的环境会让后续用例的主配置/降级链被错误过滤。
    pf._env_cache["data"] = None
    pf._env_cache["ts"] = 0.0
    pf._audit_buffer.clear()
    # ✅ 遗留项加固（2026-10-11 · 全量跑随机失败治理）：只清了缓冲、没清
    #    **刷新时间戳**，是 `test_reliability_failure_still_buffers_audit`
    #    在全量会话里偶发变红的直接成因。
    #    机理：`_log_audit` 追加一行后立刻看 `due = now - _audit_last_flush["ts"]
    #    >= _AUDIT_FLUSH_INTERVAL(10s)`，为真就**当场把缓冲刷掉**。该 ts 只在
    #    真正 flush 时更新。全量跑 9 分钟，中间只要有一段 >10s 没人碰审计路径，
    #    下一条 `_log_audit` 就会顺带 flush ⇒ 断言 `len(buffer)==before+1`
    #    必然失败；单跑该文件（几十 ms）时永远绿 —— 典型的"耗时相关"假失败。
    #    这里把 ts 重置为"刚刚"，让每个用例在时间意义上相互独立。
    #    ⚠️ 不破坏有意改写 ts 的用例：既有两派写法（`= 0.0` 强制到期、
    #    `= 1e18` 永久阻止）都写在用例内部，晚于本 fixture 执行，不受影响。
    # ⚠️ A/B 开关：设 BIDFORGE_TEST_KEEP_AUDIT_TS=1 可临时还原旧行为，
    #    用于验证本重置确为该随机失败的成因（还原后必红）。
    if not os.environ.get("BIDFORGE_TEST_KEEP_AUDIT_TS"):
        pf._audit_last_flush["ts"] = time.time()
    # ✅ 流式能力记忆（provider_factory._stream_unsupported_at）同样是进程级全局：
    #    前一个用例判定的「该 provider 不支持流式」会污染后续用例的调用路径。
    pf.reset_stream_capability()
    # ✅ 配额冷却（2026-09-22 · O2）同样是进程级全局：
    #    前一个用例触发的 402/429 冷却会让后续用例的 provider 被跳过。
    pf.reset_quota_cooldown()
    # ✅ R110：思考吞噬冷却 + 全熔断快速失败也是进程级全局（2026-10-20）：
    #    前一个用例标记的 60s 冷却窗口会让后续用例的候选被跳过或快速失败。
    pf.reset_thinking_dead()
    pf.reset_all_circuit_fast_fail()
    # ✅ R169：可靠性滑窗 + 确定性死配置连击（R108）同为进程级全局——
    #    前一个用例触发的 ≥3 次 404 连击会让后续依赖同身份键用例的候选
    #    被静默判死剔除（顺序相关偶发红），与 thinking-dead 污染同族。
    pf.reset_reliability()
    # ✅ 2026-11-05：自适应对冲延迟样本（进程级全局）同样需要隔离，
    #    否则前一个用例灌入的 P90 会让后续对冲窗口取值不稳定。
    pf.reset_provider_latency()
    # ✅ 自适应并发控制器（2026-09-22 · 测试隔离修复）：进程级全局。
    #    前面用例的失败调用（402/429 等）会把并发降到 min_c=1 并清不掉，
    #    导致后续依赖并发语义的测试（如 test_perf_content_pipeline 的 hedge）
    #    中两个候选串行排队、对冲失效 —— 表现为顺序相关的偶发失败。
    from app.services.ai.workflows_base import concurrency_controller as _cc

    _cc.set_concurrency(_cc.max_c if _cc.target > _cc.max_c else max(1, _cc.target))
    _cc._window.clear()
    _cc._consecutive_429 = 0


@pytest.fixture(autouse=True)
def reset_task_registry_state():
    """每个测试前重置 task_registry 的内存全局状态

    _tasks 是模块级全局，跨测试会残留任务状态，导致 is_stopped 误判。
    （2026-10-04 起 `_subscribers` 死链路已清理，此处同步删除。）
    """
    import app.services.ai.task_registry as tr

    tr._tasks.clear()


def pytest_sessionfinish(session, exitstatus):
    """会话结束统一关闭 app.db 的进程级 aiosqlite 连接（2026-11-07）。

    背景：`app.db.init_db()` 会把全局单连接 `_conn` 建出来并常驻模块全局；
    生产由 lifespan 的 `close_db()` 收尾（main.py），但 pytest 会话没有对应
    的关闭点。测试文件（约 78 个）直接 `await init_db()` 后即结束，`_conn`
    一直被模块引用，aiosqlite 的 `_connection_worker_thread` 是**非守护线程**，
    用例全绿后解释器会卡在 `threading._shutdown` 等待它 —— 表现为「测试通过
    但命令永远不退出」，长跑还会累积僵死进程。

    这里在会话结束（所有 fixture teardown 之后）补上与生产一致的
    `close_db()`，关闭 `_conn` + 写池 + 读池；并断言没有残留的 aiosqlite
    工作线程，残留时把会话置为失败（避免下一次又静默挂死）。
    """
    import asyncio
    import threading

    try:
        import app.db as _appdb
    except Exception:  # 收集失败等场景下模块可能不可用，不能因此掩盖原始失败
        return

    try:
        asyncio.run(_appdb.close_db())
    except Exception as e:  # 清理路径失败仅告警，不覆盖既有用例结果
        print(f"[conftest] 会话结束关闭 app.db 连接失败（忽略）: {e}")

    leaked = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive() and "_connection_worker_thread" in t.name
    ]
    if leaked:
        print(f"[conftest] 会话结束后仍有残留 aiosqlite 工作线程: {[t.name for t in leaked]}")
        # 会话级断言：用例可能全绿，但进程无法正常退出，必须让 CI 可见地失败。
        session.exitstatus = 1
