"""AI 配置模块 · 技术债收口护栏（2026-10-06 · 文本模型配置专项）。

本轮定位到并落地 10 项，护栏逐项锁定（含 A/B 反向验证）：

B1 【P0 真缺陷】``/ai/config/import`` 绕过「计费方式 ↔ Base URL」联动校验
   → 导入的 ``plan=coding_plan`` 且无 base_url 的配置会落库成功，而运行时
   ``_build_provider`` 对「已知预设 + 空地址」一律回退到**按量计费**地址，
   于是「包月配置实际按量调用」（计费方式静默错配）。现保存与导入共用
   ``resolve_config_base_url`` 单一出口。
B2 【P1 真缺陷】``_gate_candidates`` 归因漏掉 ``skipped_key_broken``
   → 「一条密钥解不开 + 一条被禁用」时错误文案归因成「都被禁用了」，
   用户恢复开关后问题依旧。
B3 【P1 真缺陷】``resolve_scene_config`` 的 ``except Exception: return None``
   零日志 → 场景路由静默停摆无从排查。
B4 【P1 真缺陷】``_log_audit`` 裸 ``except: pass`` 把「可靠性统计更新」与
   「审计缓冲写入」两种失败吞成零日志，且前者失真会直接影响候选排序。
B5 【P1 真缺陷】审计降级写入靠元组**按位切片**（r[:11] / r[:10]）与列数对齐，
   元组中段新增字段会静默写进错位的值。现加行宽守卫。
B6 【P1 真缺陷】同一 setting ``ai_provider_dead_success_rate`` 被读成两个
   模块常量（``_PROVIDER_MIN_SUCCESS_RATE`` / ``_PROVIDER_DEAD_SUCCESS_RATE``），
   两处各自演进会让「降级链过滤」与「死配置剔除」用不同门限。
   同时新增 ``_setting_num`` 单一出口：不再用 ``getattr(...) or default``
   （该写法把用户**显式配置的 0** 当成「未配置」静默回落默认值）。
B7 【P2】多环境下 ``/ai/health`` 恒报「尚未启用任何文本模型配置」——
   真因常是「当前环境没有主配置」，提示把用户指向错误的修法。
B9 【P2】``connectivity.test_config`` 自带一份 request_mode 白名单字面量。
B10 【P2 防回归护栏】``upsert_runtime_setting`` 是**刻意不失效缓存**的写助手，
   静态锁定「每个生产调用点都必须自行失效」，防止第三个调用点漏接线。
B11 【P2 补测试 + 漂移护栏】``sanitize_config_snapshot`` / ``diff_snapshots``
   此前零单测，且 ``SNAPSHOT_FIELDS`` 与 ``ai_config`` 列漂移无人拦截。
B12 【P2 补测试】``GET /ai/models`` 此前零测试（AGENTS §5.12 专门警告过
   它与 ``GET /ai/config`` 的 presets 形状不同）。
"""
import contextlib
import inspect
import json
import logging
import re

import pytest

import app.routers.ai_config.config as config_module
import app.routers.ai_config.connectivity as conn_module
import app.services.ai.provider_factory as pf
from app.models import ConfigImportIn, SceneRouteUpdate
from app.routers import ai_config as ai_router
from app.services import audit_service
from app.services.ai.provider_factory import (
    CUSTOM_PROVIDER_NAME,
    PROVIDER_PRESETS,
    VALID_REQUEST_MODES,
    normalize_plan,
    resolve_config_base_url,
)
from app.services.crypto import encrypt_api_key


