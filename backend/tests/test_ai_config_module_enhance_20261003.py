"""文本模型配置模块增强回归（2026-10-03）

覆盖本轮四组收口（判据锚定实现，不锚定字符）：
1. 数值缺省值单一出口（DEFAULT_CONFIG_NUMBERS）：此前 timeout 在
   schema/AIConfigIn 声明 900、运行时候选兜底 60 —— 配置行缺字段或为 0 时
   主候选超时静默缩水到 60s。现统一引用，缺省=900。
2. 并发上限配置化（settings.max_concurrency，默认 5）：默认行为零变化。
3. 密钥解密失败与未填 Key 在运行时门控可区分（key_broken 标记 + 分开归因）。
4. 入参长度约束（AIConfigIn / AIConfigTest / ProviderModelsIn）与
   探测 max_tokens 钳制（与保存链路同口径）。
"""
import pytest
from pydantic import ValidationError

import app.services.ai.provider_factory as pf
from app.config import settings
from app.models import AIConfigIn, AIConfigTest, ProviderModelsIn
from app.services.crypto import encrypt_api_key


# ===========================================================================
# 1. 数值缺省值单一出口
# ===========================================================================
class TestDefaultNumbersSingleSource:
    def test_clamp_fallback_uses_shared_defaults(self):
        """全空入参时 clamp_config_numbers 的兜底必须等于声明默认值。"""
        assert pf.clamp_config_numbers({}) == pf.DEFAULT_CONFIG_NUMBERS

    def test_candidate_timeout_missing_uses_declared_default(self):
        """缺 timeout 的主候选（外部构造 dict / 脏行）按声明默认 900s，
        不再回落旧硬编码 60s（长正文生成曾因此必超时）。
        注：非主候选仍受 ai_fallback_attempt_timeout 上限约束。"""
        c = {"provider_name": "x", "_is_primary": True}
        assert pf._candidate_timeout(c, None) == 900

    def test_candidate_timeout_zero_uses_declared_default(self):
        """timeout=0（falsy 脏值）同样按声明默认处理。"""
        c = {"provider_name": "x", "timeout": 0, "_is_primary": True}
        assert pf._candidate_timeout(c, None) == 900

    def test_fallback_candidate_still_capped(self):
        """降级候选的单次尝试超时上限（120s）不受缺省值对齐影响。"""
        c = {"provider_name": "x", "timeout": 900, "_is_primary": False}
        cap = int(getattr(settings, "ai_fallback_attempt_timeout", 120) or 120)
        assert pf._candidate_timeout(c, None) == cap

    def test_caller_timeout_still_wins(self):
        """调用方显式 req_timeout 仍优先（主候选完整预算语义不变）。"""
        c = {"provider_name": "x", "timeout": 900, "_is_primary": True}
        assert pf._candidate_timeout(c, 300) == 300

    async def test_build_candidates_defaults(self, db_conn):
        """_build_candidates 对缺字段 cfg 的缺省值与声明一致。"""
        cfg = {"id": "c1", "provider_name": "deepseek",
               "api_key": "sk-t", "base_url": "https://api.x.com/v1",
               "model": "m"}
        out = await pf._build_candidates(cfg)
        primary = out[0]
        assert primary["timeout"] == 900
        assert primary["max_tokens"] == 8192
        assert primary["temperature"] == 0.7

    def test_attempt_candidate_defaults_aligned(self):
        """_attempt_candidate 的 max_tokens/temperature 缺省引用同一张表。"""
        # 通过源码静态锁防止回退为散落字面量（判据锚定实现）
        import inspect
        src = inspect.getsource(pf)
        assert 'c.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"])' in src
        assert 'DEFAULT_CONFIG_NUMBERS["temperature"]' in src
        # 旧的散落缺省不得回潮（60 只允许出现在非缺省语义处）
        assert 'c.get("timeout", 60)' not in src
        assert '(c.get("timeout") or 60)' not in src


# ===========================================================================
# 2. 并发上限配置化（默认 5，行为零变化）
# ===========================================================================
class TestConcurrencyBound:
    def test_range_bound_follows_settings_default(self):
        assert pf._MAX_CONCURRENCY == int(settings.max_concurrency or 5)
        assert pf._RANGE["concurrency"] == (1, pf._MAX_CONCURRENCY)

    def test_clamp_concurrency_over_limit(self):
        assert pf.clamp_config_numbers({"concurrency": 99})["concurrency"] \
            == pf._MAX_CONCURRENCY

    def test_clamp_concurrency_under_limit(self):
        """下界钳到 1；非数值（TypeError/ValueError）才走缺省 4。"""
        assert pf.clamp_config_numbers({"concurrency": 0})["concurrency"] == 1
        assert pf.clamp_config_numbers({"concurrency": "abc"})["concurrency"] == 4
        assert pf.clamp_config_numbers({"concurrency": None})["concurrency"] == 4


