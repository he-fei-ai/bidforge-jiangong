"""正文生成 AI 调用次数优化（2026-09-21）回归测试。

本次落地两项「零质量风险 + 立刻可量化」：

  P1-1 scene 场景归因：正文链路 4 处（字数分配 / 首稿 / 续写 / 压缩）+ 全文一致性
      3 处（扫描 / 仲裁 / 定向修复）+ 事实提取 1 处写入 ``ai_audit_logs.scene``，
      /ai/stats 的 by_scene 才能按场景聚合调用次数 —— 这是评估「是否上一
      致性扫描批处理（P0-1）」的度量基准（此前正文链路完全没有场景标记，
      by_scene 里看不到正文生成的任何调用）。

  P0-4 流式能力记忆：平台不支持 stream 时，旧实现每次调用都先失败一次再回退
      普通请求 → N 章正文就白跑 N 次 HTTP。现按 provider+model 记忆连续流式失败，
      达到阈值后在 TTL 内直接走普通请求（流式只改变与厂商之间的传输方式，
      应用侧语义完全不变）。

锁定的不变量：
1. 首次流式失败仍要自动回退普通请求（请求方式不是可用性开关），且**仍会再探测**；
2. 连续 STREAM_FAIL_STREAK 次失败才记忆 —— 单次网络抖动不得判死流式；
3. 记忆有 TTL，到期自动重新探测；流式一旦成功立即清除记忆（平台修复可自愈）；
4. scene 只做归因标记，不改变任何调用语义与参数。
"""
import inspect

import app.services.ai.provider_factory as pf
from app.routers import sections as sec
from app.routers import sse_handlers as sh
from app.services import conflict_arbiter as ca
from app.services import consistency_scanner as cs
from app.services import facts_extractor as fe
from app.services import repair_agent as ra
from app.services.crypto import encrypt_api_key


class _FakeProvider:
    """可控 Provider 替身：分别记录 stream / chat 调用次数，可动态切换行为。"""

    name = "fake"
    model = "m"
    temperature = 0.7
    last_usage: dict = {}
    last_finish_reason = ""

    def __init__(self, *, stream_pieces=None, stream_err=None,
                 chat_text="CHAT", chat_err=None):
        self.stream_pieces = list(stream_pieces or [])
        self.stream_err = stream_err
        self.chat_text = chat_text
        self.chat_err = chat_err
        self.stream_calls = 0
        self.chat_calls = 0

    async def stream(self, messages, **kw):
        self.stream_calls += 1
        if self.stream_err is not None:
            raise self.stream_err
        for piece in self.stream_pieces:
            yield piece

    async def chat(self, messages, **kw):
        self.chat_calls += 1
        if self.chat_err is not None:
            raise self.chat_err
        return self.chat_text


async def _insert(db, cid="main", provider="openai", model="gpt-4o",
                  is_active=1, request_mode="normal"):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, remark, timeout, concurrency, request_mode)"
        " VALUES (?,?,?,?,?,?,?,?,?,60,4,?)",
        (cid, provider, "pay_as_you_go", encrypt_api_key("sk-test"),
         "https://api.test.com/v1", model, is_active, 0, "", request_mode))
    await db.commit()


async def _call(pf_mod, text="hi", **kw):
    return await pf_mod.chat_with_fallback([{"role": "user", "content": text}], **kw)


# ============================================================
# P1-1：scene 场景归因（源码级护栏 + 端到端落库）
# ============================================================
class TestSceneCallSites:
    def test_content_generation_sites(self):
        """正文链路 4 个场景都必须打标（否则 by_scene 看不到正文调用量）。"""
        src = inspect.getsource(sh.generate_content)
        for scene in ("word_budget_alloc", "content_draft",
                      "content_continue", "content_shrink"):
            assert f'scene="{scene}"' in src, f"缺少场景标记: {scene}"

    def test_consistency_pipeline_sites(self):
        """全文一致性三阶段（扫描/仲裁/修复）分别打标 —— 评估 P0-1 的度量基准。"""
        assert 'scene="consistency_scan"' in inspect.getsource(cs.ai_scan_section)
        assert 'scene="consistency_arbitrate"' in inspect.getsource(ca._arbitrate_batch)
        assert 'scene="consistency_repair"' in inspect.getsource(ra.repair_section)

    def test_facts_extraction_site(self):
        assert 'scene="facts_extract"' in inspect.getsource(fe)

    def test_manual_shrink_site(self):
        """手动「压缩本章」与自动压缩同一场景（口径一致才能合并统计）。"""
        assert 'scene="content_shrink"' in inspect.getsource(sec)


