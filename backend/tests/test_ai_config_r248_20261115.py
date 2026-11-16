"""文本模型配置模块 · R248 配置收敛回归（2026-11-15）。

覆盖 TD-1（P2）：``settings.ai_request_timeout``（config.py 声明 900）此前全库
零消费 —— 实际缺省硬编码在 ``provider_factory.DEFAULT_CONFIG_NUMBERS``，操作员
显式设置 ``AI_REQUEST_TIMEOUT`` 环境变量后完全无效（静默失效）。现改读
settings 并按 ``_RANGE`` 钳制，收敛为单一运行时来源。

护栏（每项先在旧实现上跑红，再修复转绿）：
  1. 默认 900（未设环境变量）逐字节不变；
  2. 源码锁：``getattr(settings, "ai_request_timeout", 900)`` + ``_DEFAULT_TIMEOUT``
     必须同时在场（防止后续有人把运行时来源改回硬编码 900）；
  3. 显式 ``AI_REQUEST_TIMEOUT=120`` 即时生效（行为实证，子进程隔离 import）；
  4. 上界钳制：99999 -> 3600；
  5. 下界钳制：1 -> 10；
  6. ``0`` 按未配置回落 900（与并发字段同口径）。

R249 遗留加固（2026-11-16 续）：
  7. ``max_tokens`` / ``temperature`` / ``concurrency`` 数值默认也收口为
     settings（AI_DEFAULT_MAX_TOKENS / AI_DEFAULT_TEMPERATURE /
     AI_DEFAULT_CONCURRENCY），默认值 8192 / 0.7 / 4 逐字节不变；
  8. 显式环境变量即时生效、越界钳制、temperature=0 尊重合法值；
  9. ``settings.default_concurrency``（.env.example 已声明）结束零消费：
     AdaptiveConcurrencyController 初始档位读取它并钳到 max_c。
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import app.services.ai.provider_factory as pf
import app.services.ai.workflows_base as wb
import pytest
from app.models import DuplicateConfigIn, SceneRouteUpdate
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key

_BACKEND = Path(__file__).resolve().parent.parent
_ROOT = _BACKEND.parent
_SOURCE = _BACKEND / "app" / "services" / "ai" / "provider_factory.py"
_WORKFLOWS_SOURCE = _BACKEND / "app" / "services" / "ai" / "workflows_base.py"


@pytest.fixture(autouse=True)
def _patch_write_tx(db_conn, monkeypatch):
    """update_scene_route 走 write_tx_conn（独立写池）；测试指向内存连接。"""

    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn

    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _insert_cfg(db, cid, provider, model, is_active=0, priority=0):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            cid,
            provider,
            "pay_as_you_go",
            encrypt_api_key("sk-" + cid),
            "https://api.test.com/v1",
            model,
            is_active,
            priority,
            4,
        ),
    )
    await db.commit()


def _run_subprocess(env_value):
    """在独立子进程中 import provider_factory 并回读默认 timeout。

    必须用子进程隔离：``_DEFAULT_TIMEOUT`` 在模块 import 时一次性计算，
    父进程 settings 已固定，改环境变量无法重载。
    """
    code = (
        "import sys\n"
        "sys.path.insert(0, r'" + str(_BACKEND).replace("\\", "\\\\") + "')\n"
        "from app.services.ai.provider_factory import DEFAULT_CONFIG_NUMBERS\n"
        "print(DEFAULT_CONFIG_NUMBERS['timeout'])\n"
    )
    env = os.environ.copy()
    env.pop("AI_REQUEST_TIMEOUT", None)
    if env_value is not None:
        env["AI_REQUEST_TIMEOUT"] = str(env_value)
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程失败: {proc.stderr}"
    return int(proc.stdout.strip())


def _run_subprocess_numbers(env_overrides: dict[str, str] | None = None) -> dict:
    """在独立子进程中 import provider_factory 并回读四个数值默认。"""
    code = (
        "import sys\n"
        "sys.path.insert(0, r'" + str(_BACKEND).replace("\\", "\\\\") + "')\n"
        "from app.services.ai.provider_factory import DEFAULT_CONFIG_NUMBERS\n"
        "for k in ('max_tokens', 'temperature', 'timeout', 'concurrency'):\n"
        "    print(DEFAULT_CONFIG_NUMBERS[k])\n"
    )
    env = os.environ.copy()
    for _k in (
        "AI_DEFAULT_MAX_TOKENS",
        "AI_DEFAULT_TEMPERATURE",
        "AI_DEFAULT_CONCURRENCY",
        "AI_REQUEST_TIMEOUT",
    ):
        env.pop(_k, None)
    if env_overrides:
        env.update({str(k): str(v) for k, v in env_overrides.items()})
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程失败: {proc.stderr}"
    vals = [line.strip() for line in proc.stdout.strip().splitlines()]
    assert len(vals) == 4
    return {
        "max_tokens": int(vals[0]),
        "temperature": float(vals[1]),
        "timeout": int(vals[2]),
        "concurrency": int(vals[3]),
    }


def test_default_timeout_is_900_without_env():
    """未设置 AI_REQUEST_TIMEOUT 时默认必须仍是 900（零行为变化）。"""
    assert _run_subprocess(None) == 900


def test_source_lock_settings_consumed_not_hardcoded():
    """源码锁：运行时来源必须读 settings，而不是改回硬编码 900。"""
    src = _SOURCE.read_text(encoding="utf-8")
    assert "getattr(settings, \"ai_request_timeout\", 900)" in src
    assert "_DEFAULT_TIMEOUT" in src
    # 旧硬编码 900 若以 DEFAULT_CONFIG_NUMBERS 字典字面量形式重现即视为回退
    assert '"timeout": 900' not in src


def test_env_120_is_effective():
    """显式 AI_REQUEST_TIMEOUT=120 必须即时生效（旧实现静默忽略）。"""
    assert _run_subprocess(120) == 120


def test_env_above_range_clamped_to_3600():
    """超上界 99999 必须钳制到 _RANGE 上界 3600，不落库不炸运行时。"""
    assert _run_subprocess(99999) == 3600


def test_env_below_range_clamped_to_10():
    """超下界 1 必须钳制到 _RANGE 下界 10。"""
    assert _run_subprocess(1) == 10


def test_env_zero_falls_back_to_900():
    """0 按未配置处理回落 900（与 concurrency 的 ``or 5`` 同口径）。"""
    assert _run_subprocess(0) == 900


def test_default_numbers_match_back_compat_without_env():
    """未设置新环境变量时，四个数值默认必须与既有字面量逐字一致。"""
    assert _run_subprocess_numbers() == {
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout": 900,
        "concurrency": 4,
    }


def test_env_default_numbers_are_effective():
    """显式设置三个新环境变量必须即时生效（旧实现为模块级字面量）。"""
    got = _run_subprocess_numbers(
        {
            "AI_DEFAULT_MAX_TOKENS": "12000",
            "AI_DEFAULT_TEMPERATURE": "0.1",
            "AI_DEFAULT_CONCURRENCY": "3",
            "AI_REQUEST_TIMEOUT": "120",
        }
    )
    assert got == {"max_tokens": 12000, "temperature": 0.1, "timeout": 120, "concurrency": 3}


def test_env_default_numbers_clamped_to_ranges():
    """越界默认值必须按 _RANGE 钳制，不能带脏值进入运行链路。"""
    got = _run_subprocess_numbers(
        {
            "AI_DEFAULT_MAX_TOKENS": "99999999",
            "AI_DEFAULT_TEMPERATURE": "-1",
            "AI_DEFAULT_CONCURRENCY": "999",
        }
    )
    assert got["max_tokens"] == 200000
    assert got["temperature"] == 0.0
    assert got["concurrency"] == pf._MAX_CONCURRENCY
    assert got["timeout"] == 900, "本轮未覆盖 timeout 时保持旧回落逻辑"


def test_temperature_zero_is_respected():
    """AI_DEFAULT_TEMPERATURE=0 是合法默认值，不得被 ``or default`` 吞成 0.7。"""
    got = _run_subprocess_numbers({"AI_DEFAULT_TEMPERATURE": "0"})
    assert got["temperature"] == 0.0


def test_default_concurrency_consumed_by_controller(monkeypatch):
    """settings.default_concurrency 结束零消费：控制器初始档位必须跟随。"""
    monkeypatch.setattr(pf.settings, "default_concurrency", 2)
    ctrl = wb.AdaptiveConcurrencyController()
    assert ctrl.current == ctrl.target == 2


def test_default_concurrency_clamped_to_max(monkeypatch):
    """DEFAULT_CONCURRENCY 超过 max_concurrency 时按上限钳制。"""
    monkeypatch.setattr(pf.settings, "default_concurrency", 99)
    monkeypatch.setattr(pf.settings, "max_concurrency", 2)
    ctrl = wb.AdaptiveConcurrencyController()
    assert ctrl.current == ctrl.target == ctrl.max_c == 2


def test_source_lock_defaults_read_settings():
    """源码锁：新默认值不得回退为 provider_factory 字面量 / 控制器字面量。"""
    pf_src = _SOURCE.read_text(encoding="utf-8")
    for name in ("ai_default_max_tokens", "ai_default_temperature", "ai_default_concurrency"):
        assert name in pf_src
    assert "DEFAULT_CONFIG_NUMBERS: dict = {" in pf_src
    wb_src = _WORKFLOWS_SOURCE.read_text(encoding="utf-8")
    assert 'getattr(_settings, "default_concurrency"' in wb_src


# ===========================================================================
# TD-2（P2）：场景路由到非全局主配置时，全局主配置必须进入降级链
# ===========================================================================
class TestSceneFallbackToGlobalPrimary:
    async def test_scene_routed_call_falls_back_to_global_primary(self, db_conn, monkeypatch):
        """场景路由到非全局主配置时，全局主配置必须作为降级候选被尝试。

        旧实现 ``_fallback_chain`` 固定排除 ``is_active=1`` 全局主配置，而场景
        路由的「主候选」是场景配置而非全局主配置 —— 场景模型失败后用户配的
        主模型完全不被尝试（行为实证 attempted=['fast','other'] 缺 'main'）。
        """
        await _insert_cfg(db_conn, "main", "mainprov", "main-model", is_active=1)
        await _insert_cfg(db_conn, "fast", "fastprov", "fast-model")
        await _insert_cfg(db_conn, "other", "otherprov", "other-model", priority=1)
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="facts_extract", config_id="fast"), db=db_conn
        )
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        attempted = []

        async def fake_attempt(c, messages, **kw):
            attempted.append(c.get("config_id", ""))
            raise RuntimeError("fail all")

        monkeypatch.setattr(pf, "_attempt_candidate", fake_attempt)
        with pytest.raises(Exception):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="facts_extract")
        assert attempted[0] == "fast", "场景路由配置必须是本调用首个候选"
        assert attempted.count("fast") == 1, "场景配置不得因同时存在于降级链而被重复尝试"
        assert attempted.count("main") == 1, "全局主配置应恰好作为一次降级候选被尝试"

    async def test_scene_route_to_global_primary_itself_no_duplicate(self, db_conn, monkeypatch):
        """场景路由指向全局主配置本身时，不得重复尝试（主候选=全局主配置）。"""
        await _insert_cfg(db_conn, "main", "mainprov", "main-model", is_active=1)
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="facts_extract", config_id="main"), db=db_conn
        )
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        attempted = []

        async def fake_attempt(c, messages, **kw):
            attempted.append(c.get("config_id", ""))
            return "ok", None

        monkeypatch.setattr(pf, "_attempt_candidate", fake_attempt)
        await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="facts_extract")
        assert attempted == ["main"], "场景路由指向全局主配置时只能尝试一次"


class TestDuplicateInvalidatesFallbackCache:
    async def test_duplicate_config_invalidates_fallback_cache(self, db_conn, monkeypatch):
        """复制配置后降级链必须立即看到新候选，不得等 TTL 过期。

        旧实现 ``duplicate_config`` 落库 + 审计后直接返回，从不调用
        ``invalidate_config_cache()`` —— 新复制体是降级候选（is_active=0 且有
        Key），但 ``_fallback_chain`` 的 5 分钟缓存仍返回旧链，用户复制后最长
        5 分钟配置不生效（同族「配置写进库 ≠ 生效」静默失效）。
        """
        await _insert_cfg(db_conn, "main", "mainprov", "main-model", is_active=1)
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        pf.invalidate_config_cache()
        before = await pf._fallback_chain()
        before_ids = {c.get("config_id", "") for c in before}

        res = await ai_router.duplicate_config(
            DuplicateConfigIn(source_id="main"), db=db_conn
        )
        after = await pf._fallback_chain()
        after_ids = {c.get("config_id", "") for c in after}
        assert res["id"] not in before_ids, "新复制体在复制前不应存在"
        assert res["id"] in after_ids, "复制后降级链必须立即包含新配置"
