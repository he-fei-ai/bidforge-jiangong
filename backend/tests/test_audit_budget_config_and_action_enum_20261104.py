"""D-41 预算配置化 + ai_audit_logs action 枚举兼容护栏（2026-11-04）。

锁三件事：

1. ``compliance_max_batches`` / ``compliance_max_chars`` 默认值与 R176 逐字相同
   （40 / 240000），且 compliance.py **从 settings 读取**——未设置时行为不变；
2. 取值为 0 / 负数时视为「该维度无上限」并记 WARNING（不拒绝启动）；
   同时验证行为确实不限：两批照跑、partial=false。
3. 新增 ai_audit_logs action 必须同步更新 ``KNOWN_AUDIT_ACTIONS``，否则本文件红：
   - ``_log_audit(..., "<新值>", ...)`` 的 AST 扫描会失败；
   - 新增直写 ``INSERT INTO ai_audit_logs`` 的文件会让「写入方收敛」护栏失败。
"""

import ast
import logging
import pathlib
import re
import uuid

import app.db as _appdb
import pytest
from app.config import Settings, settings
from app.db import close_db, get_conn, init_db
from app.models import ComplianceCheckIn
from app.routers import compliance as compliance_mod

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: ai_audit_logs.action 的**封闭白名单**（新增 action 必须同步本集合，否则测试红）。
KNOWN_AUDIT_ACTIONS = {
    "chat",
    "circuit_skipped",
    "provider_disabled",
    compliance_mod.COMPLIANCE_BATCH_ACTION,
}


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, sid, pid
    await close_db()


async def _seed_sections(db, sid: str, pid: str, items: list) -> None:
    for so, (title, content) in enumerate(items):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " level, sort_order, status, content, word_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, pid, "", title, 1, so, "done", content, len(content)),
        )
    await db.commit()


def _two_batch_sections() -> list:
    """两章各 5000 字 → 按 6000 装箱 = 两批（每批一章）。"""
    return [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)]


class TestBudgetConfig:
    def test_defaults_identical_to_r176(self):
        """默认值必须与 R176 的模块常量逐字相同（40 批 / 240000 字）。"""
        fields = Settings.model_fields
        assert fields["compliance_max_batches"].default == 40
        assert fields["compliance_max_chars"].default == 240000

    def test_compliance_reads_settings_not_module_constants(self):
        """compliance.py 不得再自持预算常量；读取唯一入口是 _batch_budget。"""
        src = (APP_DIR / "routers" / "compliance.py").read_text(encoding="utf-8")
        assert "COMPLIANCE_MAX_BATCHES =" not in src
        assert "COMPLIANCE_MAX_CHARS =" not in src
        assert 'settings, "compliance_max_batches"' in src
        assert 'settings, "compliance_max_chars"' in src
        assert "def _batch_budget(" in src

    async def test_unset_keeps_single_batch_behavior(self, ctx, monkeypatch):
        """未改配置时，小目录仍是 1 批（与 R176 行为一致）。"""
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, [("第一章", "短正文" * 100)])
        calls: list[int] = []

        async def _fake_collect(_m, _v, **_kw):
            calls.append(1)
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)
        assert out["batch_count"] == out["executed_batch_count"] == len(calls) == 1
        assert out["partial"] is False
        assert out["budget"] == {
            "max_batches": settings.compliance_max_batches,
            "max_chars": settings.compliance_max_chars,
        }

    @pytest.mark.parametrize("bad", [0, -1, -100])
    async def test_non_positive_means_unlimited_with_warning(self, ctx, monkeypatch, caplog, bad):
        """<=0 视为无上限：不拒绝启动、两批照跑，并记一次 WARNING。"""
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, _two_batch_sections())
        monkeypatch.setattr(settings, "compliance_max_batches", bad)
        monkeypatch.setattr(settings, "compliance_max_chars", bad)
        monkeypatch.setattr(compliance_mod, "_BUDGET_WARNED", False)
        calls: list[str] = []

        async def _fake_collect(messages, _v, **_kw):
            calls.append(messages[0]["content"])
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        with caplog.at_level(logging.WARNING, logger="compliance"):
            out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)

        assert out["batch_count"] == out["executed_batch_count"] == len(calls) == 2
        assert out["partial"] is False
        assert out["truncated_sections"] == []
        assert out["budget"] == {"max_batches": bad, "max_chars": bad}
        assert "无上限" in caplog.text


