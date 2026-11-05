# -*- coding: utf-8 -*-
"""R103 配置卫生可观测收口护栏（2026-10-18 · R102 遗留 L1）。

背景（R102 日志基线）：provider_factory 的熔断 / 配额冷却 / 实时成功率剔除
都是**进程内存态自愈机制**——「已剔除实时成功率过低的候选」在生产日志刷了
209 条 WARNING，但「哪条配置被判死、样本多少、成功率多少」只存在于日志与
内存，「文本模型配置」页完全看不见 ⇒ 用户只见「降级链 9→3」，只能靠翻日志。
生产实证：4 条 nvidia 死配置（HTTP 404 UnsupportedModel）+ volcengine 不
支持 coding plan 的模型长期占据候选池。

本轮收口（全部只读/加法式，零选路行为变更）：
  A 组 · ``provider_health_snapshot`` 单一访问器（判据与选路同源）；
  B 组 · ``report_dead_configs_at_startup`` 启动死配置报告（只读日志）；
  C 组 · ``GET /ai/configs/health`` 每配置 ``runtime_health`` + 汇总
         ``runtime_dead``（前端可行动展示的数据源）；
  D 组 · 源码锁：main.py 启动接线 + 熔断/冷却跳过日志降噪（R11 先例）；
  E 组 · 字节体检。
"""

import ast
import inspect
import logging
from pathlib import Path

import app.services.ai.provider_factory as pf
import pytest
from app.routers.ai_config import connectivity as conn_mod

BACKEND = Path(__file__).resolve().parent.parent
PF_PY = BACKEND / "app" / "services" / "ai" / "provider_factory.py"
MAIN_PY = BACKEND / "app" / "main.py"
CONN_PY = BACKEND / "app" / "routers" / "ai_config" / "connectivity.py"


@pytest.fixture
def seed_reliability():
    """向进程内存可靠性表灌入两个身份（运行时键形态 cfg:{config_id}）。"""
    with pf._provider_reliability_lock:
        pf._provider_reliability["cfg:dead-cfg"] = {"ok": 0, "fail": 12}
        pf._provider_reliability["cfg:good-cfg"] = {"ok": 9, "fail": 1}
    yield
    with pf._provider_reliability_lock:
        pf._provider_reliability.pop("cfg:dead-cfg", None)
        pf._provider_reliability.pop("cfg:good-cfg", None)


async def _insert_cfg(db, cid, provider="nvidia", model="glm-x", base_url="https://x.example/v1"):
    from app.services.crypto import encrypt_api_key

    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
        " base_url, model, is_active, priority, remark, timeout, concurrency)"
        " VALUES (?,?,?,?,?,?,?,?,'',60,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key("sk-test"), base_url, model, 1, 0),
    )
    await db.commit()


# ---------------------------------------------------------------- A 组：快照访问器
class TestProviderHealthSnapshot:
    def test_dead_identity_detected(self, seed_reliability):
        snap = pf.provider_health_snapshot("nvidia", "glm-x", "dead-cfg", "https://x")
        assert snap["dead"] is True
        assert snap["samples"] == 12
        assert snap["success_rate"] == 0.0

    def test_healthy_identity_not_dead(self, seed_reliability):
        snap = pf.provider_health_snapshot("zhipu", "glm-4", "good-cfg", "https://y")
        assert snap["dead"] is False
        assert snap["samples"] == 10
        assert abs(snap["success_rate"] - 0.9) < 1e-9

    def test_insufficient_samples_never_dead(self):
        """样本不足（冷启动）→ 成功率 None、dead False（不误报）。"""
        snap = pf.provider_health_snapshot("unknown", "m", "no-such-cfg", "")
        assert snap["samples"] == 0
        assert snap["success_rate"] is None
        assert snap["dead"] is False

    def test_snapshot_has_full_shape(self):
        snap = pf.provider_health_snapshot("any", "m", "", "")
        for key in ("samples", "success_rate", "dead", "circuit_open", "cooldown_remaining_s", "quota_cooldown_s"):
            assert key in snap, f"快照缺字段 {key}"

    def test_snapshot_agrees_with_is_dead_provider(self, seed_reliability):
        """判据单一事实源：快照的 dead 必须与 _is_dead_provider 逐字一致。"""
        for cid in ("dead-cfg", "good-cfg"):
            snap = pf.provider_health_snapshot("p", "m", cid, "")
            assert snap["dead"] == pf._is_dead_provider("p", "m", cid, "")


# ---------------------------------------------------------------- B 组：启动报告
class TestReportDeadConfigsAtStartup:
    async def test_reports_only_dead_rows(self, db_conn, seed_reliability, caplog):
        await _insert_cfg(db_conn, "dead-cfg")
        await _insert_cfg(db_conn, "good-cfg", provider="zhipu", model="glm-4", base_url="https://y.example/v1")
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            dead = await pf.report_dead_configs_at_startup()
        assert len(dead) == 1
        assert dead[0]["config_id"] == "dead-cfg"
        assert dead[0]["provider_name"] == "nvidia"
        assert "已被自动剔除出降级链" in caplog.text

    async def test_empty_when_no_dead(self, db_conn, caplog):
        await _insert_cfg(db_conn, "good-cfg", provider="zhipu", model="glm-4")
        with caplog.at_level(logging.INFO, logger="provider_factory"):
            dead = await pf.report_dead_configs_at_startup()
        assert dead == []
        assert "无实时成功率过低的死配置" in caplog.text

    async def test_fail_soft_on_db_error(self, monkeypatch):
        """DB 异常绝不向上传播（启动不得被报告阻断）。"""

        async def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(pf, "get_conn", _boom)
        dead = await pf.report_dead_configs_at_startup()
        assert dead == []


