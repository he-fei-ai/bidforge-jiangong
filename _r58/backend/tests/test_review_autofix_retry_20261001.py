"""审核与预检 · 自动修复「失败重试一次」（2026-10-01）

方案《审核与预检模块 — 自动修复功能增强方案》阶段 4 要求：
「修复失败 → **重试 1 次** → 仍失败则跳过」。旧实现恒 1 次调用，
AI 正常返回但校验不合格即直接判 failed。本轮按 ``repair_agent`` 的既有
重试口径补齐（**不另立一套判据**），守护以下不变量：

1. **默认逐字兼容**：两个开关默认 False ⇒ 每章恒 1 次 AI 调用。
2. **异常恒重试**：AI 调用本身抛异常（超时/网络/空返回）是偶发故障而非
   模型能力问题，**不看开关**也重试一次。
3. **校验不合格按开关 + 严重度分级**：``auto`` 模式恒不重试（纯程序化，
   重试只是把同一段确定性代码再跑一遍）。
4. **重试成功即修复**：第 2 次通过 ⇒ 该章判 repaired 并进 pending 落库；
   两次都失败 ⇒ 判 failed 且**保留原文**（绝不写半成品）。
5. **不变量：最多 2 次调用** —— 无论开关怎么开都不许出现第 3 次。
"""
import pytest
from app.services import review_autofix
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-retry-1"
PROJ = "prj-retry-1"
SEC_A = "sec-retry-a"

_BEFORE = "本工程总工期为 120 日历天，混凝土强度等级为 C30。\n第二段说明施工部署。\n"
#: 校验必然不合格的返回：与原文归一化后完全相同（validate_fixed 判「问题未消除」）
_SAME = _BEFORE
#: 合格返回：统一工期口径，且与原文等长（不触发篇幅软告警）
_AFTER = _BEFORE.replace("120 日历天", "90 日历天")


async def _seed(db, *, content: str = _BEFORE) -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (SCHEME, PROJ, "深基坑专项方案"))
    wc = text_word_count(content)
    await db.execute(
        "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
        "word_status,word_budget,status,level,sort_order) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (SEC_A, SCHEME, PROJ, "工程概况", content, wc,
         word_status_for(wc, 1500), 1500, "generated", 1, 0))
    await db.commit()


def _sections(content: str = _BEFORE) -> list[dict]:
    return [{"id": SEC_A, "title": "工程概况", "content": content}]


def _con01_finding(severity: str = "high") -> dict:
    return {
        "rule_id": "CON-01", "dimension": "consistency", "severity": severity,
        "title": "全文一致性", "detail": "「工期」在正文中出现 2 种不同取值",
        "evidence": ["120日历天（工程概况）", "90日历天（施工进度计划）"],
        "section_id": "", "section_title": "", "suggestion": "请统一工期口径",
        "basis": "", "mode": "program",
    }


def _dlv05_finding(section_id: str = SEC_A) -> dict:
    return {
        "rule_id": "DLV-05", "dimension": "deliverability", "severity": "block",
        "title": "控制字符", "detail": "含控制字符", "evidence": [],
        "section_id": section_id, "section_title": "工程概况",
        "suggestion": "清除控制字符", "basis": "", "mode": "program",
    }


async def _run(db, monkeypatch, finding, *, replies, exc_at=None,
               sections=None):
    """跑一次 ``apply_fix``，返回 (结果, AI 实际被调用次数)。"""
    calls = {"n": 0}

    async def fake_chat(messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if exc_at is not None and i == exc_at:
            raise RuntimeError("模拟 AI 超时")
        return replies[min(i, len(replies) - 1)]

    monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
    res = await review_autofix.apply_fix(
        db, scheme_id=SCHEME, finding=finding,
        sections=sections if sections is not None else _sections(),
        scheme={"name": "深基坑专项方案", "type": "深基坑"})
    return res, calls["n"]


@pytest.fixture(autouse=True)
def _reset_retry_flags(monkeypatch):
    """每个用例从「默认关闭」出发，避免用例间串味。"""
    monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", False)
    monkeypatch.setattr(
        review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", False)


class TestShouldRetryOnInvalid:
    """纯函数判据（配置组合 × 严重度 × 模式）。"""

    def test_default_off_never_retries(self):
        for sev in ("block", "high", "medium", "low"):
            assert review_autofix.should_retry_on_invalid(
                _con01_finding(sev)) is False

    def test_global_switch_retries_any_severity(self, monkeypatch):
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        for sev in ("block", "high", "medium", "low"):
            assert review_autofix.should_retry_on_invalid(
                _con01_finding(sev)) is True

    def test_severity_switch_only_retries_block_and_high(self, monkeypatch):
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("block")) is True
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("high")) is True
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("medium")) is False
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("low")) is False

    def test_global_switch_wins_over_severity(self, monkeypatch):
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("low")) is True

    def test_auto_mode_never_retries_even_when_switch_on(self, monkeypatch):
        """程序化修复不调 AI，重试只是把同一段确定性代码再跑一遍。"""
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        assert review_autofix.should_retry_on_invalid(
            _dlv05_finding()) is False

    def test_manual_mode_never_retries(self, monkeypatch):
        """manual 规则不会进入改写链路，判据同样必须 fail-closed。"""
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        finding = dict(_dlv05_finding(), rule_id="DLV-01")
        assert review_autofix.should_retry_on_invalid(finding) is False

    def test_unknown_severity_does_not_retry_under_severity_switch(
            self, monkeypatch):
        """未知严重度必须 fail-closed 到「不重试」。"""
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("")) is False
        assert review_autofix.should_retry_on_invalid(
            _con01_finding("严重")) is False

    def test_retry_severities_are_valid_severity_values(self):
        """重试严重度白名单必须是 SEVERITY_ORDER 的真子集（防拼错永不命中）。"""
        from app.services.audit_rules import SEVERITY_ORDER
        assert review_autofix.AUTOFIX_RETRY_SEVERITIES
        assert set(review_autofix.AUTOFIX_RETRY_SEVERITIES) <= set(SEVERITY_ORDER)