# ===========================================================================
# 3. key_broken 门控区分
# ===========================================================================
def _broken_candidate(**over):
    c = {"config_id": "c1", "provider_name": "deepseek", "api_key": "",
         "base_url": "https://api.x.com/v1", "model": "m",
         "max_tokens": 8192, "temperature": 0.7, "timeout": 900,
         "key_broken": True, "_is_primary": True}
    c.update(over)
    return c


class TestKeyBrokenGate:
    @pytest.fixture(autouse=True)
    def _isolate_reliability_state(self, monkeypatch):
        """确定性隔离：候选顺序不得取决于进程调用史（flaky 根因回归）。

        ✅ 修复（2026-10-03）：_order_candidates 的「死配置剔除 / 主配置后置」
        依赖进程级 _provider_reliability（由其他测试直接写入、或由启动预热从
        审计日志近 24h 真实失败史加载）——全量套件中 deepseek 历史低成功率会
        把主候选后置/剔除，本类三条「cands[0] 是主候选」断言随之定向失败
        （单跑通过、全量失败的典型顺序依赖 flaky）。本 fixture 只关闭排序
        规则（属编排层行为，非本类待验的 key_broken 判据），生产代码零改动；
        排序规则自身由 test_perf_content_pipeline / test_reliability_granularity
        显式注入态另行锁定。
        """
        monkeypatch.setattr(pf, "_is_dead_provider", lambda *_a, **_k: False)
        monkeypatch.setattr(pf, "_provider_reliability_snapshot",
                            lambda *_a, **_k: (0, None))

    def test_build_candidates_marks_broken_key_isolated_despite_dead_stats(self):
        """回归锁：即使进程里残留了主配置的死亡级失败统计，隔离后的构建判据
        仍稳定把首位候选指向主配置（摘掉 fixture 即定向失败）。"""
        import asyncio
        key = pf._reliability_key("deepseek", "m", "c1", "https://api.x.com/v1")
        with pf._provider_reliability_lock:
            saved = pf._provider_reliability.get(key)
            pf._provider_reliability[key] = {"ok": 0, "fail": 50}
        try:
            cfg = {"id": "c1", "provider_name": "deepseek",
                   "api_key_encrypted": "gAAAA-not-a-valid-token",
                   "base_url": "https://api.x.com/v1", "model": "m"}
            cands = asyncio.run(pf._build_candidates(cfg))
            assert cands and cands[0]["config_id"] == "c1"
            assert cands[0]["key_broken"] is True
        finally:
            with pf._provider_reliability_lock:
                if saved is None:
                    pf._provider_reliability.pop(key, None)
                else:
                    pf._provider_reliability[key] = saved

    def test_build_candidates_marks_broken_key(self):
        """密文存在但解不开 → key_broken=True（不再与未填 Key 混同）。"""
        import asyncio
        cfg = {"id": "c1", "provider_name": "deepseek",
               "api_key_encrypted": "gAAAA-not-a-valid-token",
               "base_url": "https://api.x.com/v1", "model": "m"}
        cands = asyncio.run(pf._build_candidates(cfg))
        assert cands and cands[0]["key_broken"] is True
        assert cands[0]["api_key"] == ""

    def test_build_candidates_valid_key_not_broken(self):
        import asyncio
        cfg = {"id": "c1", "provider_name": "deepseek",
               "api_key_encrypted": encrypt_api_key("sk-real"),
               "base_url": "https://api.x.com/v1", "model": "m"}
        cands = asyncio.run(pf._build_candidates(cfg))
        assert cands[0]["key_broken"] is False
        assert cands[0]["api_key"] == "sk-real"

    def test_build_candidates_no_enc_not_broken(self):
        """未填 Key（无密文）不算 broken —— 两者修法不同，不得混报。"""
        import asyncio
        cfg = {"id": "c1", "provider_name": "deepseek",
               "api_key_encrypted": "",
               "base_url": "https://api.x.com/v1", "model": "m"}
        cands = asyncio.run(pf._build_candidates(cfg))
        assert cands[0]["key_broken"] is False

    async def test_gate_broken_only_reports_decryption(self):
        with pytest.raises(RuntimeError, match="无法解密"):
            await pf._gate_candidates([_broken_candidate()], scene="t")

    async def test_gate_broken_and_nokey_reports_count(self):
        nokey = _broken_candidate(config_id="c2", key_broken=False)
        with pytest.raises(RuntimeError, match="1 条已保存的密钥无法解密"):
            await pf._gate_candidates([_broken_candidate(), nokey], scene="t")

    async def test_gate_usable_passes_positive_control(self):
        """阳性对照：有可用候选时门控必须放行（防判据判否导致静默通过）。"""
        usable = _broken_candidate(api_key="sk-ok", key_broken=False)
        out, last_err = await pf._gate_candidates([usable], scene="t")
        assert out == [usable] and last_err is None


