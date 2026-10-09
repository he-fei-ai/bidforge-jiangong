"""pytest 公共 fixture

核心策略：
- 使用 aiosqlite 内存数据库，每个测试函数独立连接，互不干扰
- monkeypatch app.db.get_conn 及所有已导入的引用，使被测代码拿到测试连接
- autouse fixture 在每个测试前重置模块级全局状态（缓存、订阅者）
"""
import aiosqlite
import os
import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
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
    for _mod_name in ("app.routers.sse_handlers", "app.routers.sse_checkpoint",
                      "app.services.repair_record",
                      "app.routers.system", "app.routers.outline_library"):
        try:
            _mod = importlib.import_module(_mod_name)
        except Exception:  # 模块缺失/导入失败不应影响其它用例
            continue
        if hasattr(_mod, "get_conn"):
            monkeypatch.setattr(_mod, "get_conn", fake_get_conn)
        if hasattr(_mod, "get_read_conn"):
            monkeypatch.setattr(_mod, "get_read_conn", fake_get_conn)

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
    # ✅ 流式能力记忆（provider_factory._stream_unsupported_at）同样是进程级全局：
    #    前一个用例判定的「该 provider 不支持流式」会污染后续用例的调用路径。
    pf.reset_stream_capability()
    # ✅ 配额冷却（2026-09-22 · O2）同样是进程级全局：
    #    前一个用例触发的 402/429 冷却会让后续用例的 provider 被跳过。
    pf.reset_quota_cooldown()
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