class TestRetryBehaviour:
    """端到端：调用次数、状态、原文保护。"""

    async def test_default_is_single_call(self, db_conn, monkeypatch):
        """默认关闭 ⇒ 恒 1 次调用，判 failed（与引入前逐字一致）。"""
        await _seed(db_conn)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_SAME])
        assert n == 1
        assert res["status"] == "failed"
        assert res["pending"] == []

    async def test_exception_always_retries_even_when_switches_off(
            self, db_conn, monkeypatch):
        """AI 调用异常是偶发故障，不看开关也重试一次。"""
        await _seed(db_conn)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_AFTER], exc_at=0)
        assert n == 2
        assert res["status"] == "repaired", res["items"]
        assert res["stats"]["repaired"] == 1

    async def test_exception_then_invalid_still_failed(self, db_conn, monkeypatch):
        await _seed(db_conn)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_SAME], exc_at=0)
        # 第 1 次异常 → 重试；第 2 次返回不合格 → 判 failed，共 2 次
        assert n == 2
        assert res["status"] == "failed"
        assert res["pending"] == []

    async def test_retry_on_invalid_recovers_on_second_attempt(
            self, db_conn, monkeypatch):
        """开启开关后：第 1 次不合格、第 2 次合格 ⇒ repaired。"""
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_SAME, _AFTER])
        assert n == 2
        assert res["status"] == "repaired", res["items"]
        assert len(res["pending"]) == 1
        # ⚠️ 比对必须 rstrip：``_clean_output`` 会剥掉尾部换行（既有清洗行为），
        #    逐字相等断言会因一个尾换行假失败。
        assert res["pending"][0][2].rstrip() == _AFTER.rstrip()

    async def test_retry_twice_invalid_keeps_original(self, db_conn, monkeypatch):
        """两次都不合格 ⇒ failed，且 pending 为空（绝不写半成品）。"""
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_SAME])
        assert n == 2
        assert res["status"] == "failed"
        assert res["pending"] == []
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == _BEFORE

    async def test_never_exceeds_two_calls(self, db_conn, monkeypatch):
        """不变量：开关全开也不许出现第 3 次调用。"""
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        _res, n = await _run(db_conn, monkeypatch, _con01_finding("block"),
                             replies=[_SAME])
        assert n == 2

    async def test_medium_severity_does_not_retry_under_severity_switch(
            self, db_conn, monkeypatch):
        """分级重试只覆盖 block/high：medium 维持 1 次调用。"""
        await _seed(db_conn)
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        res, n = await _run(db_conn, monkeypatch, _con01_finding("medium"),
                            replies=[_SAME])
        assert n == 1
        assert res["status"] == "failed"

    async def test_block_severity_retries_under_severity_switch(
            self, db_conn, monkeypatch):
        await _seed(db_conn)
        monkeypatch.setattr(
            review_autofix, "AUTOFIX_RETRY_ON_INVALID_BY_SEVERITY", True)
        res, n = await _run(db_conn, monkeypatch, _con01_finding("block"),
                            replies=[_SAME, _AFTER])
        assert n == 2
        assert res["status"] == "repaired", res["items"]

    async def test_auto_mode_makes_exactly_one_pass(self, db_conn, monkeypatch):
        """程序化修复：即使全开关打开也只跑一次，且全程零 AI 调用。"""
        content = "正常\x07内容"
        await _seed(db_conn, content=content)
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        res, n = await _run(db_conn, monkeypatch, _dlv05_finding(),
                            replies=[_SAME], sections=_sections(content))
        assert n == 0
        assert res["status"] == "repaired"
        assert len(res["pending"]) == 1
        assert "\x07" not in res["pending"][0][2]

    async def test_retry_persists_via_router(self, db_conn, monkeypatch):
        """端到端走 routers 层：重试成功的正文必须真的落库 + 退回待审核。"""
        from app.routers import review_autofix as ra
        await _seed(db_conn)
        await db_conn.execute(
            "UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))
        await db_conn.commit()
        monkeypatch.setattr(review_autofix, "AUTOFIX_RETRY_ON_INVALID", True)
        res, n = await _run(db_conn, monkeypatch, _con01_finding(),
                            replies=[_SAME, _AFTER])
        assert n == 2 and res["status"] == "repaired"
        snapshot_id = await ra._persist_fixed(
            db_conn, scheme_id=SCHEME, rule_id="CON-01",
            pending=res["pending"], repair_id=res.get("repair_id", ""))
        assert snapshot_id
        cur = await db_conn.execute(
            "SELECT content, review_status FROM sections WHERE id=?", (SEC_A,))
        row = await cur.fetchone()
        assert "90 日历天" in row["content"] and "120 日历天" not in row["content"]
        assert row["review_status"] == "pending"


class TestConfigContract:
    """配置项默认值必须向后兼容（AGENTS.md §3.1.3）。"""

    def test_defaults_are_false(self):
        from app.config import Settings
        s = Settings()
        assert s.review_autofix_retry_on_invalid is False
        assert s.review_autofix_retry_on_invalid_by_severity is False

    def test_module_constants_are_config_driven(self):
        """模块常量必须由配置驱动，不能写死 True。"""
        import inspect
        src = inspect.getsource(review_autofix)
        assert 'getattr(settings, "review_autofix_retry_on_invalid"' in src
        assert ('getattr(settings, "review_autofix_retry_on_invalid_by_severity"'
                in src)