class TestScenePersisted:
    async def test_scene_written_to_audit_log(self, db_conn, monkeypatch):
        """scene 必须真的落到 ai_audit_logs（/ai/stats by_scene 的数据源）。"""
        await _insert(db_conn)
        fake = _FakeProvider(chat_text="ok")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        await _call(pf, scene="content_draft")
        await pf.flush_audit_buffer()

        cur = await db_conn.execute(
            "SELECT scene FROM ai_audit_logs WHERE action='chat'")
        scenes = [r[0] for r in await cur.fetchall()]
        assert "content_draft" in scenes


# ============================================================
# P0-4：流式能力记忆
# ============================================================
class TestStreamCapabilityMemory:
    async def test_first_failure_still_probes_stream(self, db_conn, monkeypatch):
        """单次失败只回退、不记忆：偶发网络抖动不能把流式判死。"""
        await _insert(db_conn, request_mode="stream")
        fake = _FakeProvider(stream_err=RuntimeError("HTTP 400: stream not supported"),
                             chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        out = await _call(pf)
        assert out == "回退成功"
        assert fake.stream_calls == 1 and fake.chat_calls == 1
        assert pf._stream_disabled(pf._stream_capability_key(fake)) is False

    async def test_two_consecutive_failures_disable_stream(self, db_conn, monkeypatch):
        """连续失败达到阈值后，后续调用直接走普通请求（省掉必然失败的流式 HTTP）。"""
        await _insert(db_conn, request_mode="stream")
        fake = _FakeProvider(stream_err=RuntimeError("HTTP 400: stream not supported"),
                             chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        await _call(pf)
        await _call(pf)
        assert fake.stream_calls == 2 and fake.chat_calls == 2

        third = await _call(pf)
        assert third == "回退成功"
        # 第三次不再尝试流式：正文生成 N 章时这里就是被省下的 N-2 次 HTTP
        assert fake.stream_calls == 2 and fake.chat_calls == 3

    async def test_ttl_expiry_reprobes(self, db_conn, monkeypatch):
        """TTL 到期后重新探测 —— 绝不永久判死某个平台。"""
        await _insert(db_conn, request_mode="stream")
        monkeypatch.setattr(pf, "STREAM_CAPABILITY_TTL", 0.0)
        fake = _FakeProvider(stream_err=RuntimeError("boom"), chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        await _call(pf)
        await _call(pf)
        await _call(pf)
        assert fake.stream_calls == 3, "TTL=0 时每次都应重新探测流式"

    async def test_success_clears_memory(self, db_conn, monkeypatch):
        """流式成功后立即清除记忆（平台修复/换模型可自愈回到流式）。"""
        await _insert(db_conn, request_mode="stream")
        monkeypatch.setattr(pf, "STREAM_CAPABILITY_TTL", 0.0)
        fake = _FakeProvider(stream_err=RuntimeError("boom"), chat_text="x")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        await _call(pf)
        await _call(pf)
        assert fake.stream_calls == 2

        fake.stream_err = None
        fake.stream_pieces = ["成", "功"]
        assert await _call(pf) == "成功"
        assert fake.stream_calls == 3

        # 记忆已清除：再失败一次仍会先探测流式
        fake.stream_err = RuntimeError("boom again")
        fake.stream_pieces = []
        await _call(pf)
        assert fake.stream_calls == 4

    async def test_reset_stream_capability(self, db_conn, monkeypatch):
        """显式重置入口（测试隔离 / 配置变更后立即重探测）。"""
        await _insert(db_conn, request_mode="stream")
        fake = _FakeProvider(stream_err=RuntimeError("boom"), chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        await _call(pf)
        await _call(pf)
        assert fake.stream_calls == 2

        pf.reset_stream_capability()
        await _call(pf)
        assert fake.stream_calls == 3

    async def test_normal_mode_unaffected(self, db_conn, monkeypatch):
        """非流式配置完全不受能力记忆影响。"""
        await _insert(db_conn, request_mode="normal")
        fake = _FakeProvider(chat_text="普通结果")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)

        assert await _call(pf) == "普通结果"
        assert fake.stream_calls == 0 and fake.chat_calls == 1
