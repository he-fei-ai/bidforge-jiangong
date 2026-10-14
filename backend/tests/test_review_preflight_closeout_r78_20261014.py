"""R78 · 审核与预检模块收口护栏（2026-10-14）。

锁定三处修复（详见 docs/审核与预检模块收口_修复落地与验证_R78_20261014.md）：

F-1 ``compliance.run_consistency_audit``：consistency_audit INSERT 的**列序与
    参数序必须逐位对应**。旧实现（05e1380 加 content_fingerprint 列时）把指纹
    参数插到 score 之前 → score 列落指纹串、content_fingerprint 列落分值，
    连带三处后果：总检一致性来源**永远判 stale 跳过**、/latest 返回坏 score、
    /history 同污染。A 组 = 静态列参对应锁 + 真库行为实证 + 消费端 stale 判据。

F-2 ``compliance._readiness_overview_compute``：P1-4（2026-10-12）新增的
    ``fingerprint`` 形参必须**被复用** —— 旧实现被无条件重算覆盖，docstring
    承诺的「同一请求只扫一次库、缓存键与落库指纹同源」从未兑现。
    B 组 = AST 锁重算必须在 ``if fingerprint is None:`` 守卫内 + 传参形态
    零重算行为实证 + 缺省形态自行计算行为实证。

F-3 ``routers/consistency_repair.py`` 收口 R13 单一出口（此前是
    ``test_review_r13_closeout_20261006.py`` TARGET_FILES 三 router 之外的
    漏网之鱼）。C 组 = 静态禁裸 db.execute/executemany + review_db 单独行
    import 契约 + None 游标读→503 行为实证 + 「写没生效不得汇报假成功」
    （exec_write 严格模式）与 require_rows=False 宽松模式两条行为实证。
    D 组 = rollback 的冲突状态回写必须带 ``AND scheme_id=`` 约束（静态 +
    跨方案同名历史行行为实证）。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import uuid

import pytest
from fastapi import HTTPException

from app.routers import compliance as cc
from app.routers import consistency_repair as cr
from app.services import facts_builder, repair_record

# pytest.ini asyncio_mode = auto —— 不设全局 asyncio mark（同步静态护栏用例会被误标）


# ===========================================================================
# helpers
# ===========================================================================
async def _seed_scheme(db, *, pid=None, sid=None, name="深基坑支护专项方案",
                       with_section: bool = True) -> tuple[str, str]:
    pid = pid or uuid.uuid4().hex
    sid = sid or uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget)"
        " VALUES(?,?,?,?,0)", (sid, pid, name, "目录已确认"))
    if with_section:
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " description, level, status, word_count, word_budget, content,"
            " review_status, sort_order)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, pid, "", "第一章", "", 1, "generated",
             20, 0, "本工程总工期为 120 日历天。", "", 0))
    await db.commit()
    return pid, sid


def _module_tree(fn) -> ast.AST:
    """装饰器 + def 的 getsource 本身就是合法模块级源码，直接 parse。"""
    return ast.parse(inspect.getsource(fn))


def _call_nodes(tree: ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _attr_root_name(func: ast.expr) -> str:
    """a.b.c → 'a.b'；c → 'c'。"""
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# ===========================================================================
# A 组 · F-1 一致性审计 INSERT 列参对应
# ===========================================================================
class TestConsistencyAuditInsertColumnParamParity:
    def test_insert_column_param_parity_static(self):
        """静态锁：INSERT 列名与参数元祖**逐位**语义对应。"""
        tree = _module_tree(cc.run_consistency_audit)
        target = None
        for call in _call_nodes(tree):
            if _attr_root_name(call.func) != "review_db.exec_write":
                continue
            sql = ast.unparse(call.args[1]) if len(call.args) > 1 else ""
            if "INSERT INTO consistency_audit" in sql:
                target = call
                break
        assert target is not None, "run_consistency_audit 必须经 review_db.exec_write 写审计结果"
        sql = ast.literal_eval(target.args[1])
        m = re.search(r"INSERT INTO consistency_audit \(([^)]*)\)\s*VALUES \(([^)]*)\)",
                      sql, re.S)
        assert m, "INSERT 必须是显式列清单形态"
        cols = [c.strip() for c in m.group(1).split(",")]
        placeholders = [p.strip() for p in m.group(2).split(",")]
        assert len(cols) == len(placeholders) == len(target.args[2].elts), (
            f"列数/占位符数/参数数必须一致: {cols} vs {placeholders}")
        pairs = dict(zip(cols, [ast.unparse(e) for e in target.args[2].elts]))
        assert "score_float" in pairs["score"], pairs["score"]
        assert "_content_fingerprint" in pairs["content_fingerprint"], (
            pairs["content_fingerprint"])
        assert "json.dumps" in pairs["issues"], pairs["issues"]

    async def test_written_row_score_and_fingerprint_correct(self, db_conn,
                                                             monkeypatch):
        """行为实证：端点落库后 score 列是分值、content_fingerprint 是指纹。"""
        async def fake_collect(*args, **kwargs):
            return ({"score": 88.5,
                     "issues": [{"dimension": "工期", "severity": "high",
                                 "fact": "90 日历天",
                                 "content_quote": "120 日历天"}]}, "raw")

        async def fake_facts(db_, scheme_id_, **kwargs):
            return "本工程建设工期为 90 日历天"

        monkeypatch.setattr(cc, "collect_json_response", fake_collect)
        monkeypatch.setattr(facts_builder, "build_facts_text", fake_facts)
        _, sid = await _seed_scheme(db_conn)

        out = await cc.run_consistency_audit(sid, db=db_conn)
        assert out["score"] == 88.5

        cur = await db_conn.execute(
            "SELECT score, content_fingerprint, issues FROM consistency_audit"
            " WHERE scheme_id=?", (sid,))
        row = dict(zip(("score", "content_fingerprint", "issues"),
                       tuple(await cur.fetchone())))
        expect_fp = await cc._content_fingerprint(db_conn, sid)
        assert float(row["score"]) == 88.5, "score 列必须落分值（旧实现落指纹串）"
        assert row["content_fingerprint"] == expect_fp, (
            "content_fingerprint 列必须落当前正文指纹（旧实现落分值）")
        assert json.loads(row["issues"])

        latest = await cc.latest_consistency_audit(sid, db=db_conn)
        assert float(latest["score"]) == 88.5
        assert latest["exists"] is True

    async def test_overview_consistency_source_not_stale(self, db_conn,
                                                          monkeypatch):
        """消费端锁：刚写入的审计行在总检中**不得**被判 stale（旧实现恒 stale
        → consistency 来源永久漏报 + stale_ai_sources 恒误报）。"""
        async def fake_collect(*args, **kwargs):
            return ({"score": 90.0, "issues": []}, "raw")

        async def fake_facts(db_, scheme_id_, **kwargs):
            return "FACTS"

        monkeypatch.setattr(cc, "collect_json_response", fake_collect)
        monkeypatch.setattr(facts_builder, "build_facts_text", fake_facts)
        _, sid = await _seed_scheme(db_conn)
        await cc.run_consistency_audit(sid, db=db_conn)

        payload = await cc._readiness_overview_compute(db_conn, sid,
                                                       persist=False)
        assert "consistency" in payload["sources"], (
            f"新鲜审计行必须计入总检来源: {payload['sources']}")
        assert "consistency" not in payload["stale_ai_sources"]
        assert "consistency" not in payload["failed_sources"]

    def test_ai_row_is_stale_both_forms(self):
        """判据本体：同指纹不过期、异指纹过期、历史无指纹 fail-open。"""
        fp = "a" * 32
        assert cc._ai_row_is_stale({"content_fingerprint": fp}, fp) is False
        assert cc._ai_row_is_stale({"content_fingerprint": "b" * 32}, fp) is True
        assert cc._ai_row_is_stale({"content_fingerprint": ""}, fp) is False
        # 错位形态（分值落进指纹列）必须判过期 —— F-1 旧 bug 的指纹列样本
        assert cc._ai_row_is_stale({"content_fingerprint": "90.0"}, fp) is True


# ===========================================================================
# B 组 · F-2 fingerprint 形参复用（P1-4 契约兑现）
# ===========================================================================
class TestOverviewFingerprintReuse:
    def test_recompute_guarded_by_none_check(self):
        """静态锁：compute 内对 _content_fingerprint 的赋值必须位于
        ``if fingerprint is None:`` 守卫之下，不得有无条件重算。"""
        tree = _module_tree(cc._readiness_overview_compute)
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and ast.unparse(node.test).strip() \
                    == "fingerprint is None":
                for inner in ast.walk(node):
                    guarded.add(id(inner))
        offenders = []
        for call in _call_nodes(tree):
            if "_content_fingerprint" not in _attr_root_name(call.func):
                continue
            # 找到直接包含该调用的赋值（旧实现形态：无条件 fingerprint = await ...）
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.NamedExpr)):
                    if any(sub is call for sub in ast.walk(node)) \
                            and id(node) not in guarded:
                        offenders.append(ast.unparse(node))
        # 末尾 payload 里的三元回退（else await ...）不是重算路径的独立赋值，
        # 它只在 fingerprint is None 时才可达，由 B2/B3 行为实证保护。
        offenders = [o for o in offenders
                     if not o.lstrip().startswith("'content_fingerprint'")]
        assert not offenders, f"存在未被 None 守卫的重算赋值: {offenders}"

    async def test_passed_fingerprint_never_recomputed(self, db_conn,
                                                       monkeypatch):
        """行为实证：调用方传入指纹时，compute 全链路**一次都不**重算。"""
        _, sid = await _seed_scheme(db_conn)
        real_fp = await cc._content_fingerprint(db_conn, sid)

        def boom(*args, **kwargs):
            raise AssertionError("P1-4：传入 fingerprint 后不得再重算（旧实现覆盖参数）")

        monkeypatch.setattr(cc, "_content_fingerprint", boom)
        payload = await cc._readiness_overview_compute(
            db_conn, sid, persist=False, fingerprint=real_fp)
        assert payload["content_fingerprint"] == real_fp

    async def test_default_fingerprint_computed_once(self, db_conn,
                                                     monkeypatch):
        """缺省形态（review_autofix 三处调用方）：自行计算，行为逐字不变。"""
        _, sid = await _seed_scheme(db_conn)
        real_fp = await cc._content_fingerprint(db_conn, sid)
        calls: list[str] = []
        orig = cc._content_fingerprint

        async def spy(db_, scheme_id_):
            calls.append(scheme_id_)
            return await orig(db_, scheme_id_)

        monkeypatch.setattr(cc, "_content_fingerprint", spy)
        payload = await cc._readiness_overview_compute(db_conn, sid,
                                                       persist=False)
        assert payload["content_fingerprint"] == real_fp
        assert len(calls) == 1, f"缺省形态只允许算一次，实际 {len(calls)} 次"


# ===========================================================================
# C 组 · F-3 consistency_repair 收口 R13 单一出口
# ===========================================================================
class _NoneCursorDB:
    """模拟 R13 事故形态：db.execute 返回 None（不是异常）。"""

    async def execute(self, *args, **kwargs):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class TestConsistencyRepairReviewDbSingleSource:
    def test_no_raw_db_execute(self):
        """静态扫描：模块内不得出现 db.execute / db.executemany 裸调用
        （db.commit 允许 —— exec_write 不自动提交，与三个已收口 router 同构）。"""
        tree = ast.parse(inspect.getsource(cr))
        offenders = []
        for call in _call_nodes(tree):
            if not isinstance(call.func, ast.Attribute):
                continue
            if _attr_root_name(call.func) in ("db.execute", "db.executemany"):
                offenders.append(f"L{call.lineno}: {_attr_root_name(call.func)}")
        assert not offenders, f"consistency_repair 退回裸调用: {offenders}"

    def test_review_db_imported_on_own_line(self):
        """R13 契约形态：``from app.services import review_db`` 必须单独一行。"""
        tree = ast.parse(inspect.getsource(cr))
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module == "app.services"
                and [a.name for a in n.names] == ["review_db"]]
        assert len(hits) == 1, "review_db 必须以单独一行 ImportFrom 引入"

    async def test_read_with_none_cursor_is_503_not_500(self):
        """行为实证：None 游标读 → 503（旧实现直接 AttributeError 500）。"""
        with pytest.raises(HTTPException) as exc:
            await cr.get_conflicts("sch-x", scan_id="", db=_NoneCursorDB())
        assert exc.value.status_code == 503

    async def test_confirm_restore_write_not_effective_is_503(self, db_conn):
        """行为实证（假成功路径）：拒绝冲突 → 恢复正文 UPDATE 命中 0 行
        （章节行不存在）→ 必须 503，**不得**返回 confirmed 假成功。
        旧实现裸 db.execute 不校验 rowcount，照常返回 200。"""
        _, sid = await _seed_scheme(db_conn, with_section=False)
        snap = await repair_record.create_snapshot(
            db_conn, sid, [{"section_id": "S-GONE", "content_before": "旧文"}])
        rep = await repair_record.save_repair(
            db_conn, scheme_id=sid, scan_id="S1", mode="auto",
            items=[{"conflict_id": "C1", "section_id": "S-GONE",
                    "status": "repaired"}],
            snapshot_id=snap, total_conflicts=1,
            stats={"repaired": 1, "failed": 0, "skipped": 0})
        with pytest.raises(HTTPException) as exc:
            await cr.confirm(
                scheme_id=sid,
                body={"repair_id": rep["repair_id"], "accepted": [],
                      "rejected": ["C1"]},
                db=db_conn)
        assert exc.value.status_code == 503

    async def test_confirm_status_write_allows_zero_rows(self, db_conn):
        """宽松模式锁：accepted 的 cid 来自请求体、行可以不存在 →
        require_rows=False，不得误伤成 503。"""
        _, sid = await _seed_scheme(db_conn, with_section=False)
        rep = await repair_record.save_repair(
            db_conn, scheme_id=sid, scan_id="S1", mode="auto", items=[],
            snapshot_id="", total_conflicts=0,
            stats={"repaired": 0, "failed": 0, "skipped": 0})
        out = await cr.confirm(
            scheme_id=sid,
            body={"repair_id": rep["repair_id"], "accepted": ["C-ABSENT"],
                  "rejected": []},
            db=db_conn)
        assert out["status"] == "confirmed"


# ===========================================================================
# D 组 · rollback 冲突回写 scheme 作用域加固
# ===========================================================================
class TestRollbackSchemeScoped:
    def test_rollback_update_scoped_by_scheme(self):
        """静态锁：回滚的 UPDATE consistency_conflicts 必须带 scheme 约束。"""
        tree = _module_tree(cr.rollback)
        sqls = []
        for call in _call_nodes(tree):
            if _attr_root_name(call.func) != "review_db.exec_write":
                continue
            sql = ast.unparse(call.args[1])
            if "UPDATE consistency_conflicts" in sql:
                sqls.append(sql)
        assert sqls, "rollback 必须存在冲突状态回写"
        for sql in sqls:
            assert "scheme_id=" in sql, f"回滚 UPDATE 缺 scheme 约束: {sql}"

    async def test_rollback_does_not_touch_other_scheme(self, db_conn):
        """行为实证：历史无前缀冲突行 C001 现归属方案 B（P0 upsert 收回），
        回滚方案 A 的旧批次不得把 B 的行打回 pending。"""
        _, sid_a = await _seed_scheme(db_conn, with_section=False)
        _, sid_b = await _seed_scheme(db_conn, with_section=False)
        await db_conn.execute(
            "INSERT INTO consistency_conflicts (id, scheme_id, scan_id,"
            " conflict_type, severity, topic, occurrences, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            ("C001", sid_b, "scanB", "numeric", "high", "工期", "[]",
             "repaired"))
        snap = await repair_record.create_snapshot(db_conn, sid_a, [])
        await repair_record.save_repair(
            db_conn, scheme_id=sid_a, scan_id="scanA", mode="auto",
            items=[{"conflict_id": "C001", "section_id": "S1",
                    "status": "repaired"}],
            snapshot_id=snap, total_conflicts=1,
            stats={"repaired": 1, "failed": 0, "skipped": 0})
        await db_conn.commit()

        out = await cr.rollback(sid_a, {"snapshot_id": snap}, db=db_conn)
        assert out["status"] == "rolled_back"
        cur = await db_conn.execute(
            "SELECT status FROM consistency_conflicts WHERE id='C001'")
        assert (await cur.fetchone())[0] == "repaired", (
            "跨方案同名历史行被回滚误伤（旧实现 UPDATE 不带 scheme 约束）")


# ===========================================================================
# E 组 · 本模块触碰文件的 INSERT 列/占位符 parity 通检（F-1 同族防再发）
# ===========================================================================
class TestInsertColumnPlaceholderParity:
    def test_all_literal_inserts_parity(self):
        """两触碰文件（consistency_repair 收口后无自身 INSERT，计数允许为 0，
        由 compliance 保证扫描非空转）的全部字面 INSERT：列数 == 占位符数。"""
        checked = 0
        for module in (cc, cr):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)):
                    continue
                m = re.search(
                    r"INSERT INTO \w+ ?\(([^)]*)\)\s*VALUES ?\(([^)]*)\)",
                    node.value)
                if not m:
                    continue
                cols = [c.strip() for c in m.group(1).split(",") if c.strip()]
                ph = [p.strip() for p in m.group(2).split(",") if p.strip()]
                assert all(p == "?" for p in ph), (
                    f"非纯 ? 占位的 INSERT 请人工核对: {node.value[:80]}")
                assert len(cols) == len(ph), (
                    f"列数 {len(cols)} != 占位符数 {len(ph)}:"
                    f" {node.value[:120]}")
                checked += 1
        assert checked > 0, "扫描必须实际命中 INSERT（防护栏空转）"
