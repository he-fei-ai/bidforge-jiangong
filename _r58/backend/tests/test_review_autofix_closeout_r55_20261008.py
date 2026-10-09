# -*- coding: utf-8 -*-
"""R55 · 审核与预检模块收口护栏（2026-10-08）

锁定本轮 5 处修复的行为与接线：

F1 批量路径章节上限（services/review_autofix.stage_fixes）
    旧实现批量入口完全不受 ``AUTOFIX_MAX_SECTIONS`` 约束、且 ``stats.skipped``
    硬编码 0（把「没截断」伪装成「截断后无跳过」）。现与单条路径同判据
    （问题最多的章节优先），超出章节记入 ``skipped_sections`` 并如实计入。

F2a /confirm 条目粒度（routers/review_autofix.confirm）
    accept/reject 新增复合键 ``"rule_id|section_id"``（同规则跨章可只接受
    单章）；裸 ``rule_id`` 旧语义**逐字不变**（向后兼容）。

F2b 非前缀检测在生产路径复活（同端点）
    旧实现把 by_section 预过滤成「只剩被接受项」再喂 ``_merged_after_if_prefix``
    —— 非前缀分支永不触发，接受 [A,B,C] 中的 {C} 时直接取 C.after（链式累积、
    含被用户拒绝的 B 的改写）→ 越权写入。现喂完整 repaired 链，非前缀 →
    重新链式改写（只对**当前库内正文**应用被接受项）。

F3/F4/F5 前后端接线（源码级 parity 锁，R54 同族做法 —— 本机 Node 缺失，
    vitest/tsc 无法本地执行，用 Python 读源文件锁符号在位 / 旧符号不回流）。

R56 追加：F6 前端源码**字节**体检（补丁把转义展开成裸 CR/LF 的实况回归锁）、
    以及 I 组「装了 Node 就真跑 tsc + vitest」的自动闭环（无 Node 时 skip）。

R57 追加：I 组的 Node 解析改为**三级出口**（环境变量 > PATH > 随其它工具一起装好
    的自带运行时），真跑范围扩到 4 个前端测试文件并新增 eslint 零 error 锁 —— R57
    正是靠这条通道首次跑通全量前端测试，抓到 4 处「写了从没跑过」的缺陷。
"""
import glob
import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import repair_record, review_autofix
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-r55-1"
PROJ = "prj-r55-1"
SEC_A = "sec-r55-a"
SEC_B = "sec-r55-b"

_CTRL = "正常\x07内容"
_FENCE = "正常内容\n```mermaid\ngraph TD; A-->B\n"
_BOTH = "正常\x07内容\n```mermaid\ngraph TD; A-->B\n"


def _seeded_sections():
    return [
        {"id": SEC_A, "title": "工程概况", "content": _CTRL},
        {"id": SEC_B, "title": "施工进度计划", "content": _FENCE},
    ]


async def _seed(db, a_content: str = _CTRL) -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (SCHEME, PROJ, "深基坑专项方案"))
    for sid, title, content, order in (
            (SEC_A, "工程概况", a_content, 0),
            (SEC_B, "施工进度计划", _FENCE, 1)):
        wc = text_word_count(content)
        await db.execute(
            "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
            "word_status,word_budget,status,level,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, SCHEME, PROJ, title, content, wc,
             word_status_for(wc, 1500), 1500, "generated", 1, order))
    await db.commit()


def _dlv05(sec: str) -> dict:
    return {"rule_id": "DLV-05", "dimension": "deliverability", "severity": "block",
            "title": "控制字符", "detail": "含控制字符", "evidence": [],
            "section_id": sec, "section_title": "工程概况",
            "suggestion": "清除控制字符", "basis": "", "mode": "program"}


def _dlv07(sec: str) -> dict:
    return {"rule_id": "DLV-07", "dimension": "deliverability", "severity": "block",
            "title": "未闭合围栏", "detail": "代码围栏未闭合", "evidence": [],
            "section_id": sec, "section_title": "施工进度计划",
            "suggestion": "补齐结束围栏", "basis": "", "mode": "program"}


