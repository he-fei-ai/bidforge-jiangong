"""数据库连接（aiosqlite，含死线程检测与自动建表）"""
import asyncio
import datetime as _dt
import logging
import os
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config import DATA_DIR, settings
from app.schema_sql import SCHEMA_SQL

logger = logging.getLogger("db")

DB_PATH = DATA_DIR / "scheme_assistant.db"

# ---------- USB 外置盘抖动兜底（2026-09-14） ----------
# 现象：数据库放在外置 USB 硬盘（J:），Windows 事件日志出现
#   disk 警告 + Ntfs {延迟写入失败}，SQLite 端集中报
#   sqlite3.OperationalError: disk I/O error，导致 REST 端点 500。
# 修复：
#   1) 探活从 SELECT 1 升级为 PRAGMA wal_checkpoint(PASSIVE)，强制 WAL/主库
#      IO 往返——SELECT 1 只查内存，命中不了"写入时才抖"的场景；
#   2) 请求侧 db.execute/commit 抛 disk I/O error 时，在连接上打
#      _poisoned 标记；归还连接时若标记置位则直接关闭剔除，不再入池。
#      这样坏连接不会污染下一个请求。
#   3) 取连接前也跑探针，连续 N 次坏即抛错，避免底层磁盘离线时空转。


def _is_disk_io_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "disk I/O error" in str(exc)


# ---------- 共享 DB 写重试（2026-09-23 · 正文生成深度审计） ----------
# 背景（logs/backend.log 2026-09-23 20:44~20:48，trace=9fe4498339da，真实生产）：
#   task_registry.update_progress 落库失败 ×4、保存目录生成 checkpoint 失败、
#   finish_task 终态落库失败: database is locked —— 而「任务终态」正是前端
#   pollTaskUntilTerminal 唯一等待的信号，写失败即任务在 DB 里永远是 running，
#   用户表现为「生成早就停了却再也存不了任何一章」，只能重启后端（当天 20:51
#   启动清理日志「已清理 1 个中断遗留任务（completed=0, failed=1）」即为证据）。
# 根因：全局单写连接 + WAL，前端轮询风暴与任务进度/终态写在同一连接上竞争
#   写锁；只有 routers/sse_handlers._retry_db_locked 做了重试，
#   task_registry / checkpoint / 审计批量落库三处完全裸奔。
# 修法：抽出共享助手 retry_db_op（幂等写语句专用），各处薄封装复用，
#   语义与 _retry_db_locked 完全一致（只重试 database is locked / disk I/O error，
#   指数退避，其它异常原样抛出）。默认 3 次重试、0.05s 起步，总等待 ≤0.35s，
#   对终态收尾路径的时延影响可忽略；调用方可按场景覆盖。
# ✅ BUG 修复（2026-09-23）：下方比对用 msg.lower()，而常量里的
#   "disk I/O error" 是混合大小写 → 永远不命中，disk I/O 重试形同虚设
#   （回归测试 test_content_deep_audit_20260923_b 抓出）。列表统一小写。
_RETRYABLE_DB_ERRORS = ("database is locked", "disk i/o error")


def is_retryable_db_error(exc: BaseException) -> bool:
    """是否为可重试的瞬态 SQLite 错误（database is locked / disk I/O error）。"""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return any(k in msg for k in _RETRYABLE_DB_ERRORS)


def safe_rowcount(cur, what: str = "") -> int:
    """UPDATE/DELETE 影响行数的**唯一出口**（R13 判空护栏，2026-09-29 收口）。

    AGENTS.md §5.5 的 R13：全局单写连接 + aiosqlite 下 ``db.execute()`` **可能
    返回 None**（连接/事务瞬时异常），直接 ``cur.rowcount`` 即
    ``AttributeError: 'NoneType' object has no attribute 'rowcount'``。

    此前全仓 7 处 ``.rowcount`` 各自裸取，命中后分两种结局：
      · 外层有 ``except Exception`` → 异常被吞、**写操作没生效却无任何日志**
        （``bid_analysis.clear_interrupted_items`` 即典型：中断遗留的 running
        项静默残留，UI 永远显示「运行中」、18 项永久判缺失）；
      · 外层无守卫 → 直接 500（批量审核状态、审计日志清理）。
    现在统一走本函数：None 时返回 0 并打 WARNING，调用方据此走「本次写未生效」
    分支 —— 既不再崩、也不再静默。

    Args:
        cur: ``db.execute()`` 的返回值（可能为 None）；
        what: 只用于告警文案（说明是哪笔写操作未生效），便于日志定位。
    """
    if cur is None:
        logger.warning("db.execute 返回 None（R13），%s 本次未生效",
                       what or "写操作")
        return 0
    # rowcount 为负（非行级语句/未知）时按「本次未影响行」处理，
    # 避免调用方 `if affected > 0` 之类的守卫被 -1 意外触发。
    n = cur.rowcount or 0
    return n if n > 0 else 0