# ---------------------------------------------------------------- C 组：接口接线
class TestConfigsHealthRuntimeHealth:
    async def test_items_carry_runtime_health_and_summary_counts(self, db_conn, seed_reliability, monkeypatch):
        await _insert_cfg(db_conn, "dead-cfg")

        # 网络预检打桩：避免用例依赖真实 DNS/TCP
        async def _fake_net(url):
            return {"ok": True, "step": "ok", "ip": "1.2.3.4", "message": ""}

        monkeypatch.setattr(conn_mod, "_dns_precheck_async", _fake_net)
        monkeypatch.setattr(conn_mod, "decrypt_api_key", lambda enc: "sk-test")

        res = await conn_mod.configs_health(db=db_conn)
        item = next(i for i in res["items"] if i["id"] == "dead-cfg")
        rh = item["runtime_health"]
        assert rh["dead"] is True and rh["samples"] == 12
        assert res["summary"]["runtime_dead"] == 1

    async def test_snapshot_source_is_single(self, db_conn, monkeypatch):
        """接线必须经 provider_health_snapshot 单一出口（判据不与选路分叉）。"""
        await _insert_cfg(db_conn, "c-probe")

        calls = []

        def _spy(provider_name, model="", config_id="", base_url=""):
            calls.append((provider_name, model, config_id))
            return {
                "samples": 0,
                "success_rate": None,
                "dead": False,
                "circuit_open": False,
                "cooldown_remaining_s": 0.0,
                "quota_cooldown_s": 0.0,
            }

        monkeypatch.setattr(pf, "provider_health_snapshot", _spy)

        async def _fake_net(url):
            return {"ok": True, "step": "ok", "ip": "", "message": ""}

        monkeypatch.setattr(conn_mod, "_dns_precheck_async", _fake_net)
        monkeypatch.setattr(conn_mod, "decrypt_api_key", lambda enc: "sk-test")

        await conn_mod.configs_health(db=db_conn)
        assert calls, "configs_health 必须调用 provider_health_snapshot"
        assert all(c[2] == "c-probe" for c in calls), "快照身份必须带 config_id（与运行时 cfg:{id} 键一致）"


# ---------------------------------------------------------------- D 组：源码锁
class TestSourceLocks:
    def test_main_wires_startup_report(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "report_dead_configs_at_startup" in src, "main.py lifespan 必须接线启动死配置报告"
        assert "启动死配置报告失败（不影响服务）" in src, "接线必须 fail-soft（异常只记 warning 不阻断启动）"

    def test_startup_report_after_warmup(self):
        """报告必须在可靠性预热之后（否则统计还没恢复，报告恒空）。"""
        src = MAIN_PY.read_text(encoding="utf-8")
        i_warm = src.find("warmup_reliability_from_db")
        i_report = src.find("report_dead_configs_at_startup")
        assert 0 < i_warm < i_report, "报告必须排在预热之后"

    def test_skip_logs_downgraded_to_info(self):
        """熔断/配额冷却跳过是设计内节流，按 R11 先例降为 INFO（可观测性降噪）。"""
        src = PF_PY.read_text(encoding="utf-8")
        assert 'logger.info("配额冷却中，跳过 %s（冷却窗口 %ds）"' in src
        assert 'logger.info("熔断器 OPEN，跳过 %s", pname)' in src
        # 旧 WARNING 形态不得回流
        assert 'logger.warning("配额冷却中，跳过 %s（冷却窗口 %ds）"' not in src
        assert 'logger.warning("熔断器 OPEN，跳过 %s", pname)' not in src

    def test_snapshot_is_single_accessor_in_endpoint(self):
        """接口只经单一访问器取健康态，不内联另写判据。"""
        src = CONN_PY.read_text(encoding="utf-8")
        assert "provider_health_snapshot(" in src
        assert "_is_dead_provider" not in src, "connectivity 不得自己调死配置判定（判据必须单一出口）"

    def test_snapshot_function_defined_once(self):
        tree = ast.parse(PF_PY.read_text(encoding="utf-8"))
        names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert names.count("provider_health_snapshot") == 1
        assert names.count("report_dead_configs_at_startup") == 1


# ---------------------------------------------------------------- E 组：字节体检
class TestByteHealth:
    @pytest.mark.parametrize("path", [PF_PY, MAIN_PY, CONN_PY])
    def test_no_bom_no_orphan_cr_no_fffd(self, path: Path):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 不得带 BOM"
        assert b"\r" not in raw.replace(b"\r\n", b""), f"{path.name} 不得有孤立 CR"
        assert b"\xef\xbf\xbd" not in raw, f"{path.name} 不得含 U+FFFD"