# ===========================================================================
# 4. 入参长度约束 / 探测参数钳制
# ===========================================================================
class TestInputLengthConstraints:
    def test_model_over_limit_rejected(self):
        with pytest.raises(ValidationError):
            AIConfigIn(provider_name="deepseek", model="m" * 201)

    def test_model_at_limit_accepted(self):
        AIConfigIn(provider_name="deepseek", model="m" * 200)

    def test_api_key_over_limit_rejected(self):
        with pytest.raises(ValidationError):
            AIConfigIn(provider_name="deepseek", api_key="k" * 4097)

    def test_base_url_over_limit_rejected(self):
        with pytest.raises(ValidationError):
            AIConfigTest(base_url="https://x.com/" + "a" * 2048)

    def test_remark_over_limit_truncated_not_rejected(self):
        """remark 保持既有「超长截断」语义（save_ai_config 截到 2000），
        模型层不拒绝 —— 回归锁定 test_long_snapshot_is_valid_json_and_remark_is_bounded。"""
        assert AIConfigIn(provider_name="deepseek", remark="r" * 5000).remark == "r" * 5000

    def test_provider_models_in_config_id_over_limit(self):
        with pytest.raises(ValidationError):
            ProviderModelsIn(config_id="c" * 65)

    def test_existing_legal_payload_unaffected(self):
        """向后兼容：既有合法表单载荷原样通过。"""
        m = AIConfigIn(provider_name="deepseek", plan="pay_as_you_go",
                       api_key="sk-abc", base_url="https://api.deepseek.com/v1",
                       model="deepseek-chat", env="prod")
        assert m.model == "deepseek-chat" and m.env == "prod"


class TestProbeMaxTokensClamp:
    def test_probe_max_tokens_upper(self):
        """探测 max_tokens 与保存链路同一口径钳到 200000。"""
        assert pf.clamp_config_numbers({"max_tokens": 10 ** 12})["max_tokens"] == 200000

    def test_probe_max_tokens_lower(self):
        assert pf.clamp_config_numbers({"max_tokens": 1})["max_tokens"] == 256

    def test_connectivity_wiring_locked(self):
        """接线静态锁：test_config 的探测参数必须经 clamp_config_numbers。"""
        import inspect
        from app.routers.ai_config import connectivity
        src = inspect.getsource(connectivity.test_config)
        assert "clamp_config_numbers" in src


# ===========================================================================
# 5. 降级链：密文解不开的候选显式告警（不再静默吞）
# ===========================================================================
class TestFallbackChainBrokenKeyWarning:
    async def test_broken_key_candidate_skipped_with_warning(self, db_conn, caplog):
        import logging
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, is_active, priority, timeout, concurrency)"
            " VALUES ('cbrk','deepseek','pay_as_you_go','gAAAA-not-a-valid-token',"
            "'https://api.x.com/v1','m',0,9,60,4)")
        await db_conn.commit()
        pf.invalidate_config_cache()
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            chain = await pf._fallback_chain()
        assert all(c.get("config_id") != "cbrk" for c in chain)
        assert any("无法解密" in r.message for r in caplog.records)

    async def test_healthy_chain_still_works(self, db_conn):
        """阳性对照：健康候选照常入链（防空转）。"""
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, is_active, priority, timeout, concurrency)"
            " VALUES ('cok','deepseek','pay_as_you_go',?,"
            "'https://api.x.com/v1','m',0,9,60,4)",
            (encrypt_api_key("sk-ok"),))
        await db_conn.commit()
        pf.invalidate_config_cache()
        chain = await pf._fallback_chain()
        assert any(c.get("config_id") == "cok" for c in chain)