@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    """把 ``write_tx_conn`` 指向测试库，使 save_ai_config 真正落库。"""
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _rows(db):
    cur = await db.execute(
        "SELECT id, provider_name, plan, base_url, model, is_active"
        " FROM ai_config ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


# ===========================================================================
# B1 · 导入绕过「计费方式 ↔ Base URL」联动校验
# ===========================================================================
class TestPlanBaseUrlSingleExit:
    def test_coding_plan_without_url_rejected(self):
        """包月套餐无地址必须拒绝（否则运行时落到按量计费地址）。"""
        with pytest.raises(ValueError, match="包月套餐"):
            resolve_config_base_url("deepseek", "coding_plan", "")

    def test_custom_without_url_rejected(self):
        with pytest.raises(ValueError, match="自定义供应商"):
            resolve_config_base_url(CUSTOM_PROVIDER_NAME, "pay_as_you_go", "")

    def test_pay_as_you_go_autofills_preset_url(self):
        """按量计费 + 已知预设 + 空地址 → 回填预设地址（既有行为）。"""
        got = resolve_config_base_url("deepseek", "pay_as_you_go", "")
        assert got == PROVIDER_PRESETS["deepseek"]["base_url"]

    def test_pay_as_you_go_unknown_preset_rejected(self):
        with pytest.raises(ValueError, match="Base URL 不能为空"):
            resolve_config_base_url("some_unknown_vendor", "pay_as_you_go", "")

    def test_coding_plan_with_url_kept_verbatim(self):
        got = resolve_config_base_url(
            "deepseek", "coding_plan", "https://api.deepseek.com/coding/v1")
        assert got == "https://api.deepseek.com/coding/v1"

    def test_normalized_plan_used_before_linkage_check(self):
        """脏 plan 值先归一为按量计费，再走自动回填（不会被当成包月而误拒）。"""
        assert normalize_plan("nonsense") == "pay_as_you_go"
        got = resolve_config_base_url("deepseek", normalize_plan("nonsense"), "")
        assert got == PROVIDER_PRESETS["deepseek"]["base_url"]

    def test_save_and_import_share_the_same_exit(self):
        """保存路径与导入路径必须调同一出口（静态锁，防再次分叉）。"""
        save_src = inspect.getsource(pf.save_ai_config)
        assert "resolve_config_base_url(" in save_src
        assert 'provider_name == "custom"' not in save_src, \
            "自定义供应商判定不得再内嵌一份，必须走 resolve_config_base_url"

    async def test_import_rejects_coding_plan_without_url(self, db_conn):
        """A/B 承重例：本条导入此前会落库成功（静默按量计费调用）。"""
        res = await ai_router.import_config(
            db=db_conn,
            body=ConfigImportIn(items=[{
                "provider_name": "deepseek", "model": "deepseek-chat",
                "plan": "coding_plan", "base_url": "",
            }]))
        assert res["imported"] == 0
        assert res["skipped"] == 1
        assert res["invalid_plan_items"] == 1
        assert res["skip_reasons"] and "包月套餐" in res["skip_reasons"][0]
        assert await _rows(db_conn) == [], "违规条目绝不能落库"

    async def test_import_accepts_coding_plan_with_url(self, db_conn):
        res = await ai_router.import_config(
            db=db_conn,
            body=ConfigImportIn(items=[{
                "provider_name": "deepseek", "model": "deepseek-chat",
                "plan": "coding_plan", "base_url": "https://api.deepseek.com/coding/v1",
            }]))
        assert res["imported"] == 1 and res["invalid_plan_items"] == 0
        rows = await _rows(db_conn)
        assert rows[0]["plan"] == "coding_plan"
        assert rows[0]["base_url"] == "https://api.deepseek.com/coding/v1"

    async def test_import_pay_as_you_go_still_autofills(self, db_conn):
        """既有行为不得回退：按量计费导入仍自动回填预设地址。"""
        res = await ai_router.import_config(
            db=db_conn,
            body=ConfigImportIn(items=[{
                "provider_name": "deepseek", "model": "deepseek-chat", "base_url": "",
            }]))
        assert res["imported"] == 1
        rows = await _rows(db_conn)
        assert rows[0]["base_url"] == PROVIDER_PRESETS["deepseek"]["base_url"]

    def test_runtime_still_falls_back_to_paygo_endpoint(self):
        """记录运行时既有行为（导入防线失效时的后果），锁住修复的必要性。

        ``_build_provider`` 对「已知预设 + 空 base_url」一律回退到预设地址；
        这正是 B1 必须在**写入侧**拦截的原因 —— 只在运行时修会掩盖问题。
        """
        p = pf._build_provider("deepseek", "sk-x", "", "deepseek-chat")
        assert p.base_url.rstrip("/") == PROVIDER_PRESETS["deepseek"]["base_url"]


# ===========================================================================
# B2 · 门禁归因漏掉「密钥解不开」
# ===========================================================================
class TestGateAttribution:
    @staticmethod
    def _cand(pname, key, key_broken=False):
        return {"config_id": pname, "provider_name": pname, "api_key": key,
                "base_url": "https://api.test.com/v1", "model": "m",
                "key_broken": key_broken}

    async def test_broken_key_alone_reports_key_problem(self, db_conn, monkeypatch):
        monkeypatch.setattr(pf, "resolve_disabled_providers",
                            _async_return(set()))
        with pytest.raises(RuntimeError, match="无法解密"):
            await pf._gate_candidates(
                [self._cand("a", "", key_broken=True)], "content_draft")

    async def test_disabled_plus_broken_key_reports_key_problem(
            self, db_conn, monkeypatch
    ):
        """A/B 承重例：修复前此处报「都被禁用了」，用户恢复开关后问题依旧。"""
        monkeypatch.setattr(pf, "resolve_disabled_providers",
                            _async_return({"disabledvendor"}))
        with pytest.raises(RuntimeError) as ei:
            await pf._gate_candidates(
                [self._cand("disabledvendor", "sk-a"),
                 self._cand("brokenvendor", "", key_broken=True)],
                "content_draft")
        msg = str(ei.value)
        assert "无法解密" in msg
        assert "所有候选 Provider 均被运行时开关禁用" not in msg, \
            "并存 key_broken 时不得把原因归给运行时开关"
        assert "另有 1 条候选被运行时开关禁用" in msg, "并存原因必须如实报出"

    async def test_all_disabled_still_reports_disabled(self, db_conn, monkeypatch):
        """纯禁用场景的文案不得被 B2 改坏。"""
        monkeypatch.setattr(pf, "resolve_disabled_providers",
                            _async_return({"dv1", "dv2"}))
        with pytest.raises(RuntimeError, match="运行时开关禁用"):
            await pf._gate_candidates(
                [self._cand("dv1", "sk-a"), self._cand("dv2", "sk-b")],
                "content_draft")


def _async_return(value):
    async def _f():
        return value
    return _f


# ===========================================================================
# B3 · 场景路由读取异常零日志
# ===========================================================================
class TestSceneRouteReadFailureIsObservable:
    async def test_failure_logs_warning_and_falls_back(
            self, db_conn, monkeypatch, caplog
    ):
        async def _boom():
            raise RuntimeError("ai_scene_routes 表不可读")
        monkeypatch.setattr(pf, "load_scene_routes", _boom)
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            assert await pf.resolve_scene_config("content_draft") is None
        assert any("读取场景路由失败" in r.message for r in caplog.records), \
            "场景路由读取失败必须留 WARNING（否则静默停摆无从排查）"

    def test_no_bare_silent_swallow_left(self):
        """静态锁：``resolve_scene_config`` 内不得再有「无日志 except → None」。"""
        src = inspect.getsource(pf.resolve_scene_config)
        silent = re.findall(r"except Exception[^\n]*:\n\s+return None", src)
        assert not silent, f"仍存在零日志的 except → return None：{silent}"


# ===========================================================================
# B4 · _log_audit 的裸 pass
# ===========================================================================
class TestLogAuditObservability:
    async def test_reliability_failure_still_buffers_audit(
            self, db_conn, monkeypatch, caplog
    ):
        """统计更新抛错时，审计行仍必须入缓冲（修复前两者被同一个 pass 吞掉）。"""
        monkeypatch.setattr(pf, "_reliability_key",
                            _raise("统计键计算失败"))
        before = len(pf._audit_buffer)
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._log_audit("prov", "model", "chat", 1.0, True, scene="content_draft")
        assert len(pf._audit_buffer) == before + 1, "统计失败不得连带吞掉审计行"
        assert any("实时可靠性统计失败" in r.message for r in caplog.records)

    async def test_buffer_failure_is_logged(self, db_conn, monkeypatch, caplog):
        async def _boom():
            raise RuntimeError("落库失败")
        monkeypatch.setattr(pf, "_flush_audit_buffer", _boom)
        monkeypatch.setattr(pf, "_AUDIT_BATCH_SIZE", 1)
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._log_audit("prov", "model", "chat", 1.0, True)
        assert any("审计缓冲失败" in r.message for r in caplog.records), \
            "审计丢失必须可观测（用量统计整块功能否则静默为空）"

    def test_no_bare_pass_left(self):
        src = inspect.getsource(pf._log_audit)
        assert not re.search(r"except Exception:\s*\n\s*pass", src), \
            "_log_audit 内不得再有裸 except: pass"


def _raise(msg):
    def _f(*_a, **_kw):
        raise RuntimeError(msg)
    return _f


# ===========================================================================
# B5 · 审计降级写入的行宽守卫
# ===========================================================================
class _FakeConn:
    """模拟旧库：FULL(13 列) 与 NO_IDENTITY(11 列) 两种 INSERT 均因缺列失败。

    LEGACY(10 列) 模拟补齐 scene 后成功 —— 复刻真实的「两级降级」行为。
    """

    def __init__(self):
        self.calls: list[tuple[int, list]] = []
        self.commits = 0

    async def executemany(self, sql, rows):
        ncols = len(sql.split("?")) - 1
        self.calls.append((ncols, list(rows)))
        if ncols >= 11:
            raise RuntimeError("no such column: config_id")
        return None

    async def commit(self):
        self.commits += 1


class TestAuditDegradeWidthGuard:
    async def test_full_width_rows_degrade_in_order(self, db_conn, monkeypatch):
        conn = _FakeConn()

        @contextlib.asynccontextmanager
        async def _fake():
            yield conn
        monkeypatch.setattr(pf, "write_tx_conn", _fake)
        rows = [tuple(range(13)) for _ in range(2)]
        await pf._flush_audit_rows_once(rows)
        # FULL(13) → NO_IDENTITY(11) → LEGACY(10)；宽度与顺序都不得乱
        assert [n for n, _ in conn.calls] == [13, 11, 10]
        assert [len(r) for r in conn.calls[1][1]] == [11, 11]
        assert [len(r) for r in conn.calls[2][1]] == [10, 10]
        assert conn.commits == 1

    async def test_too_narrow_rows_are_not_silently_miswritten(
            self, db_conn, monkeypatch, caplog
    ):
        """A/B 承重例：行宽不足时旧实现会按位切片写出「错位的值」。"""
        conn = _FakeConn()

        @contextlib.asynccontextmanager
        async def _fake():
            yield conn
        monkeypatch.setattr(pf, "write_tx_conn", _fake)
        rows = [tuple(range(8))]
        with caplog.at_level(logging.ERROR, logger="provider_factory"):
            await pf._flush_audit_rows_once(rows)
        # 只有 FULL 一次尝试；两级降级都因行宽不足被跳过（没有写出错位数据）
        assert [n for n, _ in conn.calls] == [13], f"不应有任何降级写入：{conn.calls}"
        assert any("无法降级写入" in r.message for r in caplog.records)

    def test_width_guard_present_in_source(self):
        src = inspect.getsource(pf._flush_audit_rows_once)
        assert "len(r) >= 11" in src and "len(r) >= 10" in src


# ===========================================================================
# B6 · 阈值单一源 + settings 数值读取单一出口
# ===========================================================================
class TestSettingNumSingleExit:
    def test_zero_is_respected_not_treated_as_unset(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate", 0, raising=False)
        assert pf._setting_num("ai_provider_demote_success_rate", 0.60) == 0.0

    def test_none_and_empty_fall_back_to_default(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate", None, raising=False)
        assert pf._setting_num("ai_provider_demote_success_rate", 0.60) == 0.60
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate", "", raising=False)
        assert pf._setting_num("ai_provider_demote_success_rate", 0.60) == 0.60

    def test_garbage_falls_back_and_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate",
                            "abc", raising=False)
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            assert pf._setting_num("ai_provider_demote_success_rate", 0.60) == 0.60
        assert any("取值非法" in r.message for r in caplog.records)

    def test_lo_hi_clamps_applied(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate", -5, raising=False)
        assert pf._setting_num("ai_provider_demote_success_rate", 0.6, lo=0.0) == 0.0
        monkeypatch.setattr(pf.settings, "ai_provider_demote_success_rate", 99, raising=False)
        assert pf._setting_num("ai_provider_demote_success_rate", 0.6, hi=1.0) == 1.0

    def test_min_and_dead_rate_are_the_same_threshold(self):
        """同一 setting 只能有**一个**门限常量（否则两条链路各判各的）。"""
        assert pf._PROVIDER_MIN_SUCCESS_RATE == pf._PROVIDER_DEAD_SUCCESS_RATE

    def test_dead_success_rate_setting_read_only_once(self):
        """静态锁：该 setting 的名字在整个模块里只能出现一次（_setting_num 的实参）。

        出现两次以上 = 又多了一份独立读取，两处各自演进即产生不同门限。
        """
        src = inspect.getsource(pf)
        hits = re.findall(r'"ai_provider_dead_success_rate"', src)
        assert len(hits) == 1, f"同一 setting 被引用 {len(hits)} 次，必须收敛为 _setting_num 单一出口"
        assert 'getattr(settings, "ai_provider_dead_success_rate"' not in src, \
            "禁止绕过 _setting_num 直接读（会把显式 0 当成未配置）"

    def test_old_or_default_idiom_gone_from_rate_constants(self):
        src = inspect.getsource(pf)
        assert 'getattr(settings, "ai_provider_demote_success_rate", 0.60) or 0.60' not in src


# ===========================================================================
# B7 · 多环境下 health 提示不再误导
# ===========================================================================
class TestHealthHintUnderMultiEnv:
    async def test_env_without_primary_gives_actionable_hint(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, env, is_active, priority, concurrency)"
            " VALUES ('devcfg','devprov','pay_as_you_go',?, 'https://api.test.com/v1',"
            " 'm', 'dev', 1, 0, 4)",
            (encrypt_api_key("sk-dev"),))
        await db_conn.execute(
            "INSERT INTO ai_runtime_settings(key, value) VALUES(?,?)",
            (pf.RUNTIME_ACTIVE_ENV_KEY, "prod"))
        await db_conn.commit()
        pf.invalidate_config_cache()

        h = await ai_router.ai_health(db=db_conn)
        assert h["status"] == "not_configured", "status 契约不得变化"
        assert "当前生效环境「prod」" in h["hint"]
        assert "设为当前使用" in h["hint"]
        assert "尚未启用任何文本模型配置" not in h["hint"], \
            "库里明明有配置，却提示「尚未配置」= 把用户指向错误的修法"

    async def test_empty_db_hint_unchanged(self, db_conn):
        h = await ai_router.ai_health(db=db_conn)
        assert h["hint"] == "尚未启用任何文本模型配置，所有 AI 生成能力将不可用"

    async def test_no_env_keeps_legacy_hint(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, env, is_active, priority, concurrency)"
            " VALUES ('g','p','pay_as_you_go',?, 'https://api.test.com/v1','m','',0,0,4)",
            (encrypt_api_key("sk-g"),))
        await db_conn.commit()
        pf.invalidate_config_cache()
        h = await ai_router.ai_health(db=db_conn)
        assert h["status"] == "not_configured"
        assert h["hint"] == "尚未启用任何文本模型配置，所有 AI 生成能力将不可用"


# ===========================================================================
# B9 · request_mode 白名单单一出口
# ===========================================================================
class TestRequestModeSingleSource:
    def test_valid_modes_matches_normalizer(self):
        assert VALID_REQUEST_MODES == ("normal", "stream")
        for m in VALID_REQUEST_MODES:
            assert pf.normalize_request_mode(m) == m

    def test_connectivity_does_not_redeclare_the_whitelist(self):
        """静态锁：connectivity 侧不得再自带一份 (normal, stream) 字面量。"""
        src = inspect.getsource(conn_module)
        assert '("normal", "stream")' not in src, \
            "connectivity.py 不得重实现 request_mode 白名单"
        assert "VALID_REQUEST_MODES" in src

    def test_probe_mode_semantics_preserved(self):
        """``auto`` 仍只出现在探测侧，且含义不变（未指定 → 流式优先）。"""
        assert conn_module is not None
        for raw, expect in (("stream", "stream"), ("normal", "normal"),
                            ("", "auto"), ("junk", "auto")):
            probe_mode = raw if raw in VALID_REQUEST_MODES else "auto"
            assert probe_mode == expect
            assert (probe_mode != "normal") == (raw != "normal" or raw == "")


# ===========================================================================
# B10 · upsert_runtime_setting 调用点必须自行失效缓存（防回归）
# ===========================================================================
class TestRuntimeSettingWritePathsInvalidate:
    def test_every_production_caller_invalidates_cache(self):
        """``upsert_runtime_setting`` 刻意不失效缓存（调用方自行处理）。

        漏一处 = 「配置写进库 ≠ 生效」的静默失效（本仓反复踩的同一类）。
        静态扫全部生产调用点，缺 ``invalidate_config_cache()`` 即失败。
        """
        offenders = []
        for name in ("config", "runtime", "audit", "connectivity",
                     "models", "scene_routes", "usage"):
            mod = __import__(
                f"app.routers.ai_config.{name}", fromlist=[name])
            src = inspect.getsource(mod)
            if "upsert_runtime_setting(" not in src:
                continue
            # 去掉 import 行后再判断是否在同文件内失效
            body = "\n".join(
                ln for ln in src.splitlines()
                if not ln.strip().startswith(("import ", "from ")))
            if "invalidate_config_cache()" not in body:
                offenders.append(name)
        assert not offenders, f"以下模块调用 upsert_runtime_setting 但未失效缓存：{offenders}"


# ===========================================================================
# B11 · 配置快照：脱敏 + 字段漂移护栏 + diff
# ===========================================================================
class TestConfigSnapshot:
    def test_snapshot_fields_cover_ai_config_columns(self):
        """``SNAPSHOT_FIELDS`` 必须覆盖 ai_config 的全部业务列。

        日后新增一列（如 ``proxy_url``）若不同步登记，回滚会静默漏掉该字段，
        而审计 diff 也看不到 —— 属本仓反复出现的「静默失效」。此断言让
        新增列在**测试期**即失败。
        """
        import app.schema_sql as schema_sql

        cols = _ai_config_columns(schema_sql.SCHEMA_SQL)
        excluded = {"id", "api_key_encrypted", "created_at", "updated_at"}
        missing = (cols - excluded) - set(audit_service.SNAPSHOT_FIELDS)
        assert not missing, (
            f"ai_config 新增列未登记进 SNAPSHOT_FIELDS（回滚与审计 diff 会静默漏掉）：{missing}")

    def test_snapshot_only_fields_are_not_rollbackable(self):
        """``is_active`` / ``priority`` 进快照但**不可回滚**（刻意分离）。

        两者由独立端点维护（toggle 切换当前使用 / PUT /fallback-chain 调降级链
        顺序），回滚时恢复旧值会破坏全局不变量：主配置唯一性、降级链 priority
        0..n-1 连续（``_resequence_priority`` 在增删配置后重排，恢复一个过期序号
        会留下空洞或重复）。快照留存只为审计可追溯 —— 「进快照 ≠ 可回滚」是
        设计而非遗漏。此断言锁死后，任何想把它们加进 ``ROLLBACK_FIELDS`` 的改动
        都会在测试期失败。
        """
        from app.routers.ai_config import audit as audit_router

        snapshot_only = {"is_active", "priority"}
        missed = snapshot_only - set(audit_service.SNAPSHOT_FIELDS)
        assert not missed, (
            f"以下字段应进快照（审计可追溯）但被漏登：{sorted(missed)}")
        leaked = snapshot_only & set(audit_router.ROLLBACK_FIELDS)
        assert not leaked, (
            f"以下字段不得参与回滚写入（由独立端点维护，恢复旧值会破坏全局不变量）："
            f"{sorted(leaked)}")

    def test_rollback_fields_are_subset_of_snapshot_fields(self):
        """``ROLLBACK_FIELDS`` 必须是 ``SNAPSHOT_FIELDS`` 的子集。

        回滚只能恢复「快照里确实捕获过」的字段 —— 否则会出现「按钮可点却写回
        空值」的静默失真（与 AGENTS §4.7 BUG-P1-E 的 rollbackable 判据同源）。
        """
        from app.routers.ai_config import audit as audit_router

        missing = set(audit_router.ROLLBACK_FIELDS) - set(audit_service.SNAPSHOT_FIELDS)
        assert not missing, (
            f"可回滚字段未进快照，回滚会写回空值：{sorted(missing)}")

    def test_snapshot_captures_priority_for_traceability(self):
        """快照捕获 priority，diff 能报出降级链顺序变化（可追溯）。"""
        snap = audit_service.sanitize_config_snapshot(
            {"provider_name": "p", "priority": 2, "model": "m"})
        assert snap["priority"] == 2
        changes = audit_service.diff_snapshots({
            "before": {"provider_name": "p", "priority": 2, "model": "m"},
            "after": {"provider_name": "p", "priority": 0, "model": "m"},
        })
        priority_changes = [c for c in changes if c["field"] == "priority"]
        assert len(priority_changes) == 1
        assert (priority_changes[0]["before"],
                priority_changes[0]["after"]) == (2, 0)
        assert priority_changes[0]["label"] == "降级链顺序"

    async def test_rollback_does_not_touch_priority(self, db_conn):
        """回滚不得写回过期的降级链序号。

        场景：改动发生时 priority=0；随后用户调整降级链把它挪到 2；
        回滚那次改动不应把它挪回 0（会在降级链里留下空洞 / 重复序号）。
        """
        from app.models import AIConfigIn, ConfigRollbackIn

        res = await ai_router.save_config(
            AIConfigIn(provider_name="deepseek", model="v1-model",
                       api_key="sk-x"), db=db_conn)
        cid = res["id"]
        await ai_router.save_config(
            AIConfigIn(provider_name="deepseek", id=cid, model="v2-model"),
            db=db_conn)
        cur = await db_conn.execute(
            "SELECT id FROM ai_config_audit_logs WHERE action='update'"
            " AND config_id=? ORDER BY rowid DESC LIMIT 1", (cid,))
        update_id = (await cur.fetchone())[0]

        await db_conn.execute(
            "UPDATE ai_config SET priority=? WHERE id=?", (2, cid))
        await db_conn.commit()
        cur = await db_conn.execute(
            "SELECT priority FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == 2

        rb = await ai_router.rollback_config(
            cid, ConfigRollbackIn(audit_id=update_id), db=db_conn)
        assert rb["ok"] is True
        cur = await db_conn.execute(
            "SELECT model, priority FROM ai_config WHERE id=?", (cid,))
        row = await cur.fetchone()
        assert row[0] == "v1-model", "模型应被回滚"
        assert row[1] == 2, (
            "回滚不得写回过期的降级链序号（会在 priority 里留下空洞或重复）")
        assert "priority" not in (rb.get("restored") or {}), (
            "restored 不应声称回滚了降级链顺序")

    def test_snapshot_fields_never_contain_secrets(self):
        assert "api_key_encrypted" not in audit_service.SNAPSHOT_FIELDS
        assert "api_key" not in audit_service.SNAPSHOT_FIELDS
        assert "api_key_encrypted" not in audit_service.FIELD_LABELS

    def test_snapshot_has_key_matches_decryptable_ciphertext(self):
        assert audit_service.sanitize_config_snapshot(
            {"provider_name": "p", "api_key_encrypted": encrypt_api_key("sk-abc1234")})[
                "has_key"] == 1
        assert audit_service.sanitize_config_snapshot(
            {"provider_name": "p", "api_key_encrypted": ""})["has_key"] == 0
        assert audit_service.sanitize_config_snapshot(
            {"provider_name": "p", "api_key_encrypted": "not-a-valid-token"})["has_key"] == 0

    def test_snapshot_truncates_long_strings(self):
        long_remark = "x" * 5000
        snap = audit_service.sanitize_config_snapshot(
            {"remark": long_remark, "provider_name": "y" * 500,
             "model": "m" * 2000, "env": "e" * 500, "plan": "p" * 100})
        assert len(snap["remark"]) == 2000
        assert len(snap["provider_name"]) == 128
        assert len(snap["model"]) == 512
        assert len(snap["env"]) == 64
        assert len(snap["plan"]) == 32

    def test_snapshot_returns_none_for_empty(self):
        assert audit_service.sanitize_config_snapshot(None) is None
        assert audit_service.sanitize_config_snapshot({}) is None

    def test_diff_only_reports_real_changes(self):
        before = {"model": "a", "temperature": 0.7, "has_key": 0}
        after = {"model": "a", "temperature": 0.9, "has_key": 1}
        changes = audit_service.diff_snapshots({"before": before, "after": after})
        fields = {c["field"] for c in changes}
        assert fields == {"temperature", "has_key"}
        key_change = next(c for c in changes if c["field"] == "has_key")
        assert (key_change["before"], key_change["after"]) == ("未设置", "已设置")

    def test_diff_guards_bad_shapes(self):
        assert audit_service.diff_snapshots(None) == []
        assert audit_service.diff_snapshots("x") == []
        assert audit_service.diff_snapshots({"before": "x", "after": {}}) == []

    def test_field_labels_cover_all_snapshot_fields(self):
        assert set(audit_service.SNAPSHOT_FIELDS) <= set(audit_service.FIELD_LABELS)

    async def test_audit_row_never_carries_plaintext_key(self, db_conn):
        from app.models import AIConfigIn
        await ai_router.save_config(
            AIConfigIn(provider_name="deepseek", model="deepseek-chat",
                       base_url="https://api.deepseek.com/v1",
                       api_key="sk-supersecret9999"),
            db=db_conn)
        cur = await db_conn.execute(
            "SELECT detail, snapshot_json FROM ai_config_audit_logs")
        rows = [dict(r) for r in await cur.fetchall()]
        assert rows, "保存配置必须写一条变更审计"
        for d in rows:
            assert "sk-supersecret9999" not in (d["detail"] or "")
            assert "sk-supersecret9999" not in (d["snapshot_json"] or "")
            assert "supersecret" not in (d["snapshot_json"] or "")

    def test_detail_is_truncated(self):
        assert audit_service._DETAIL_MAX == 500


def _ai_config_columns(schema_sql_text: str) -> set[str]:
    """从 ``SCHEMA_SQL`` 提取 ``ai_config`` 的列名集合（跳过注释行）。"""
    m = re.search(r"CREATE TABLE IF NOT EXISTS ai_config\s*\(.*?\n\);",
                  schema_sql_text, re.S)
    assert m, "未能在 schema_sql.SCHEMA_SQL 中定位 ai_config 建表语句"
    body = "\n".join(ln for ln in m.group(0).splitlines()
                     if not ln.strip().startswith("--"))
    return set(re.findall(r"^\s{4}(\w+)\s+\w", body, re.M))


# ===========================================================================
# B12 · GET /ai/models 契约
# ===========================================================================
class TestModelsEndpointContract:
    async def test_normalized_preset_shape(self):
        res = await ai_router.list_models()
        provs = res["providers"]
        assert provs, "预设清单不得为空"
        expected_keys = {"label", "models", "plans", "description", "website",
                         "pricing", "default_model", "base_url", "supports_vision"}
        for key, item in provs.items():
            assert set(item) == expected_keys, f"{key} 的键集合漂移"
            assert isinstance(item["supports_vision"], bool)
            assert item["default_model"], f"{key} 缺少 default_model"

    async def test_default_model_differs_from_raw_presets(self):
        """AGENTS §5.12：本端点用 ``default_model``，``GET /ai/config`` 的 presets
        是**原始** ``model`` —— 形状刻意不同，前端分两套类型建模。
        这里锁定差异确实存在，避免日后被「顺手统一」掉而打断前端。"""
        res = await ai_router.list_models()
        raw = PROVIDER_PRESETS["deepseek"]
        assert res["providers"]["deepseek"]["default_model"] == raw["model"]
        assert "default_model" not in raw

    async def test_plans_injected_for_every_preset(self):
        res = await ai_router.list_models()
        for key, item in res["providers"].items():
            assert set(item["plans"]) == {"pay_as_you_go", "coding_plan"}, key

    async def test_unknown_scene_route_still_rejected(self, db_conn):
        """顺带锁定场景白名单门禁（PUT 未知场景 → 400）。"""
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await ai_router.update_scene_route(
                body=SceneRouteUpdate(scene="not_a_scene", config_id=""),
                db=db_conn)