async def _content(db, sid: str) -> str:
    cur = await db.execute("SELECT content FROM sections WHERE id=?", (sid,))
    return (await cur.fetchone())["content"]


# ===========================================================================
# F1 · 批量路径章节上限（服务层直接调用，不经总检重算）
# ===========================================================================
class TestStageSectionCap:

    async def test_cap_keeps_most_findings_first_and_records_skipped(self, db_conn, monkeypatch):
        """cap=1：SEC_B（2 条）保留、SEC_A（1 条）截断 —— 判据与单条路径一致。"""
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_MAX_SECTIONS", 1)
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME,
            findings=[_dlv05(SEC_A), _dlv05(SEC_B), _dlv07(SEC_B)],
            sections=_seeded_sections(), scheme={"name": "S", "type": "深基坑"})
        assert res["max_sections"] == 1
        assert [i["section_id"] for i in res["items"]] == [SEC_B, SEC_B]
        assert res["skipped_sections"] == [{
            "section_id": SEC_A, "section_title": "工程概况",
            "finding_count": 1, "rule_ids": ["DLV-05"]}]
        # stats.skipped 必须如实 = 截断章节数（旧实现硬编码 0）
        assert res["stats"]["skipped"] == 1

    async def test_cap_zero_means_unlimited(self, db_conn, monkeypatch):
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_MAX_SECTIONS", 0)
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME,
            findings=[_dlv05(SEC_A), _dlv05(SEC_B), _dlv07(SEC_B)],
            sections=_seeded_sections(), scheme={"name": "S", "type": "深基坑"})
        assert res["skipped_sections"] == []
        assert res["stats"]["skipped"] == 0
        assert res["max_sections"] == 0
        assert {i["section_id"] for i in res["items"]} == {SEC_A, SEC_B}

    async def test_below_cap_contract_keys_present(self, db_conn, monkeypatch):
        """未触顶：skipped_sections=[] 但键必须在位（加法式契约，前端不判 undefined）。"""
        await _seed(db_conn)
        monkeypatch.setattr(review_autofix, "AUTOFIX_MAX_SECTIONS", 10)
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME, findings=[_dlv05(SEC_A)],
            sections=_seeded_sections(), scheme={"name": "S", "type": "深基坑"})
        assert "skipped_sections" in res and "max_sections" in res
        assert res["skipped_sections"] == [] and res["stats"]["skipped"] == 0

    async def test_stage_empty_path_has_contract_keys(self, db_conn, monkeypatch):
        """router /stage 空返回与正常返回契约字段对齐（max_sections/skipped_sections）。"""
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [], "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            fake_overview)
        await _seed(db_conn)
        res = await ra.stage(SCHEME, {"scope": "all_blocking"}, db_conn)
        assert res["status"] == "empty"
        assert res["skipped_sections"] == []
        assert res["max_sections"] == review_autofix.AUTOFIX_MAX_SECTIONS


# ===========================================================================
# F2a · /confirm 条目粒度（复合键 vs 裸 rule_id）
# ===========================================================================
async def _make_batch(db, items: list[dict]) -> str:
    res = await repair_record.save_repair(
        db, scheme_id=SCHEME, scan_id="", mode="review_autofix_batch",
        items=items, snapshot_id="", total_conflicts=len(items),
        stats={"repaired": len(items), "failed": 0, "skipped": 0})
    return res["repair_id"]


def _repaired_item(sec: str, after: str, chain: int = 0, rule: str = "DLV-05") -> dict:
    return {"rule_id": rule, "section_id": sec, "section_title": sec,
            "mode": "auto", "status": "repaired", "reason": "", "targets": [],
            "before": _CTRL, "after": after, "problems": [],
            "chain_index": chain, "sentence_idx": 0, "sentence_total": 0}