async def retry_db_op(coro_factory, *, max_retries: int = 3,
                      base_delay: float = 0.05) -> object:
    """对瞬态 SQLite 错误做有限重试的共享助手。

    coro_factory 是无参 async 函数（每次重试重新调用以创建新 coroutine），
    必须对应**可安全重放**的语句（UPDATE/INSERT/commit 等幂等写）。

    默认参数针对「任务终态 / checkpoint / 审计」这类小写入：
    3 次 × 0.05/0.10/0.20s，最坏多耗 0.35s，远小于一次 AI 调用（数十秒）。
    不可重试错误与重试耗尽后原样抛出，调用方语义不变。
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except BaseException as e:
            if attempt >= max_retries or not is_retryable_db_error(e):
                raise
            last_exc = e
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "DB 操作遇到可重试错误（第 %d/%d 次），%.2fs 后重试: %s",
                attempt + 1, max_retries, delay, str(e)[:200])
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover - 循环内必 return 或 raise


async def _probe_conn(conn: aiosqlite.Connection) -> bool:
    """返回 True 表示连接可用；False 表示连接已死/底层 IO 抖动，必须剔除。"""
    try:
        # wal_checkpoint(PASSIVE) 会强制 WAL → 主库刷盘往返，
        # 是探测"写入时抖动"的最小代价探针。失败即视为坏连接。
        await conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return True
    except Exception as e:
        # ✅ BUG 修复（2026-09-26 · 「no active connection」类 cryptic 失败）：
        #    旧实现只把 disk I/O error 判为坏连接，**已关闭的连接**抛的
        #    ValueError("Connection closed" / "no active connection") 落在
        #    "非 IO 类异常 → 视为可用" 分支里被原样放行 —— 池/全局缓存把死连接
        #    发给业务代码，错误在**业务深处**才炸，且信息完全指不到根因
        #    （全量套件里 test_perf_content_pipeline / test_reasoning_effort 的
        #    8 个用例正是这么红的：单独跑 100% 通过）。连接被关闭是不可恢复的
        #    确定性失效，必须与 disk I/O 同等对待。
        if _is_dead_conn(conn) or _is_closed_conn_error(e):
            return False
        if _is_disk_io_error(e):
            return False
        # 非 IO 类异常（如 query_only 拒绝）不视为坏连接，交由上层语义处理
        return True


def _mark_poisoned(conn: aiosqlite.Connection) -> None:
    try:
        conn._poisoned = True
    except Exception:
        pass


def _is_poisoned(conn: aiosqlite.Connection) -> bool:
    return getattr(conn, "_poisoned", False)


# ---------- 死连接判定（2026-09-26） ----------
# ⚠️ aiosqlite 的 `Connection._conn` 是 **property**：连接关闭后访问它会抛
#    `ValueError("no active connection")`（aiosqlite/core.py: `_conn` property）。
#    这正是生产日志与全量测试里那条 cryptic 错误的出处 —— 症状离根因极远：
#    真正的失败是"缓存/池里躺着一个已关闭的连接"，却在业务代码深处才暴露。
#    因此判定必须**只读不抛**的属性（`_connection` / `_running`），绝不能碰 `_conn`。
def _is_dead_conn(conn) -> bool:
    """连接是否已关闭 / 已失去底层 sqlite 连接（只读属性，不抛异常）。"""
    if conn is None:
        return True
    if getattr(conn, "_connection", None) is None:
        return True
    if getattr(conn, "_running", True) is False:
        return True
    return False


def _is_closed_conn_error(exc: BaseException) -> bool:
    """异常是否表示「连接已关闭」（aiosqlite 的两种措辞都覆盖）。"""
    text = str(exc).lower()
    return "no active connection" in text or "connection closed" in text


# 探针连续失败上限：防止 J 盘彻底离线时空转
_PROBE_MAX_ATTEMPTS = 3

# ---------- class-level monkey-patch：写入路径 disk I/O error 自动标记 ----------
# 目的：REST 端点内 db.execute/commit 抛 disk I/O error 时给连接打 poisoned
# 标记，归还连接时直接剔除、不再复用，坏连接不污染下一个请求。
# 非 disk I/O 类异常一律原样抛出，业务语义不变。

def _mark_on_disk_io(exc: BaseException) -> bool:
    """是 disk I/O error 就标记（返回 True），否则 False。"""
    if isinstance(exc, sqlite3.OperationalError) and "disk I/O error" in str(exc):
        return True
    return False


_PatchedClasses = set()


def _patch_audiosqlite():
    """幂等地给 aiosqlite.Connection 打补丁：捕获 disk I/O error 时打标记。"""
    global _PatchedClasses
    if aiosqlite.Connection in _PatchedClasses:
        return
    original = aiosqlite.Connection.execute

    # original 是 function（类属性访问），self 会作为第一个参数自动传入
    async def patched_execute(self, sql, parameters=()):
        try:
            return await original(self, sql, parameters)
        except Exception as e:
            if _mark_on_disk_io(e):
                _mark_poisoned(self)
            raise

    aiosqlite.Connection.execute = patched_execute

    # commit 也需要打标记（写路径末尾的刷盘最容易触发 disk I/O error）
    if hasattr(aiosqlite.Connection, "commit"):
        original_commit = aiosqlite.Connection.commit

        async def patched_commit(self):
            try:
                return await original_commit(self)
            except Exception as e:
                if _mark_on_disk_io(e):
                    _mark_poisoned(self)
                raise

        aiosqlite.Connection.commit = patched_commit

    # executemany 与 executescript 同样处理（用默认参数捕获闭包）
    def _make_wrapper(orig_m):
        async def _wrapper(self, *args, **kwargs):
            try:
                return await orig_m(self, *args, **kwargs)
            except Exception as e:
                if _mark_on_disk_io(e):
                    _mark_poisoned(self)
                raise
        return _wrapper

    for method_name in ("executemany", "executescript"):
        if hasattr(aiosqlite.Connection, method_name):
            orig_m = getattr(aiosqlite.Connection, method_name)
            setattr(aiosqlite.Connection, method_name, _make_wrapper(orig_m))

    _PatchedClasses.add(aiosqlite.Connection)


_patch_audiosqlite()

_db_lock = asyncio.Lock()
_conn: aiosqlite.Connection | None = None
_conn_path: Path | None = None  # 记录 _conn 打开时的 DB_PATH（路径切换后需重连）
_last_health_check: float = 0.0
_HEALTH_CHECK_TTL = 30.0  # 性能优化：全局连接健康检查 TTL，避免每次 get_conn 都 SELECT 1


def _thread_alive() -> bool:
    try:
        loop = asyncio.get_running_loop()
        return loop.is_running()
    except RuntimeError:
        return False


async def get_conn() -> aiosqlite.Connection:
    global _conn, _last_health_check, _conn_path
    async with _db_lock:
        # BUG 修复（2026-09-13）：模块级 _conn 不感知 DB_PATH 切换——
        # 测试改 DB_PATH 后 get_conn 仍返回指向旧库的连接（写入真实库、
        # 与运行中的服务争锁，导致依赖隔离库的测试间歇失败）。
        if _conn is not None and _conn_path is not None and _conn_path != DB_PATH:
            try:
                await _conn.close()
            except Exception:
                pass
            _conn = None
            _conn_path = None
        if _conn is not None:
            # poisoned 检查必须先于 TTL 短路（否则坏连接在 30s TTL 窗口内仍被复用）
            # ✅ 2026-09-26：同样地，**已关闭**的连接也必须先于 TTL 短路被识别 ——
            #    旧实现只认 _poisoned 自定义标记，外部 close_db() / 异常路径 / 测试
            #    换库留下的死连接会在 30s 窗口内被原样发出去，调用方随后拿到
            #    aiosqlite 的 "no active connection"（错误信息完全指不到根因）。
            if _is_poisoned(_conn) or _is_dead_conn(_conn):
                logger.warning("全局连接已失效（poisoned/已关闭），重建连接")
                try:
                    await _conn.close()
                except Exception:
                    pass
                _conn = None
                _conn_path = None
            else:
                now = time.time()
                # TTL 健康检查：仅距上次检查超过 30s 时才做 PRAGMA wal_checkpoint(PASSIVE)
                # 探针（真实 IO 往返），避免 SELECT 1 只查内存命中不了"写入才抖"的
                # 场景；本地库极少断连，检查代价可忽略。
                if now - _last_health_check < _HEALTH_CHECK_TTL:
                    return _conn
                if not await _probe_conn(_conn):
                    logger.warning("数据库连接探活失败（disk I/O），重建连接")
                    try:
                        await _conn.close()
                    except Exception:
                        pass
                    _conn = None
                    _conn_path = None
                else:
                    _last_health_check = now
                    return _conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(DB_PATH)
        _conn_path = DB_PATH
        await _setup_connection(_conn)
        _last_health_check = time.time()
        return _conn


async def settle_global_conn(tag: str = "") -> None:
    """兜底收敛全局共享连接上的悬挂写事务（P0 断连事务泄漏，2026-09-23）。

    背景：SSE 事件流被取消时，task_registry / checkpoint 的写可能停在
    `execute()` 已 BEGIN、`commit()` 未发生的状态。全局单连接一旦挂着未提交
    写事务，后续所有写都要等它提交/回滚 → 全站 500 "database is locked"。
    事件流 finally 里调本函数（调用方一律配合 asyncio.shield：外层被取消时
    内层回滚仍在后台完成）即可解冻。

    安全性：仅当 `in_transaction` 为 True 才回滚（绝大多数调用是无害空操作）。
    极小概率误伤：恰有另一协程在同一全局连接上处于写事务中途 —— 该写本就
    即将因取消中断而丢失，回滚只是把「悬挂」变成「确定性丢弃」，并立即解冻
    全站（权衡后可接受，WARNING 日志便于追溯）。
    """
    conn = _conn
    if conn is None:
        return
    try:
        if not getattr(conn, "in_transaction", False):
            return
        await conn.rollback()
        logger.warning("已回滚全局连接上的悬挂写事务（tag=%s）——断连收尾兜底", tag)
    except Exception as e:
        logger.warning("收敛全局连接悬挂事务失败（tag=%s）: %s", tag, e)


def _self_heal_corrupt_wal() -> bool:
    """启动自愈：当「主库本身完好，但 -wal/-shm 损坏」导致整库被 SQLite 判定为
    ``database disk image is malformed`` 时，备份并丢弃损坏的 WAL/SHM，使主库以
    回滚日志模式正常打开，避免应用被永久卡死在 malformed（典型症状：所有 DB 操作
    报 ``no such table`` / ``database is locked`` / ``database disk image is malformed``，
    全局事实等重度依赖 DB 的模块整体「提取失败」）。

    仅在该操作**安全**时执行：
    - 必须存在 -wal 文件（WAL 才是可疑方，主库本身完好）；无 WAL 则不触碰；
    - 丢弃前把 主库+WAL+SHM 一并备份到 ``data/recov_bak_<时间戳>/``，**绝不删除主库**；
    - 若丢弃后主库仍不通过 ``PRAGMA integrity_check`` 自检，则把备份**还原**，
      保持原状、让应用以原始错误暴露根因，绝不雪上加霜。
    返回 True 表示本次确实丢弃了损坏的 WAL 并完成自愈。
    """
    if not DB_PATH.exists():
        return False
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    shm = DB_PATH.with_name(DB_PATH.name + "-shm")
    if not wal.exists():
        return False

    # 1) 只读自检：主库 + 当前 WAL 是否真的损坏
    try:
        _probe = sqlite3.connect(str(DB_PATH))
        try:
            _row = _probe.execute("PRAGMA integrity_check(1)").fetchone()
        finally:
            _probe.close()
        if _row == ("ok",):
            return False  # 主库完好且 WAL 未触发损坏，无需处理
    except sqlite3.DatabaseError:
        pass  # 落到下方自愈分支

    # 2) 备份（连同主库，便于事后追查/恢复 WAL 中未提交事务）
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_dir = DB_PATH.parent / f"recov_bak_{ts}"
    try:
        bak_dir.mkdir(parents=True, exist_ok=True)
        for _f in (DB_PATH, wal, shm):
            if _f.exists():
                shutil.copyfile(_f, bak_dir / _f.name)
    except OSError as e:
        logger.warning("WAL 自愈：备份失败，跳过自愈（保持原状）: %s", e)
        return False

    # 3) 丢弃损坏的 WAL/SHM（主库保留，数据不丢）
    try:
        if wal.exists():
            os.remove(wal)
        if shm.exists():
            os.remove(shm)
    except OSError as e:
        logger.warning("WAL 自愈：删除损坏 WAL 失败: %s", e)
        return False

    # 4) 自愈后自检：主库应能正常打开；若仍损坏则还原备份
    try:
        _probe2 = sqlite3.connect(str(DB_PATH))
        try:
            _row2 = _probe2.execute("PRAGMA integrity_check(1)").fetchone()
        finally:
            _probe2.close()
        if _row2 != ("ok",):
            raise sqlite3.DatabaseError(f"自愈后完整性仍异常: {_row2}")
    except sqlite3.DatabaseError as e:
        logger.warning("WAL 自愈：丢弃 WAL 后主库仍损坏，还原备份: %s", e)
        for _f in (DB_PATH, wal, shm):
            _bak = bak_dir / _f.name
            if _bak.exists():
                try:
                    shutil.copyfile(_bak, _f)
                except OSError:
                    pass
        return False

    logger.warning("WAL 自愈：已丢弃损坏的 WAL/SHM（备份至 %s），主库恢复正常", bak_dir)
    return True


async def init_db():
    # ✅ 启动自愈（2026-09-29）：主库完好但 WAL 损坏会让整库被判 malformed，
    #    此前必须人工停服+清理 WAL 才能恢复。现于首次建连前自动尝试自愈。
    _self_heal_corrupt_wal()
    conn = await get_conn()
    await conn.executescript(SCHEMA_SQL)
    await _migrate(conn)
    await conn.commit()
    logger.info("数据库初始化完成: %s", DB_PATH)


async def _migrate(conn: aiosqlite.Connection):
    """兼容性迁移：为已有表添加新列"""
    migrations = [
        ("ai_config", "plan", "TEXT DEFAULT 'pay_as_you_go'"),
        ("ai_config", "priority", "INTEGER DEFAULT 0"),
        ("ai_config", "remark", "TEXT DEFAULT ''"),
        # 请求方式（normal / stream）：仅影响后端与厂商之间的调用方式，
        # 应用侧仍等待完整结果后继续流程。
        ("ai_config", "request_mode", "TEXT DEFAULT 'normal'"),
        ("sections", "flowchart_json", "TEXT DEFAULT ''"),
        ("sections", "gantt_json", "TEXT DEFAULT ''"),
        ("sections", "architecture_json", "TEXT DEFAULT ''"),
        ("sections", "labor_json", "TEXT DEFAULT ''"),
        ("sections", "comparison_json", "TEXT DEFAULT ''"),
        ("sections", "layout_json", "TEXT DEFAULT ''"),
        ("sections", "timeline_json", "TEXT DEFAULT ''"),
        ("sections", "inlined_chart_json", "TEXT DEFAULT ''"),
        # --- global_facts 增强（可行性研究报告建议） ---
        ("global_facts", "category", "TEXT DEFAULT ''"),
        ("global_facts", "source_ref", "TEXT DEFAULT ''"),
        ("global_facts", "is_simulated", "INTEGER DEFAULT 0"),
        ("global_facts", "confidence", "REAL DEFAULT 1.0"),
        ("global_facts", "is_resolved", "INTEGER DEFAULT 1"),
        ("global_facts", "has_conflict", "INTEGER DEFAULT 0"),
        ("global_facts", "conflict_keys", "TEXT DEFAULT ''"),
        ("global_facts", "fact_key", "TEXT DEFAULT ''"),
        # --- 增量提取：事实行所属分段指纹（跳过已完成段时按它保留旧行，
        #     避免部分段重提取时把上次段的事实误删）---
        ("global_facts", "chunk_hash", "TEXT DEFAULT ''"),
        # --- 全局事实分组标题（修复重新查询后分组标题退化为单条事实名） ---
        ("global_facts", "group_title", "TEXT DEFAULT ''"),
        # --- 全局事实溯源/语义扩展字段落库（数据流审计 2026-09-23）---
        #     FactItem 提取期已算出但旧表无列→重新拉取即丢失，幂等补列（均有默认值）。
        ("global_facts", "value_unit", "TEXT DEFAULT ''"),
        ("global_facts", "fact_type", "TEXT DEFAULT ''"),
        ("global_facts", "evidence_kind", "TEXT DEFAULT ''"),
        ("global_facts", "page_ref", "INTEGER"),
        ("global_facts", "zone_type", "TEXT DEFAULT ''"),
        ("global_facts", "is_safety_critical", "INTEGER DEFAULT 0"),
        ("global_facts", "norm_group", "TEXT DEFAULT ''"),
        # --- 全局事实「九大章节分类体系」四维标注（2026-09-24）---
        #     正交于既有 22 类 category（前端下拉/分组排序/自动分类器均依赖它），
        #     在其之上叠加九大章节归属 + 事实属性 + 数据来源 + 跨章节共性标记。
        #     均为确定性规则派生（facts_classification），无 AI、无 DB 往返；
        #     新增列均有默认值，历史行由读路径惰性派生兜底（不丢事实）。
        ("global_facts", "chapter", "TEXT DEFAULT ''"),
        ("global_facts", "fact_attr", "TEXT DEFAULT ''"),
        ("global_facts", "source_kind", "TEXT DEFAULT ''"),
        ("global_facts", "is_shared", "INTEGER DEFAULT 0"),
        # 资料来源时效：重解析/删除后旧事实保守标记 stale，下游统一排除。
        ("global_facts", "is_stale", "INTEGER DEFAULT 0"),
        # --- 全局事实分步工作流（上传保存 → 解析 → 提取）---
        ("project_documents", "file_path", "TEXT DEFAULT ''"),
        # --- 文件分类/大小/解析耗时（文件导入/解析模块增强）---
        ("project_documents", "doc_category", "TEXT DEFAULT ''"),
        ("project_documents", "file_size", "INTEGER DEFAULT 0"),
        ("project_documents", "parse_time", "REAL DEFAULT 0"),
        # --- 解析器诊断告警持久化（PDF 页数截断 / Excel 行数截断 / OCR 兜底等，
        #     旧实现只在解析响应里出现一次，刷新后丢失，用户无从得知内容不完整）---
        ("project_documents", "parse_warnings", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-26（F2）：解析器级截断持久化。旧实现列表接口的 truncated
        #     仅按落库字数反推，漏报 PDF 截页 / 表格截行等解析器级截断（刷新后丢失）。
        #     现持久化截断布尔，列表直接读取，与单/批量解析响应口径一致。---
        ("project_documents", "parse_truncated", "INTEGER DEFAULT 0"),
        # --- 上传目录识别的诊断告警持久化（与 project_documents 口径一致，
        #     刷新/重新拉取后仍能回显「识别结果可能不完整」）---
        ("uploaded_outlines", "parse_warnings", "TEXT DEFAULT ''"),
        # --- 导出轮次（导出文件名规则：方案名称 + 日期 + 导出轮次）---
        ("schemes", "export_round", "INTEGER DEFAULT 0"),
        # --- AI 审计日志失败原因（2026-09-17：35% 失败记录零信息，无法区分 429/超时/认证）---
        ("ai_audit_logs", "error", "TEXT DEFAULT ''"),
        # --- AI 审计日志业务场景（2026-09-21：调用次数按场景归因，
        #     目录草稿/一级/逐章/审核/修复各占多少次可调出精确数字）---
        ("ai_audit_logs", "scene", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-24：可靠性统计粒度升级为配置身份。审计行记录
        #     config_id / base_url，使同一 provider 的不同配置可独立统计。 ---
        ("ai_audit_logs", "config_id", "TEXT DEFAULT ''"),
        ("ai_audit_logs", "base_url", "TEXT DEFAULT ''"),
        # --- 2026-09-17：人工审核状态与编译状态解耦（review.py /submit 此前直接
        #     覆盖 schemes.status，评审通过后编译态被英文 approved/rejected 覆盖）---
        ("schemes", "review_status", "TEXT DEFAULT ''"),
        # --- 2026-09-24：提取项目模块 · 危大工程分类体系落库（纯增量，不影响既有列）---
        #     分类结果由 /api/v1/bid-analysis/classify 写入；旧方案无分类时这些列恒为空，
        #     下游（目录生成/正文生成）按「有分类才用、无分类走旧逻辑」降级，向后兼容。
        ("schemes", "hazard_category", "TEXT DEFAULT ''"),
        ("schemes", "hazard_subcategory", "TEXT DEFAULT ''"),
        ("schemes", "is_hazardous", "INTEGER DEFAULT 0"),
        ("schemes", "is_oversize", "INTEGER DEFAULT 0"),
        ("schemes", "scheme_classification_json", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-26（F-CONTENT-STANDARD）：正文「生成标准」---
        #     方案级默认 precise（与既有数据真实性红线强约束等价）；
        #     章节级 '' = 沿用方案级；last_* 由生成链路记录最近一次实际生效标准。
        ("schemes", "generation_standard", "TEXT DEFAULT 'precise'"),
        ("sections", "generation_standard", "TEXT DEFAULT ''"),
        ("sections", "last_generation_standard", "TEXT DEFAULT ''"),
        ("sections", "last_generation_report", "TEXT DEFAULT ''"),
        # --- ✅ 四层存储架构（文件上传解析结果存储规范）：project_documents
        #     指纹/版本/状态列，与 schema_sql DDL 同名共存（旧库靠这里补列）---
        ("project_documents", "file_hash_md5", "TEXT DEFAULT ''"),
        ("project_documents", "file_hash_sha256", "TEXT DEFAULT ''"),
        ("project_documents", "page_count", "INTEGER DEFAULT 0"),
        ("project_documents", "parse_status", "TEXT DEFAULT 'pending'"),
        ("project_documents", "parse_version", "TEXT DEFAULT 'v1'"),
        ("project_documents", "parsed_at", "TEXT DEFAULT ''"),
        ("project_documents", "parse_duration_ms", "INTEGER DEFAULT 0"),
        ("project_documents", "parse_engine", "TEXT DEFAULT ''"),
        ("project_documents", "extract_status", "TEXT DEFAULT 'pending'"),
        ("project_documents", "extract_time", "TEXT DEFAULT ''"),
        ("project_documents", "quality_score", "REAL DEFAULT -1"),
        ("project_documents", "completeness_json", "TEXT DEFAULT ''"),
        ("project_documents", "expires_at", "TEXT DEFAULT ''"),
        ("project_documents", "status", "TEXT DEFAULT 'valid'"),
        # --- ✅ 2026-09-20：提取项目（bid_analysis）结果来源标记
        #     人工校正（POST /bid-analysis/results/{item_id}）后写 'manual'，
        #     前端据此显示「已人工校正」徽标，避免用户把 AI 抽错的关键参数
        #     （如基坑深度）误认为权威值并继续往下游目录/正文生成传递。
        ("bid_analysis_items", "source", "TEXT DEFAULT 'ai'"),
        # --- ✅ 2026-09-23：提取结果「来源位置」溯源。
        #     提取成功后用确定性反查（bid_analysis_service.build_evidence）把
        #     结果句回查到原文档的行/标题出处，存为 JSON 数组；匹配不上存空串。
        #     人工校正/清空/重跑重置时同步清列（旧证据对新内容不再成立）。
        ("bid_analysis_items", "evidence", "TEXT DEFAULT ''"),
        # --- ✅ G4/G5（2026-09-21）：审核 / 预检留痕补齐项目维度。
        #     compliance_check 早已有 project_id，review_records / preflight_runs
        #     却没有 → 项目级审计（"这个项目有几个方案过了审"）只能 JOIN schemes，
        #     且与 compliance_check / consistency_audit 的口径不一致。
        ("review_records", "project_id", "TEXT DEFAULT ''"),
        ("preflight_runs", "project_id", "TEXT DEFAULT ''"),
        # --- ✅ G3（2026-09-21）：预检结论失效标记。运行落库时记录正文/图表/
        #     字数指纹，展示层（看板 / 历史趋势 / 整改报告）据此回 stale，
        #     用户不再把"上次的旧结论"当成"现在能不能交付"。
        #     历史行无指纹 → 视为不过期（无法判定就不打扰用户）。
        ("preflight_runs", "content_fingerprint", "TEXT DEFAULT ''"),
        # --- ✅ 根治批次截头（2026-09-23）：一次 /check 调用一个 batch_id，
        #     取代 rowid 连续段锚定（created_at 仅秒级精度，跨秒交错会错切批次）。
        #     历史行空串 → 读取端回退 rowid 锚定，向后兼容。
        ("compliance_check", "batch_id", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-22（对齐易标 bidSectionContext）：选中标段的完整明细
        #     （id/title/headLine/description/evidence）。旧表只有
        #     selected_section_id/title 两列，明细缺失时无法给 AI 提供足够
        #     的「当前标段」上下文；旧行为（无选择 → 不注入）保持不变。
        ("bid_sections", "selected_section_json", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-23：多环境（G5）。环境标签列，历史行空串 = 通用，
        #     未设置「当前环境」时选取口径与引入前完全一致（旧行为不变）。
        ("ai_config", "env", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-23：配置版本/回滚（G6）。结构化变更快照（JSON），
        #     只存非敏感字段（不含明文/密文 Key）；历史行为空串 = 无快照，
        #     读取端容忍，仅回滚能力对历史记录不可用。
        ("ai_config_audit_logs", "snapshot_json", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-29：章节「事实变更失效标记」的数据源。
        #     全局事实（global_facts）发生任何写操作后，由唯一出口
        #     invalidate_export_cache(..., facts_touched=True) 把本方案的时间戳
        #     推到当前时刻；章节树读路径据此派生 facts_stale（章节 updated_at
        #     早于该时刻且有正文 → 章节正文引用的事实已变更，建议重新生成）。
        #     选「方案级单点时间戳 + 读侧派生」而不是「13 处正文写路径各清一个
        #     布尔列」：后者正是本仓反复踩的「同一判据散落多处、漏改一处」陷阱。
        #     历史行为空串 → 视为「无从判定」，所有章节 facts_stale=0（不打扰用户）。
        ("schemes", "facts_updated_at", "TEXT DEFAULT ''"),
        # --- ✅ 2026-09-30（第十一轮 · 招标响应域）：提取域标记。
        #     历史行默认 'scheme'（本软件原有 18 项），主键 {project_id}_{item_id}
        #     完全不变；bid_response 域行主键为 {project_id}__bid_response__{item_id}。
        #     空串视同 'scheme'（读侧 fail-closed 兜底），旧数据零迁移。
        ("bid_analysis_items", "domain", "TEXT DEFAULT 'scheme'"),
        # ✅ 2026-10-07：AI 结论行的正文指纹（陈旧结论不再计入总检评分，
        #   见 routers/compliance.py::_ai_row_is_stale）。历史行空串 = fail-open。
        ("compliance_check", "content_fingerprint", "TEXT DEFAULT ''"),
        ("consistency_audit", "content_fingerprint", "TEXT DEFAULT ''"),
        ("consistency_conflicts", "content_fingerprint", "TEXT DEFAULT ''"),
    ]
    for table, column, definition in migrations:
        try:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await cur.fetchall()}
            # ✅ BUG 修复（2026-09-23 · 迁移告警噪音）：表尚不存在时 cols 为空集，
            #    旧实现照样执行 ALTER → 反复刷 "no such table" WARNING（后台日志
            #    实证：uploaded_outlines.parse_warnings 在测试/半初始化环境下每次
            #    迁移都告警）。缺表说明走的是新库路径，schema DDL 已含同名列，
            #    无需补列 —— 幂等跳过即可，表存在但缺列的旧库行为完全不变。
            if not cols:
                logger.debug("迁移 %s.%s 跳过：表不存在（新表 DDL 已含该列）", table, column)
                continue
            if column not in cols:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                logger.info("迁移：为 %s 添加列 %s", table, column)
        except Exception as e:
            logger.warning("迁移 %s.%s 失败: %s", table, column, e)

    # --- ✅ 2026-10-01 修复启动失败：域索引依赖 domain 列，必须等上方补列后再建。
    #     旧实现把该索引写在 SCHEMA_SQL 的 executescript 中、早于本迁移执行；
    #     对「已存在旧库」（CREATE TABLE 被 IF NOT EXISTS 跳过、domain 尚不存在）
    #     会抛 "no such column: domain" 致 init_db 整体失败、后端无法启动。
    #     现改为补列之后创建：新库（domain 已随 CREATE TABLE 存在）与旧库均安全幂等。
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bid_analysis_project_domain "
            "ON bid_analysis_items(project_id, domain)")
    except Exception as e:
        logger.warning("bid_analysis_items 域索引创建失败（可忽略）: %s", e)

    # --- ✅ 一致性扫描增量缓存表（2026-09-22 · P0-1）：旧库没有此表，
    #     一致性扫描会退化为「每章必调 AI」（不报错，但增量优化失效）。
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS consistency_scan_cache ("
            "section_id TEXT PRIMARY KEY, scheme_id TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, context_hash TEXT NOT NULL, "
            "rows_json TEXT NOT NULL, "
            "created_at TEXT DEFAULT (datetime('now','localtime')))")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cscan_scheme"
            " ON consistency_scan_cache(scheme_id)")
    except Exception as e:
        logger.warning("consistency_scan_cache 建表失败: %s", e)

    # --- 提示词配置审计（旧库幂等建表；新库已由 SCHEMA_SQL 创建）。
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS prompt_audit_logs ("
            "id TEXT PRIMARY KEY, prompt_key TEXT NOT NULL, action TEXT DEFAULT '',"
            " before_hash TEXT DEFAULT '', after_hash TEXT DEFAULT '',"
            " variables_before TEXT DEFAULT '[]', variables_after TEXT DEFAULT '[]',"
            " client_ip TEXT DEFAULT '',"
            " created_at TEXT DEFAULT (datetime('now','localtime')))")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_audit_key_created"
            " ON prompt_audit_logs(prompt_key, created_at)")
    except Exception as e:
        logger.warning("prompt_audit_logs 建表失败: %s", e)

    # --- ✅ 性能优化（2026-09-24 · 基线优化 P0-2）：ai_audit_logs 覆盖索引 ---
    # 背景（基线实测，13334 行）：`GET /ai/config/audit-logs` 每次附带两条
    # 「筛选下拉」查询
    #   SELECT DISTINCT provider_name FROM ai_audit_logs WHERE provider_name!=''
    #   SELECT DISTINCT action        FROM ai_audit_logs WHERE action!=''
    # 两者都无索引可用，必须 SCAN 全表 13334 行 + 内存去重，实测 4.07ms / 3.56ms。
    # 前端 AIConfigPage 每 30s 刷一次模型列表，且每次打开审计面板都重跑。
    # 建 (provider_name) / (action) 单列覆盖索引后 SQLite 走
    # 「SCAN ... USING COVERING INDEX + DISTINCT 走 B-树」，无需回表读大字段
    # （error / *_json 等长文本列全部不在索引里），把两次查询压到亚毫秒级。
    # 索引为纯新增、无 DDL 变更表结构，不影响任何既有查询与写入路径
    # （ai_audit_logs 是只追加的审计表，写入量 50 条攒批 flush，索引维护开销可忽略）。
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_audit_provider_name"
            " ON ai_audit_logs(provider_name)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_audit_action"
            " ON ai_audit_logs(action)")
        # action + success 组合：`audit-logs?success=0` 与「失败原因 TOP」
        # 聚合的公共前缀（WHERE action='chat' AND success=0 AND created_at >= ?）
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_audit_action_success"
            " ON ai_audit_logs(action, success, created_at)")
        # 场景聚合：by_scene 统计（WHERE action='chat' AND scene!='' AND created_at>=?
        # GROUP BY scene）。带上 created_at 使索引可承担时间范围裁剪。
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_audit_action_scene_created"
            " ON ai_audit_logs(action, scene, created_at)")
        # ✅ 2026-09-25（缺口补齐）：audit-logs 新增 scene 精确筛选后，
        #    上面那条 (action, scene, created_at) 索引**用不上**（前导列 action
        #    不在条件里）→ 按场景下钻会全表 SCAN。补一条以 scene 为前导列的
        #    覆盖索引：同时服务 `WHERE scene=? ORDER BY created_at` 与筛选
        #    下拉的 `SELECT DISTINCT scene`（覆盖索引免回表）。
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_audit_scene_created"
            " ON ai_audit_logs(scene, created_at)")
        await conn.commit()
    except Exception as e:
        logger.warning("ai_audit_logs 覆盖索引创建失败: %s", e)

    # --- G2 提示词版本回滚（2026-09-24）：给 prompt_audit_logs 增加快照列 ---
    # 旧表只存 before/after 的 SHA-256 哈希，哈希无法还原正文 → 无法回滚。
    # 新增 snapshot_json 存 {"before": 完整正文, "after": 完整正文}，
    # 由 settings.prompt_audit_snapshot_enabled 控制是否写入（默认开）。
    # 历史记录该列为空 → 前端 rollbackable=False，不显示回滚按钮（与 ai_config 同口径）。
    try:
        cur = await conn.execute("PRAGMA table_info(prompt_audit_logs)")
        pal_cols = {r[1] for r in await cur.fetchall()}
        if "snapshot_json" not in pal_cols:
            await conn.execute(
                "ALTER TABLE prompt_audit_logs "
                "ADD COLUMN snapshot_json TEXT DEFAULT ''")
            logger.info("迁移：prompt_audit_logs 增加 snapshot_json 列（支持版本回滚）")
    except Exception as e:
        logger.warning("prompt_audit_logs snapshot_json 迁移失败: %s", e)

    # --- 增量提取进度作用域细化（修复 BUG-1：多方案共享项目时，非首方案「提取事实」
    #     命中其它方案的历史完成段指纹 → all_skipped 早退 → 本方案写入 0 条事实）---
    # 旧表主键为 (project_id, chunk_hash)，无法区分方案；重建为 (project_id, scheme_id,
    # chunk_hash)，并把旧 project 级指纹归并为 scheme_id=''（仅影响全项目级事实提取，
    # 让各方案各自重新统计完成段，正确落库）。
    try:
        cur = await conn.execute("PRAGMA table_info(facts_extracted_chunks)")
        fec_cols = {r[1] for r in await cur.fetchall()}
        if "scheme_id" not in fec_cols:
            await conn.execute(
                "ALTER TABLE facts_extracted_chunks RENAME TO _fec_old")
            await conn.execute(
                "CREATE TABLE facts_extracted_chunks ("
                "project_id TEXT NOT NULL, scheme_id TEXT NOT NULL DEFAULT '', "
                "chunk_hash TEXT NOT NULL, "
                "extracted_at TEXT DEFAULT (datetime('now','localtime')), "
                "PRIMARY KEY (project_id, scheme_id, chunk_hash))")
            await conn.execute(
                "INSERT INTO facts_extracted_chunks "
                "(project_id, scheme_id, chunk_hash, extracted_at) "
                "SELECT project_id, '', chunk_hash, extracted_at FROM _fec_old")
            await conn.execute("DROP TABLE _fec_old")
            logger.info("迁移：facts_extracted_chunks 增加 scheme_id 作用域")
    except Exception as e:
        logger.warning("facts_extracted_chunks 迁移失败: %s", e)

    # --- 一次性数据迁移：修复被审核状态污染的 schemes.status（2026-09-17） ---
    # 旧版 review.py /submit 直接把英文审核状态写进 schemes.status，覆盖了编译状态。
    # 现把历史英文审核值搬到 review_status，并把 status 还原为「目录已确认」
    # （能进入评审说明目录已确认；原始编译态无法精确还原，取最合理回退，见设计文档）。
    # 条件带 review_status='' 保证幂等（重复执行不二次搬运）。
    try:
        cur = await conn.execute("PRAGMA table_info(schemes)")
        scheme_cols = {r[1] for r in await cur.fetchall()}
        if {"status", "review_status"} <= scheme_cols:
            cur = await conn.execute(
                "UPDATE schemes SET review_status=status"
                " WHERE status IN ('pending','reviewing','approved','rejected')"
                " AND COALESCE(review_status,'')=''")
            moved = safe_rowcount(cur, what="schemes 审核状态迁移")
            if moved:
                await conn.execute(
                    "UPDATE schemes SET status='目录已确认'"
                    " WHERE status IN ('pending','reviewing','approved','rejected')")
                logger.info("数据迁移：%d 个方案的审核状态从 status 搬运到 review_status", moved)
    except Exception as e:
        logger.warning("schemes 审核状态数据迁移失败: %s", e)

    # --- 迁移索引（CREATE INDEX IF NOT EXISTS 自带幂等性，无需先探存） ---
    index_migrations = [
        ("idx_global_facts_group", "CREATE INDEX IF NOT EXISTS idx_global_facts_group ON global_facts(group_id)"),
        ("idx_global_facts_simulated", "CREATE INDEX IF NOT EXISTS idx_global_facts_simulated ON global_facts(is_simulated)"),
        # 深度审计新增：高频复合查询索引
        ("idx_chart_pred_scheme_status", "CREATE INDEX IF NOT EXISTS idx_chart_pred_scheme_status ON chart_predictions(scheme_id, status)"),
        ("idx_global_facts_scheme_conflict", "CREATE INDEX IF NOT EXISTS idx_global_facts_scheme_conflict ON global_facts(scheme_id, has_conflict)"),
        # 九大章节维度检索（正文生成按章节精选事实、章节视图聚合统计）
        ("idx_global_facts_chapter", "CREATE INDEX IF NOT EXISTS idx_global_facts_chapter ON global_facts(chapter)"),
        ("idx_task_registry_scheme_status", "CREATE INDEX IF NOT EXISTS idx_task_registry_scheme_status ON task_registry(scheme_id, status)"),
        # AI 配置：主配置选取 + 降级链排序热点查询
        ("idx_ai_config_active", "CREATE INDEX IF NOT EXISTS idx_ai_config_active ON ai_config(is_active, updated_at)"),
        ("idx_ai_config_priority", "CREATE INDEX IF NOT EXISTS idx_ai_config_priority ON ai_config(priority, updated_at)"),
        # ✅ 性能优化（2026-09-24 · 遗留项 #3）：零索引但存在真实查询点的表
        #   - outline_library_versions：版本列表/回滚按 library_id（归档可累积多版）
        #   - knowledge_base：知识条目按 project_id(+scheme_id)（正文生成逐章注入）
        #   - consistency_audit：一致性审计按 scheme_id + created_at 倒序（每条
        #     /consistency-audit 运行都落一行，重跑多代后历史行累积）
        #   全部 CREATE INDEX IF NOT EXISTS 幂等，纯新增、不改表结构、不影响写入路径。
        ("idx_olv_library", "CREATE INDEX IF NOT EXISTS idx_olv_library ON outline_library_versions(library_id)"),
        ("idx_kb_project_scheme", "CREATE INDEX IF NOT EXISTS idx_kb_project_scheme ON knowledge_base(project_id, scheme_id)"),
        ("idx_consistency_audit_scheme", "CREATE INDEX IF NOT EXISTS idx_consistency_audit_scheme ON consistency_audit(scheme_id, created_at)"),
    ]
    for idx_name, create_sql in index_migrations:
        try:
            # ✅ 修复：原实现 PRAGMA index_list(索引名) 用法错误（应传表名）恒返回空；
            # 直接执行 IF NOT EXISTS 语句即可，重复执行无副作用
            await conn.execute(create_sql)
        except Exception as e:
            logger.warning("创建索引 %s 失败: %s", idx_name, e)

    # --- export_cache 三元组唯一索引迁移 ---
    # export_docx 的查询键是 (scheme_id, config_hash, content_fingerprint)。
    # 旧版二元唯一索引会导致同一内容的不同排版配置无法分别缓存。
    try:
        await conn.execute("DROP INDEX IF EXISTS uq_export_cache_scheme_fp")
        await conn.execute(
            "DELETE FROM export_cache WHERE rowid NOT IN ("
            "SELECT MAX(rowid) FROM export_cache GROUP BY scheme_id, config_hash, content_fingerprint)")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_export_cache_scheme_cfg_fp "
            "ON export_cache(scheme_id, config_hash, content_fingerprint)")
    except Exception as e:
        logger.warning("export_cache 三元组唯一索引迁移失败（可忽略）: %s", e)

    # --- 数据迁移：修复旧版图表预测僵尸行（status='generated' 但代码为空） ---
    # 旧版手动预测端点落库时 status 误写为 "generated" 且 mermaid_code 为空：
    # Phase 3 只处理 pending，这些图永远不会生成代码（UI 显示已生成、导出无图）。
    # 重置为 pending 让 Phase 3 补生成；layout/timeline 的 data_json 是 JSON 数据
    # 本身（顶层无 mermaid_code 字段），必须排除避免误重置。
    # ✅ BUG 修复（2026-09-18）：规范信封对**结构化数据图表**同样写
    #    {"mermaid_code": "", "data": {...}}（chart_payload.build_chart_envelope 的
    #    data 分支），本迁移每次启动执行且只判 mermaid_code 为空 → labor/gantt/
    #    architecture/comparison 等数据型图表每次重启都被打回 pending，被导出预检
    #    与 DLV-04 误判为「未生成」。现追加「无 data 字段」条件，只命中真正的
    #    mermaid 僵尸行。
    try:
        cur = await conn.execute(
            "UPDATE chart_predictions SET status='pending'"
            " WHERE status='generated'"
            " AND chart_type NOT IN ('layout','timeline')"
            " AND COALESCE(json_extract(data_json, '$.mermaid_code'), '') = ''"
            " AND json_extract(data_json, '$.data') IS NULL")
        _reset = safe_rowcount(cur, what="空代码图表预测重置迁移")
        if _reset:
            logger.info("迁移：重置 %d 条空代码图表预测为 pending（等待重新生成）", _reset)
    except Exception as e:
        # json_extract 需要 SQLite JSON1 扩展，极老版本可能不支持——失败不影响启动
        logger.warning("图表预测僵尸行迁移失败（可忽略）: %s", e)

    # --- 数据迁移：重算被污染的字数口径（word_count/word_status） ---
    # 背景（2026-09-16 · 依据运行库普查）：一致性修复链路曾用 `len(content)` 写
    # word_count 且不更新 word_status，使带```图表代码块的章节字数被图表代码抬高
    # （实测 3 章：如「进度计划横道图与网络图」实际 1718 字却被记为 2608 字 →
    # 误判 word_status='over'，会触发无意义的压缩建议/自动压缩）。
    # word_count/word_status 是**派生列**，重算不会丢失任何原始信息，故可安全自愈：
    # 只处理「文本含代码围栏」的行（口径差异只可能出现在这些行），幂等可重复执行。
    try:
        from app.services.content_utils import DEFAULT_WORD_BUDGET, text_word_count, word_status_for
        cur = await conn.execute(
            "SELECT id, content, word_count, word_status, word_budget FROM sections"
            " WHERE content LIKE '%```%'")
        _fixed = 0
        for row in await cur.fetchall():
            content = row[1] or ""
            wc = text_word_count(content)
            ws = word_status_for(wc, row[4] or DEFAULT_WORD_BUDGET)
            if int(row[2] or 0) != wc or (row[3] or "") != ws:
                await conn.execute(
                    "UPDATE sections SET word_count=?, word_status=? WHERE id=?",
                    (wc, ws, row[0]))
                _fixed += 1
        if _fixed:
            await conn.commit()
            logger.info("迁移：重算 %d 章被污染的字数口径（剔除图表代码块）", _fixed)
    except Exception as e:
        logger.warning("字数口径重算迁移失败（可忽略）: %s", e)


async def _setup_connection(conn: aiosqlite.Connection) -> aiosqlite.Connection:
    """统一连接初始化（PRAGMA、row_factory 等）"""
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # ✅ 修复（2026-09-23）：busy_timeout 从 5s 提升到 15s。
    #    原值 5s 在前端轮询风暴（实测 50 req/s）+ 目录生成等长事务场景下
    #    频繁触发 database is locked，导致生成流程直接失败。15s 覆盖绝大多数
    #    锁竞争窗口，同时不至于让真正的死锁等太久。
    await conn.execute("PRAGMA busy_timeout=15000")
    await conn.execute("PRAGMA synchronous=NORMAL")
    # ✅ 性能调优（2026-09-24 · 基线优化 P1-2）：可选追加读侧 PRAGMA。
    #    默认关闭（db_perf_pragmas_enabled=False）= 与旧行为逐字节一致。
    #    实测归因（2026-09-24 复核，13334 行快照 × 50 轮）：唯一纯收益是
    #    mmap_size —— 按场景聚合 ↓62%、失败原因 TOP ↓49%、DISTINCT 下拉也 ↓11%；
    #    temp_store=MEMORY 在该平台反而变慢（+13%~51%），故其开关默认关闭。
    #    开启后仅追加 mmap_size（读侧映射，不改变写语义与并发语义）。
    if getattr(settings, "db_perf_pragmas_enabled", False):
        if getattr(settings, "db_temp_store_memory", False):
            await conn.execute("PRAGMA temp_store=MEMORY")
        _mmap = int(getattr(settings, "db_mmap_size", 268435456) or 0)
        if _mmap > 0:
            await conn.execute(f"PRAGMA mmap_size={_mmap}")
    await conn.commit()
    return conn


async def close_db():
    global _conn, _conn_path, _db_pool_total, _read_pool_total
    if _conn is not None:
        # ✅ 幂等：连接可能已被外部关闭（再 close 会抛），关闭流程不应因
        #    重复关闭而中断 —— 否则启动/关闭竞态下残留的池连接永远不被清理。
        try:
            await _conn.close()
        except Exception:
            pass
        _conn = None
        _conn_path = None
    # P1-3：关闭连接池中全部空闲连接
    async with _db_pool_cv:
        while _db_pool:
            c = _db_pool.pop()
            _db_pool_total -= 1
            try:
                await c.close()
            except Exception:
                pass
    # Phase 3：关闭只读连接池
    async with _read_pool_cv:
        while _read_pool:
            c = _read_pool.pop()
            _read_pool_total -= 1
            try:
                await c.close()
            except Exception:
                pass


# P1-3 性能优化：FastAPI 依赖连接池（有界、请求结束归还）
# SQLite 单机单写：并发写由 WAL + busy_timeout=5000 在数据库层串行化
# （与旧"每请求独立连接"的并发语义一致），池化仅消除连接建立与
# PRAGMA 初始化开销；连接异常时自动重建。
_db_pool: list[aiosqlite.Connection] = []
_db_pool_total = 0  # 已创建未关闭的连接数（池内 + 在途）
_db_pool_path: Path | None = None  # 池连接绑定的 DB_PATH（切换后整池失效）
_db_pool_cv = asyncio.Condition()
_POOL_MAX_SIZE = 8


async def _acquire_pool_conn() -> aiosqlite.Connection:
    """从连接池取连接；池空且未达上限则新建，否则等待归还"""
    global _db_pool_total, _db_pool_path
    async with _db_pool_cv:
        # BUG 修复（2026-09-13）：DB_PATH 切换后旧路径的池连接全部失效，
        # 直接排空，避免复用指向旧库的连接。
        if _db_pool_path is not None and _db_pool_path != DB_PATH:
            while _db_pool:
                _c = _db_pool.pop()
                _db_pool_total -= 1
                try:
                    await _c.close()
                except Exception:
                    pass
            _db_pool_path = None
        while True:
            if _db_pool:
                conn = _db_pool.pop()
                # poisoned 标记：写路径里曾抛过 disk I/O error，直接剔除
                # ✅ 2026-09-26：已关闭的连接同样剔除（见 _probe_conn 说明：
                #    死连接探针会被"非 IO 异常"分支误判为可用而放行）
                if _is_poisoned(conn) or _is_dead_conn(conn):
                    _db_pool_total -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    logger.warning("剔除失效写池连接（poisoned 或已关闭）")
                    continue
                # PRAGMA wal_checkpoint 探针：真实 IO 往返，覆盖"写入才抖"场景
                if not await _probe_conn(conn):
                    _db_pool_total -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    logger.warning("剔除写池连接：探活 disk I/O 失败")
                    continue
                return conn
            if _db_pool_total < _POOL_MAX_SIZE:
                _db_pool_total += 1
                break
            await _db_pool_cv.wait()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 新建连接也做有限次重试，避免 J 盘刚好离线时一次失败就抛错
        last_err = None
        for attempt in range(_PROBE_MAX_ATTEMPTS):
            try:
                conn = await aiosqlite.connect(DB_PATH)
                _db_pool_path = DB_PATH
                await _setup_connection(conn)
                if not await _probe_conn(conn):
                    _db_pool_total -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    last_err = Exception("新建写池连接探活失败")
                    continue
                return conn
            except Exception as e:
                _db_pool_total -= 1
                last_err = e
                if not _is_disk_io_error(e):
                    raise
                logger.warning("新建写池连接 disk I/O 失败（第 %d 次），重试", attempt + 1)
        raise RuntimeError(f"数据库写池连接建立失败（连续 {_PROBE_MAX_ATTEMPTS} 次 disk I/O error）") from last_err


async def _release_pool_conn(conn: aiosqlite.Connection) -> None:
    """归还连接；poisoned / 已关闭 / 探活失败的连接直接关闭"""
    global _db_pool_total
    # 写路径标记了 disk I/O error、或连接已被关闭：不再归还，直接剔除
    # ✅ 2026-09-26：死连接**绝不能回到池里** —— 那正是「池里躺着已关闭连接、
    #    业务深处抛 no active connection」的传播路径。
    if _is_poisoned(conn) or _is_dead_conn(conn):
        async with _db_pool_cv:
            _db_pool_total -= 1
            _db_pool_cv.notify()  # 容量已释放，唤醒等待者
        try:
            await conn.close()
        except Exception:
            pass
        return
    if not await _probe_conn(conn):
        async with _db_pool_cv:
            _db_pool_total -= 1
            _db_pool_cv.notify()  # 容量已释放，唤醒等待者
        try:
            await conn.close()
        except Exception:
            pass
        logger.warning("剔除写池连接：归还前探活 disk I/O 失败")
        return
    async with _db_pool_cv:
        _db_pool.append(conn)
        _db_pool_cv.notify()


async def get_db():
    """FastAPI 依赖：连接池复用（P1-3），替代每请求新建/关闭 aiosqlite 连接。

    写入仍由 SQLite WAL + busy_timeout=5000 在数据库层串行化（单机单写），
    与旧实现并发语义一致；池化仅消除连接建立/PRAGMA 初始化开销。
    请求结束归还连接而非关闭。
    """
    conn = await _acquire_pool_conn()
    try:
        yield conn
    finally:
        # 归还前回滚未提交事务：端点中途抛异常时可能遗留隐式写事务，
        # 不回滚会让下一个借用者 commit 时把脏写一并提交（或长期持有写锁）。
        # 对无事务连接 rollback 是无害 no-op。
        try:
            await conn.rollback()
        except Exception:
            pass
        await _release_pool_conn(conn)


@asynccontextmanager
async def write_tx_conn():
    """独立写池连接上的多语句事务（服务层用）。

    背景：服务层的全局共享连接 get_conn() 被所有协程并发共用，事务边界交错；
    多语句序列在其中 rollback 会把并发协程已执行未提交的合法语句一并回滚。
    需要多语句原子性的服务层操作应使用本助手：独占一条池连接，
    异常时只回滚自己的事务，归还前统一清理残留事务。
    """
    conn = await _acquire_pool_conn()
    try:
        yield conn
    except BaseException:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # 归还前清残留事务（正常路径已 commit 则为无害 no-op）
        try:
            await conn.rollback()
        except Exception:
            pass
        await _release_pool_conn(conn)


# ---------- Phase 3 读写连接分离 ----------
# ✅ 目标（深度审计 §7.1 Phase 3）：写路径独占专用连接，读路径不受写操作排队影响。
#    - 写连接：全局单连接 _conn（get_conn，服务层/SSE 生成链路，写密集）+ get_db 写池（REST 写路由）；
#    - 读连接：独立读池（read_db），连接上设 PRAGMA query_only=ON —— 仅可执行 SELECT，
#      即使端点误写也会立刻报错（fail-fast），且这些连接永远不会持有写锁/未提交事务。
#    WAL 模式下读写本就不互斥，此分离进一步消除「读请求复用了带未提交写事务的连接」
#    这类隐性排队，并把轮询类热点读（任务状态 3s 轮询等）的 IO 与写路径完全隔离。

_read_pool: list[aiosqlite.Connection] = []
_read_pool_total = 0
_read_pool_path: Path | None = None
_read_pool_cv = asyncio.Condition()
_READ_POOL_MAX_SIZE = 8


async def _read_probe(conn: aiosqlite.Connection) -> bool:
    """读连接探活：query_only=ON 会拒绝 wal_checkpoint 的写路径，
    这里用 PRAGMA wal_checkpoint(NOOP) —— 不产生任何写入，但仍走 sqlite3
    step API，可命中底层 IO 层的错误上报；非 IO 类异常不视为坏连接。"""
    try:
        await conn.execute("PRAGMA wal_checkpoint(NOOP)")
        return True
    except Exception as e:
        # ✅ 2026-09-26：与 _probe_conn 同口径 —— 已关闭的连接判为坏连接
        if _is_dead_conn(conn) or _is_closed_conn_error(e):
            return False
        if _is_disk_io_error(e):
            return False
        return True


async def _acquire_read_conn() -> aiosqlite.Connection:
    """从只读池取连接；池空且未达上限则新建（query_only=ON），否则等待归还"""
    global _read_pool_total, _read_pool_path
    async with _read_pool_cv:
        # DB_PATH 切换后旧路径的池连接全部失效（与写池同一约定）
        if _read_pool_path is not None and _read_pool_path != DB_PATH:
            while _read_pool:
                _c = _read_pool.pop()
                _read_pool_total -= 1
                try:
                    await _c.close()
                except Exception:
                    pass
            _read_pool_path = None
        while True:
            if _read_pool:
                conn = _read_pool.pop()
                # poisoned 标记：读路径曾抛过 disk I/O error，直接剔除
                # ✅ 2026-09-26：已关闭的连接同样剔除（与写池同口径）
                if _is_poisoned(conn) or _is_dead_conn(conn):
                    _read_pool_total -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    logger.warning("剔除失效读池连接（poisoned 或已关闭）")
                    continue
                # 用 PRAGMA wal_checkpoint(NOOP) 走 IO 层，覆盖"读取时抖"场景
                if not await _read_probe(conn):
                    _read_pool_total -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    logger.warning("剔除读池连接：探活 disk I/O 失败")
                    continue
                return conn
            if _read_pool_total < _READ_POOL_MAX_SIZE:
                _read_pool_total += 1
                break
            await _read_pool_cv.wait()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 新建连接也做有限次重试，避免 J 盘刚好离线时一次失败就抛错
    last_err = None
    for attempt in range(_PROBE_MAX_ATTEMPTS):
        try:
            conn = await aiosqlite.connect(DB_PATH)
            _read_pool_path = DB_PATH
            await _setup_connection(conn)
            # 只读保护：query_only=ON 后连接拒绝任何写语句（fail-fast，暴露误用）
            await conn.execute("PRAGMA query_only=ON")
            if not await _read_probe(conn):
                _read_pool_total -= 1
                try:
                    await conn.close()
                except Exception:
                    pass
                last_err = Exception("新建读池连接探活失败")
                continue
            return conn
        except Exception as e:
            _read_pool_total -= 1
            last_err = e
            if not _is_disk_io_error(e):
                raise
            logger.warning("新建读池连接 disk I/O 失败（第 %d 次），重试", attempt + 1)
    raise RuntimeError(f"数据库读池连接建立失败（连续 {_PROBE_MAX_ATTEMPTS} 次 disk I/O error）") from last_err


async def _release_read_conn(conn: aiosqlite.Connection) -> None:
    """归还只读连接；poisoned / 已关闭 / 探活失败的连接直接关闭"""
    global _read_pool_total
    # 读路径标记了 disk I/O error、或连接已被关闭：不再归还，直接剔除
    if _is_poisoned(conn) or _is_dead_conn(conn):
        async with _read_pool_cv:
            _read_pool_total -= 1
            _read_pool_cv.notify()  # 容量已释放，唤醒等待者
        try:
            await conn.close()
        except Exception:
            pass
        return
    if not await _read_probe(conn):
        async with _read_pool_cv:
            _read_pool_total -= 1
            _read_pool_cv.notify()  # 容量已释放，唤醒等待者
        try:
            await conn.close()
        except Exception:
            pass
        logger.warning("剔除读池连接：归还前探活 disk I/O 失败")
        return
    async with _read_pool_cv:
        _read_pool.append(conn)
        _read_pool_cv.notify()


async def get_read_conn() -> aiosqlite.Connection:
    """服务层只读连接（借出后必须调用 release_read_conn 归还）。

    供纯读的服务层查询使用（如任务状态查询）；写操作一律走 get_conn()。
    """
    return await _acquire_read_conn()


async def release_read_conn(conn: aiosqlite.Connection) -> None:
    await _release_read_conn(conn)


async def read_db():
    """FastAPI 只读依赖：热点 GET 端点走独立只读连接池。

    仅可用于确认「纯读」的端点（连接已设 query_only=ON，任何写语句会直接报错）。
    """
    conn = await _acquire_read_conn()
    try:
        yield conn
    finally:
        await _release_read_conn(conn)