class TestAuditActionEnumCompatibility:
    def test_all_audit_action_literals_are_whitelisted(self):
        """AST 扫描 _log_audit 的 action 字面量 + 合规批次常量，必须恰为白名单。"""
        discovered = set()
        for path in APP_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_log_audit"
                ):
                    continue
                if (
                    len(node.args) >= 3
                    and isinstance(node.args[2], ast.Constant)
                    and isinstance(node.args[2].value, str)
                ):
                    discovered.add(node.args[2].value)
                for kw in node.keywords:
                    if kw.arg == "action" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        discovered.add(kw.value.value)
        assert KNOWN_AUDIT_ACTIONS == (discovered | {compliance_mod.COMPLIANCE_BATCH_ACTION}), (
            "ai_audit_logs 新增 action 未同步白名单："
            f"代码里发现 {sorted(discovered)}，白名单 {sorted(KNOWN_AUDIT_ACTIONS)}"
        )

    #: 「AI 调用侧」写入方：一条模型调用的遥测（厂商/模型/token/耗时/尝试序列）。
    AI_CALL_WRITERS = {"provider_factory.py", "compliance.py"}
    #: 「状态迁移流水」写入方：任务/阶段状态机借同一张表存迁移审计，**不是**调用遥测。
    STATE_AUDIT_WRITERS = {"state_machine.py"}
    #: 每个登记写入方的审计类别。未登记的写入方一律红 —— 不允许「加个文件名就过」。
    #:
    #: ⚠️ ``state_machine.py`` 的行 ``success`` **恒为 1**，仅表示「迁移动作已落库」，
    #: **不表示调用成功**。不得按 ``success`` 统计成功率（会得到永远 100% 的数字）；
    #: 真实迁移结果看 ``attempts_json`` 里的 ``to`` 与 ``error`` 列。
    AUDIT_WRITER_CLASS = {
        "provider_factory.py": "ai_call",        # AI 调用遥测（漏斗 SQL 唯一自持方）
        "compliance.py": "ai_batch",             # AI 批次遥测（复用上一家的 SQL 常量）
        "state_machine.py": "state_transition",  # 任务/阶段迁移流水
    }

    @staticmethod
    def _flat(path: pathlib.Path) -> str:
        return "".join(path.read_text(encoding="utf-8").split())

    @staticmethod
    def _writer_path(name: str) -> pathlib.Path:
        return next(p for p in APP_DIR.rglob(name))

    def test_audit_writer_funnel_is_closed(self):
        """ai_audit_logs 写入方必须逐一登记，未登记的新文件直接红。

        判据 = 「自持 INSERT INTO ai_audit_logs」或「从 provider_factory 导入该组降级
        SQL 常量」。登记表按**审计类别**区分「AI 调用遥测」与「状态迁移流水」，
        不再是一个扁平的文件名白名单。
        """
        hits = set()
        for path in APP_DIR.rglob("*.py"):
            flat = self._flat(path)
            # 同名常量在 audit_service.py 复用于**另一张表**（ai_config_audit_logs），
            # 故只认「自持 ai_audit_logs INSERT SQL」或「从 provider_factory 导入该组常量」。
            if "INSERTINTOai_audit_logs" in flat or (
                "ai.provider_factoryimport" in flat and "_AUDIT_INSERT_FULL" in flat
            ):
                hits.add(path.name)
        registered = self.AI_CALL_WRITERS | self.STATE_AUDIT_WRITERS
        assert hits == registered, (
            f"ai_audit_logs 出现未登记写入方 {sorted(hits - registered)}；"
            "新写入方必须先登记审计类别（ai_call / ai_batch / state_transition）"
            f"并声明 action 前缀/常量。当前登记 {sorted(registered)}"
        )
        assert set(self.AUDIT_WRITER_CLASS) == registered, "登记类别表与写入方集合不一致"

    def test_each_registered_writer_declares_its_audit_class(self):
        """每个登记写入方都必须有可被静态读取的类别声明与 action 约束。

        - 漏斗 SQL 只能由 provider_factory 自持，其余登记方不得自抄 INSERT；
        - state_machine 只能有**一个**写入口，action 必须带模块级声明的前缀。
        """
        for name, kind in self.AUDIT_WRITER_CLASS.items():
            assert kind in {"ai_call", "ai_batch", "state_transition"}, name

        # ① 除 provider_factory 外，任何登记方不得自持 ai_audit_logs 的 INSERT SQL
        for name in self.AI_CALL_WRITERS - {"provider_factory.py"}:
            assert self._flat(self._writer_path(name)).count("INSERTINTOai_audit_logs") == 0, (
                f"{name} 不得自抄 INSERT SQL，必须复用 provider_factory 的 _AUDIT_INSERT_*"
            )
        provider = self._flat(self._writer_path("provider_factory.py"))
        assert "INSERTINTOai_audit_logs" in provider, "provider_factory 必须自持漏斗 SQL"

        # ② 状态迁移写入方：单写入口 + action 前缀声明
        sm_path = self._writer_path("state_machine.py")
        sm_text = sm_path.read_text(encoding="utf-8")
        assert self._flat(sm_path).count("INSERTINTOai_audit_logs") == 1, (
            "state_machine.py 只允许一个 ai_audit_logs 写入口（_audit）"
        )
        m = re.search(r'_AUDIT_ACTION_PREFIX\s*=\s*"([^"]+)"', sm_text)
        assert m, "state_machine.py 必须用模块级 _AUDIT_ACTION_PREFIX 声明 action 前缀"
        prefix = m.group(1)
        assert prefix == "agent_", f"迁移审计 action 前缀应为 agent_，实为 {prefix!r}"

        calls = [
            n
            for n in ast.walk(ast.parse(sm_text))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_audit"
        ]
        assert calls, "未找到 _audit(...) 调用，前缀校验失效"
        for n in calls:
            assert len(n.args) >= 2, "_audit 的第二个位置参数必须是 action"
            arg = n.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                assert arg.value.startswith(prefix), f"action {arg.value!r} 未带前缀 {prefix!r}"
            elif isinstance(arg, ast.JoinedStr):
                head = arg.values[0] if arg.values else None
                assert (
                    isinstance(head, ast.Constant) and str(head.value).startswith(prefix)
                ), f"f-string action 首个片段必须带前缀 {prefix!r}"
            else:
                raise AssertionError("action 必须是字面量或带固定前缀的 f-string（否则护栏不可静态校验）")

    def test_state_audit_success_column_means_persisted_not_call_ok(self):
        """``success`` 恒为 1 的语义必须在代码里写明，且写入值确实写死 1。

        缺这条说明，后来者会按 success 算迁移成功率，得到一个永远 100% 的数字。
        """
        sm_path = self._writer_path("state_machine.py")
        sm_text = sm_path.read_text(encoding="utf-8")
        assert "不是 AI 调用审计" in sm_text, "_audit docstring 必须注明写的是迁移审计"
        assert "迁移动作已落库" in sm_text, "必须写明 success=1 表示「迁移动作已落库」"

        node = next(
            n
            for n in ast.walk(ast.parse(sm_text))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "execute"
            and isinstance(n.args[0], ast.Constant)
            and "INSERT INTO ai_audit_logs" in n.args[0].value
            and isinstance(n.args[1], ast.Tuple)
        )
        cols = re.search(r"ai_audit_logs\s*\((.*?)\)\s*VALUES", node.args[0].value, re.S).group(1)
        idx = [c.strip() for c in cols.split(",")].index("success")
        literal = node.args[1].elts[idx]
        assert isinstance(literal, ast.Constant) and literal.value == 1, (
            "state_machine 的 success 必须写死 1（表示迁移落库，不是调用成败）"
        )

    def test_stats_consumers_tolerate_unknown_actions(self):
        """统计/明细消费方不得对 action 做严格枚举（未知值必须安全共存）。"""
        usage = (APP_DIR / "routers" / "ai_config" / "usage.py").read_text(encoding="utf-8")
        # by_action 走 GROUP BY（未知 action 进桶而不是报错/丢弃）
        assert "GROUP BY action" in usage
        # 筛选下拉走 DISTINCT（新 action 自动可选）
        assert "SELECT DISTINCT action FROM ai_audit_logs" in usage
        # 严禁把 by_action 改成封闭白名单（新 action 会被静默隐藏）
        assert "action IN (" not in usage
        # 其余聚合只看真实调用，新增信封行不污染成功率/token
        system = (APP_DIR / "routers" / "system.py").read_text(encoding="utf-8")
        assert "action='chat'" in system


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