class TestConfirmGranularity:

    async def test_composite_key_accepts_single_section_only(self, db_conn):
        """同规则两章各一条 item：accept 复合键仅命中单章（旧裸 rule_id 会两章全收）。"""
        from app.routers import review_autofix as ra
        await _seed(db_conn)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "已清理A"), _repaired_item(SEC_B, "已清理B")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "accept": [f"DLV-05|{SEC_B}"]}, db_conn)
        assert res["status"] == "confirmed" and res["accepted"] == 1
        assert await _content(db_conn, SEC_B) == "已清理B"
        assert "\x07" in await _content(db_conn, SEC_A)  # 未接受章逐字不动

    async def test_bare_rule_id_still_matches_all_items(self, db_conn):
        """裸 rule_id 旧调用方语义逐字不变：命中该规则全部条目。"""
        from app.routers import review_autofix as ra
        await _seed(db_conn)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "已清理A"), _repaired_item(SEC_B, "已清理B")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "accept": ["DLV-05"]}, db_conn)
        assert res["accepted"] == 2  # 条目计数（非规则计数）
        assert await _content(db_conn, SEC_A) == "已清理A"
        assert await _content(db_conn, SEC_B) == "已清理B"

    async def test_composite_key_reject_excludes_single_item(self, db_conn):
        from app.routers import review_autofix as ra
        await _seed(db_conn)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "已清理A"), _repaired_item(SEC_B, "已清理B")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "reject": [f"DLV-05|{SEC_A}"]}, db_conn)
        assert res["accepted"] == 1
        assert "\x07" in await _content(db_conn, SEC_A)
        assert await _content(db_conn, SEC_B) == "已清理B"

    async def test_skipped_entries_carry_section_id(self, db_conn, monkeypatch):
        """skipped 明细新增 section_id（前端逐项展示「哪章的哪条没修」）。"""
        from app.routers import review_autofix as ra

        async def empty_overview(db, scheme_id, **_kw):
            return {"findings": [], "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            empty_overview)
        # 手工构造非前缀链：accept 第二条 → 触发重算 → _resolve_finding 404
        await _seed(db_conn, a_content=_BOTH)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "step1", chain=0),
            _repaired_item(SEC_A, "step2", chain=1, rule="DLV-07")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "accept": [f"DLV-07|{SEC_A}"]}, db_conn)
        assert res["status"] == "confirmed" and res["accepted"] == 1
        assert len(res["skipped"]) == 1
        sk = res["skipped"][0]
        assert sk["rule_id"] == "DLV-07" and sk["section_id"] == SEC_A
        assert sk["status"] == 404
        # 未持久化任何改写（正文逐字不变）
        assert await _content(db_conn, SEC_A) == _BOTH


# ===========================================================================
# F2b · 非前缀接受 → 重新链式改写（只应用被接受项；本轮最大行为修复）
# ===========================================================================
class TestNonPrefixRecomputeReachable:

    async def test_accept_tail_of_chain_does_not_write_rejected_fix(self, db_conn, monkeypatch):
        """链 [DLV-05, DLV-07] 只接受末条 DLV-07：
        旧实现 merged 直接取链末 after（含被拒的 DLV-05 改写）→ \x07 被越权清掉；
        新实现非前缀 → 以**当前库内正文**重链，只闭合围栏、\x07 原样保留。
        """
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [_dlv05(SEC_A), _dlv07(SEC_A)],
                    "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            fake_overview)
        await _seed(db_conn, a_content=_BOTH)
        stage_res = await ra.stage(SCHEME, {"rule_ids": ["DLV-05", "DLV-07"]}, db_conn)
        assert stage_res["batch_id"], stage_res
        res = await ra.confirm(
            SCHEME, {"batch_id": stage_res["batch_id"],
                     "accept": [f"DLV-07|{SEC_A}"]}, db_conn)
        assert res["status"] == "confirmed"
        content = await _content(db_conn, SEC_A)
        assert content.count("```") % 2 == 0      # 被接受的 DLV-07 已修
        assert "\x07" in content                  # 被拒绝的 DLV-05 绝不静默写入

    async def test_prefix_accept_still_takes_merged_tail(self, db_conn, monkeypatch):
        """前缀式接受（既有主干）行为不回退：接受首条 → 取首条 after，围栏仍不闭。"""
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [_dlv05(SEC_A), _dlv07(SEC_A)],
                    "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            fake_overview)
        await _seed(db_conn, a_content=_BOTH)
        stage_res = await ra.stage(SCHEME, {"rule_ids": ["DLV-05", "DLV-07"]}, db_conn)
        res = await ra.confirm(
            SCHEME, {"batch_id": stage_res["batch_id"],
                     "accept": [f"DLV-05|{SEC_A}"]}, db_conn)
        assert res["status"] == "confirmed"
        content = await _content(db_conn, SEC_A)
        assert "\x07" not in content
        assert content.count("```") % 2 == 1  # 未接受 DLV-07 → 围栏保持未闭合


# ===========================================================================
# F3/F4/F5 · 前后端接线 parity 锁（源码级；R54 同族，Node 缺失下的替代护栏）
# ===========================================================================
_ROOT = Path(__file__).resolve().parents[2]


def _read_src(rel: str) -> str:
    return io.open(_ROOT / rel, encoding="utf-8-sig").read()


class TestFrontendWiringParity:

    def test_batch_modal_consumes_cap_and_skipped(self):
        s = _read_src(r"frontend\src\components\review\BatchFixModal.tsx")
        assert "skipped_sections" in s and "max_sections" in s      # F3 上限可见
        assert "skippedList" in s                                   # F3 confirm.skipped 消费
        assert 'AutoFixConfirmResult["skipped"]' in s

    def test_composite_key_payload_no_rule_id_only(self):
        s = _read_src(r"frontend\src\components\review\BatchFixModal.tsx")
        assert "acceptedKeys" in s
        assert "acceptedRuleIds" not in s  # 旧裸 rule_id 下发不得回流

    def test_types_expose_new_contract_fields(self):
        s = _read_src(r"frontend\src\types\audit.ts")
        for frag in ("max_sections?: number", "skipped_sections?",
                     "stale_ai_sources?: string[]", "section_id?: string"):
            assert frag in s, frag

    def test_dashboard_consumes_stale_ai_sources(self):
        s = _read_src(r"frontend\src\components\review\ReadinessDashboard.tsx")
        assert "stale_ai_sources" in s
        # 来源中文名必须复用 SOURCE_LABEL 单一映射，不得另写一份中文表
        assert s.count("本次总检未采用") == 1
        assert "SOURCE_LABEL[s] || s" in s

    def test_api_detail_array_single_source(self):
        s = _read_src(r"frontend\src\api\index.ts")
        assert s.count("function detailArrayToText") == 1
        assert "Array.isArray(rawDetail) ? detailArrayToText(rawDetail)" in s
        assert s.count("const scopeCn:") == 1  # 422 数组口径的实现只有一份
        assert s.count("detailArrayToText(") >= 3  # 定义 + REST + SSE 委托

    def test_component_tests_pin_new_behaviors(self):
        a = _read_src(r"frontend\src\tests\BatchFixModal.test.tsx")
        assert '"DLV-05|sec-a"' in a and '"CON-01|sec-b"' in a
        assert 'reject: ["DLV-05"],' not in a       # 旧断言不回流
        b = _read_src(r"frontend\src\tests\ReadinessDashboard.test.tsx")
        assert "stale_ai_sources" in b
        # ✅ R56 加固：新增前端用例标题逐条锁定 —— CI 侧若用例被删/改名即红，
        #    本机（Node 缺失）至少能证明「测试确实写进去了」。
        for t in ("命中章节上限", "confirm 返回 skipped", "同规则跨两章"):
            assert t in a, t
        assert "stale_ai_sources 非空" in b and "无 stale_ai_sources" in b

    def test_backend_response_keys_align_frontend_types(self):
        svc = _read_src(r"backend\app\services\review_autofix.py")
        rtr = _read_src(r"backend\app\routers\review_autofix.py")
        assert '"max_sections"' in svc and '"skipped_sections"' in svc
        assert '"skipped": skipped' in rtr and "_item_key" in rtr
        types = _read_src(r"frontend\src\types\audit.ts")
        for key in ("max_sections", "skipped_sections"):
            assert key in types and key in svc, key


# ===========================================================================
# F2c · 重算解析按章节精确（收口 R55 遗留②）
#   旧实现重算分支只传 rule_id —— 同规则跨章时会拿「别的章的证据」改本章；
#   现先按本章精确解析，miss 才回退规则级（保整篇型 finding 可用），回退命中
#   仍属别章 → 拒绝跨章改写并记 skipped 理由。
# ===========================================================================
class TestSectionPreciseResolve:

    async def test_cross_section_same_rule_not_applied(self, db_conn, monkeypatch):
        """accept 非前缀末条 DLV-07@SEC_A，但当前总检的 DLV-07 属于 SEC_B：
        旧实现拿规则级结果（SEC_B 的 finding）重算 SEC_A；现精确+回退都拒绝
        跨章 → skipped 记理由、正文逐字不动。"""
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [_dlv07(SEC_B)],
                    "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            fake_overview)
        await _seed(db_conn, a_content=_BOTH)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "step1", chain=0),
            _repaired_item(SEC_A, "step2", chain=1, rule="DLV-07")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "accept": [f"DLV-07|{SEC_A}"]}, db_conn)
        assert res["status"] == "confirmed" and res["accepted"] == 1
        assert len(res["skipped"]) == 1
        assert "不属于本章" in res["skipped"][0]["detail"]
        assert res["skipped"][0]["section_id"] == SEC_A
        # 别的章的证据绝不落本章：正文逐字不变（含被拒的 DLV-05 与被跳的围栏）
        assert await _content(db_conn, SEC_A) == _BOTH

    async def test_whole_doc_finding_fallback_kept_working(self, db_conn, monkeypatch):
        """总检命中的是**无 section_id 的整篇型** finding（CON-01 族）→ 回退
        规则级成功、重算照常进行（历史可用性回归保护）。"""
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [{**_dlv07(SEC_A), "section_id": ""}],
                    "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr("app.routers.compliance._readiness_overview_compute",
                            fake_overview)
        called = {}

        async def fake_chain(section, findings, scheme, facts, standards_text):
            called["n"] = len(findings)
            return "RECOMPUTED", [{"status": "repaired"}]

        monkeypatch.setattr("app.services.review_autofix._chain_section_fixes",
                            fake_chain)
        await _seed(db_conn, a_content=_BOTH)
        batch = await _make_batch(db_conn, [
            _repaired_item(SEC_A, "step1", chain=0),
            _repaired_item(SEC_A, "step2", chain=1, rule="DLV-07")])
        res = await ra.confirm(SCHEME, {"batch_id": batch,
                                        "accept": [f"DLV-07|{SEC_A}"]}, db_conn)
        assert res["skipped"] == []
        assert called.get("n") == 1          # 整篇型回退命中 → 重算被调用
        assert await _content(db_conn, SEC_A) == "RECOMPUTED"


# ===========================================================================
# F6 · 前端源码字节体检（R56 抓到的真实缺陷：R55 的字节补丁把 api/index.ts
#   两条 SSE 语句里的 \r\n / \n **转义文本展开成了裸 CR/LF 字节** —— JS 正则
#   字面量内不允许出现行终止符，那是 SyntaxError，整个 api 模块编译不过，
#   SSE 正文进度流全断。本机无 Node 跑不了 tsc/vitest，唯有「字节形态 +
#   转义文本在位」两层锁能当场发现，故固化为常设护栏。
# ===========================================================================
_FE_TOUCHED = (
    r"frontend\src\api\index.ts",
    r"frontend\src\types\audit.ts",
    r"frontend\src\components\review\BatchFixModal.tsx",
    r"frontend\src\components\review\ReadinessDashboard.tsx",
    r"frontend\src\tests\BatchFixModal.test.tsx",
    r"frontend\src\tests\ReadinessDashboard.test.tsx",
)


def _read_bytes(rel: str) -> bytes:
    return io.open(_ROOT / rel, "rb").read()


class TestFrontendByteIntegrity:

    def test_no_lone_cr_or_control_bytes(self):
        """孤立 CR / NUL / BEL / FORM FEED / U+FFFD 一律视为补丁损坏签名。
        纯 CRLF 与纯 LF 都合法（core.autocrlf=true 会做检出转换，不是缺陷）。

        ⚠️ 已知局限（R56 A/B 实测）：本判据**抓不到**「转义被展开成 CRLF 对」
        —— CR 后面正好跟 LF 时孤立数为 0。那种损坏由下面
        test_sse_escape_text_not_expanded_to_raw_bytes 的转义文本锁兜住，
        两条不是冗余，是互补。"""
        for rel in _FE_TOUCHED:
            b = _read_bytes(rel)
            assert b.count(b"\x0d") == b.count(b"\x0d\x0a"), rel   # 无孤立 CR
            for bad in (b"\x00", b"\x07", b"\x0c"):
                assert bad not in b, (rel, bad)
            assert "\ufffd" not in b.decode("utf-8-sig"), rel

    def test_sse_escape_text_not_expanded_to_raw_bytes(self):
        """核心回归锁：SSE 拆行/分隔必须是**转义文本**，一个 CR 字节都不许有。"""
        rel = r"frontend\src\api\index.ts"
        b = _read_bytes(rel)
        assert b"\x0d" not in b, rel
        s = b.decode("utf-8-sig")
        for lit in (r'block.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")',
                    r'const separator = /\r\n\r\n|\n\n|\r\r/;'):
            assert s.count(lit) == 1, lit

    def test_files_end_with_a_proper_terminator(self):
        """补丁截断的最常见形态是「文件末尾少一截」→ 收尾行必须像收尾行。"""
        for rel in _FE_TOUCHED:
            s = _read_bytes(rel).decode("utf-8-sig")
            last = [x for x in s.split("\n") if x.strip()][-1].strip()
            assert last in ("});", "};") or last.startswith("export default "), (rel, last)

    def test_new_describe_blocks_present_once(self):
        """新增用例块各出现 1 次（被复制/被截掉都会红），且用例总量不缩水。"""
        a = _read_src(r"frontend\src\tests\BatchFixModal.test.tsx")
        assert a.count('describe("BatchFixModal · R55') == 1
        assert a.count("it(") >= 20 and a.count("expect(") >= 80
        b = _read_src(r"frontend\src\tests\ReadinessDashboard.test.tsx")
        assert b.count('describe("ReadinessDashboard · R55') == 1
        assert b.count("it(") >= 30


# ===========================================================================
# I · 有 Node 就真跑（R55 遗留①的自动闭环，R57 升级为三级解析）
#   CI 的 frontend job 本就跑 eslint + tsc + vitest；本机 nvm 存储目录被删后
#   PATH 里没有 node，但系统里仍有**可用的 Node 运行时**（Playwright / Visual
#   Studio / IDE 自带的 node.exe，其中 Playwright 那份就是完整版 Node）。
#   R57 首跑全量前端测试正是走这条通道抓到 4 处静默缺陷，所以解析不再只看 PATH：
#       FRONTEND_NODE_BIN 环境变量  >  shutil.which("node")  >  自带运行时探测
#   三级全落空才 skip；node_modules 缺失（如 CI 的 backend job，runner 预装了
#   node 但没装前端依赖）同样 skip —— 否则断言会把它误报成失败。
# ===========================================================================
_NODE_ENV = "FRONTEND_NODE_BIN"
# 已知「随别的工具一起装好」的运行时；用 glob 兼容版本/安装位置差异
_NODE_PROBES = (
    "C:" + os.sep + "Program Files" + os.sep + "Python*" + os.sep
    + "Lib" + os.sep + "site-packages" + os.sep + "playwright" + os.sep
    + "driver" + os.sep + "node.exe",
    "C:" + os.sep + "Program Files*" + os.sep + "Microsoft Visual Studio"
    + os.sep + "*" + os.sep + "*" + os.sep + "MSBuild" + os.sep
    + "Microsoft" + os.sep + "VisualStudio" + os.sep + "NodeJs" + os.sep + "node.exe",
    "C:" + os.sep + "Users" + os.sep + "*" + os.sep + "AppData" + os.sep + "Roaming"
    + os.sep + "TRAE SOLO CN" + os.sep + "ModularData" + os.sep + "ai-agent"
    + os.sep + "vm" + os.sep + "tools" + os.sep + "node" + os.sep + "node.exe",
)
# R57 起一并执行：这 4 个文件的用例都早于本轮写出，却从未在本机跑过
_FE_TEST_FILES = (
    "src/tests/BatchFixModal.test.tsx",
    "src/tests/ReadinessDashboard.test.tsx",
    "src/tests/factsAdjust.test.tsx",
    "src/tests/ReviewWorkflowPanel.test.tsx",
)
_FE_SCRIPTS = ("typescript/bin/tsc", "vitest/vitest.mjs", "eslint/bin/eslint.js")
_FE_DIR = _ROOT / "frontend"


def _node_ok(path) -> bool:
    """能真的执行 --version 才算可用（悬空符号链接、无执行权限都在这里被排除）。"""
    if not path:
        return False
    try:
        r = subprocess.run([str(path), "--version"], capture_output=True, timeout=30)
    except Exception:
        return False
    return r.returncode == 0 and b"v" in (r.stdout or b"")


def _resolve_frontend_node() -> str:
    """Node 可执行文件的**唯一出口**：显式环境变量 > PATH > 自带运行时探测。"""
    env = (os.environ.get(_NODE_ENV) or "").strip().strip(chr(39))
    if env:
        if _node_ok(env):
            return env
        raise AssertionError("%s 指向的文件不可执行：%s" % (_NODE_ENV, env))
    found = shutil.which("node")
    if found:
        return found
    for pattern in _NODE_PROBES:
        for cand in sorted(glob.glob(pattern)):
            if _node_ok(cand):
                return cand
    return ""


def _fe_ready(node, fe_dir) -> bool:
    """真跑层能否执行 = **既有 Node、又装好三件套依赖**（单一出口，可单测）。

    CI 的 backend job：runner 预装了 node、但不跑 npm ci —— 只看 node 会让三个
    真跑用例当场「失败」而不是 skip，把 CI 判红。判据必须带 node_modules。
    """
    if not node:
        return False
    base = Path(fe_dir) / "node_modules"
    return all((base / s).exists() for s in _FE_SCRIPTS)


_NODE = _resolve_frontend_node()
# 依赖没装（node_modules 不存在）时不执行，避免 CI backend job 误报失败
_FE_READY = _fe_ready(_NODE, _FE_DIR)


@pytest.mark.skipif(not _FE_READY,
                    reason="无可用 Node.js（%s / PATH / 自带运行时探测三级全落空），"
                           "或 frontend/node_modules 未安装" % _NODE_ENV)
class TestFrontendRealExecution:
    """真跑前端三件套（tsc / vitest / eslint）。耗时约 3~4 分钟，但 R57 已证明
    这是唯一能抓到「源码看起来对、实际跑不起来」这一类缺陷的层。"""

    def _run(self, script: str, args: list) -> subprocess.CompletedProcess:
        path = str(_FE_DIR / "node_modules" / script)
        assert io.open(path, "rb").read(), path        # 依赖装过才有 node_modules
        return subprocess.run([_NODE, path] + args, cwd=str(_FE_DIR),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)

    def test_tsc_no_emit(self):
        r = self._run("typescript/bin/tsc", ["--noEmit", "-p", "tsconfig.json"])
        assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]

    def test_vitest_review_closeout(self):
        r = self._run("vitest/vitest.mjs", ["run", "--reporter=basic"]
                      + list(_FE_TEST_FILES))
        assert r.returncode == 0, (r.stdout + r.stderr)[-6000:]

    def test_eslint_zero_errors(self):
        """npm run lint 等价：CI 只看退出码，**2 个 error 就能让整个 frontend job 红**
        （R57 实测：prefer-const + 逗号表达式各 1 处）。warning 不拦（存量 1188 条，
        属独立的清账任务），只锁 error 归零不回流。"""
        r = self._run("eslint/bin/eslint.js", ["."])
        blob = (r.stdout or "") + (r.stderr or "")
        tail = blob[-2500:]
        assert "problem" in blob or "warning" in blob, tail   # eslint 真的跑起来了
        assert "0 errors" in blob, tail
        assert r.returncode == 0, tail
