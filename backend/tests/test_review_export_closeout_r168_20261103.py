"""✅ R168（2026-11-03）· 审核与预检 + 导出文档模块收口护栏。

本轮修复面（全部带行为实证或源码锁）：
- FIX-1  preflight_engine.check_safety 应急/监测章节正文改「有效正文」（自身+全部
         子孙，与 check_completeness/check_traceability 同口径）—— 父节点形态
         SAF-03/04/05/06 此前被 547 行 if 门静默跳过（漏报）。
- FIX-2  consistency_fullbook_repair 回写阶段缓冲化、由 save_repair 单点提交 ——
         中途异常零半生效（旧实现每章 UPDATE 即 commit）。
- FIX-3  review_autofix stage→confirm 内容漂移守卫（content_sha_before）。
- FIX-4  content_progress 休眠函数族删除（活路径唯一在 _sse_outline_build）。
- FIX-5  audit_service.list_prompt_audit_logs 分页参数脏值不再 500。
- FIX-6  export_docx 普通异常分支补注销在飞登记（资源泄漏）。
- FIX-7  _count_unclosed_chart_fences 闭合判据对齐 fence_common.is_fence_close
         （R76 漏改面：带信息串行被误判闭合 → DLV-07 漏报）。
- FIX-8  _prepare_export 方案存在性读取 strict=True，DBReadError → 503（不再伪装
         404「方案不存在」）。
"""
import ast
import hashlib
from pathlib import Path

import pytest
from app.routers import export as export_mod
from app.routers import review_autofix as raf_router
from app.routers._export_issue_scan import _count_unclosed_chart_fences
from app.services import audit_service, repair_record
from app.services import consistency_fullbook_repair as cfr
from app.services import content_progress as cp
from app.services import preflight_engine as pe

R168 = "R168"

# ---------------------------------------------------------------------------
# A 组 · FIX-1 check_safety 有效正文口径（漏报收口）
# ---------------------------------------------------------------------------

_SAFETY_CHILDREN = [
    # 子节标题刻意不含 应急/救援/预案（否则旧口径也会把它们扫进来，测试不判别）
    {"id": "e1", "title": "组织机构及职责", "content": "项目成立应急组织机构，明确组长职责与分工。",
     "level": 2, "parent_id": "ep"},
    {"id": "e2", "title": "物资与装备", "content": "现场配备应急物资、救援器材并建立储备台账。",
     "level": 2, "parent_id": "ep"},
    {"id": "e3", "title": "演练安排", "content": "每半年组织一次应急演练并评估改进。", "level": 2,
     "parent_id": "ep"},
]


def _ctx(sections, scheme_name="基坑支护工程施工方案"):
    return pe.PreflightContext(scheme_id="sch-t", scheme_name=scheme_name,
                               sections=sections)


class TestCheckSafetyEffectiveContent:
    def test_a1_parent_node_keywords_found_no_false_positive(self):
        """有效正文判据不误伤：三要素齐全的父节点形态 → 0 findings（与旧口径同）。"""
        sections = [{"id": "ep", "title": "应急预案", "content": "", "level": 1},
                    *_SAFETY_CHILDREN]
        out = [f for f in pe.check_safety(_ctx(sections))
               if f["rule_id"] in ("SAF-03", "SAF-04", "SAF-05")]
        assert out == [], f"三要素齐全不应报：{[f['rule_id'] for f in out]}"

    def test_a2_missing_element_still_reported_for_parent_node(self):
        """核心判别：父节点形态（旧口径 emer_text 为空 → 整组静默跳过），
        子节确实缺「演练」要素 → 修复后必须报 SAF-05（旧实现 0 findings = 漏报）。"""
        sections = [{"id": "ep", "title": "应急预案", "content": "", "level": 1},
                    _SAFETY_CHILDREN[0], _SAFETY_CHILDREN[1]]  # 去掉演练子节
        out = [f for f in pe.check_safety(_ctx(sections))
               if f["rule_id"] in ("SAF-03", "SAF-04", "SAF-05")]
        assert [f["rule_id"] for f in out] == ["SAF-05"], \
            "父节点形态缺演练要素必须报 SAF-05（旧口径整组静默跳过 = 漏报）"

    def test_a3_monitor_threshold_reported_via_descendants(self):
        """SAF-06 同病：监测方案为父节点、预警值写在子节 → 修复后不再误报
        「未给出预警值」（旧口径只看父节点自身空 content）。"""
        sections = [
            {"id": "mp", "title": "监测监控方案", "content": "", "level": 1},
            {"id": "m1", "title": "沉降观测", "content": "布点后按频次观测，报警值 30mm。",
             "level": 2, "parent_id": "mp"},
        ]
        out = [f for f in pe.check_safety(_ctx(sections)) if f["rule_id"] == "SAF-06"]
        assert out == [], f"子节已给报警值不应报 SAF-06：{out}"

    def test_a4_single_implementation_source_lock(self):
        """check_safety 必须走 build_section_tree_index（与 completeness 同口径），
        不得回退为只拼自身 content。"""
        src = Path(pe.__file__).read_text(encoding="utf-8")
        fn = [n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "check_safety"]
        assert fn, "check_safety 函数缺失"
        seg = ast.get_source_segment(src, fn[0]) or ""
        assert "build_section_tree_index" in seg
        # 旧的「只拼自身 content」形态不得回流（ emer_text = join(s.get("content")) ）
        assert 'join((s.get("content") or "")' not in seg

# ---------------------------------------------------------------------------
# B 组 · FIX-2 整卷修复原子性
# ---------------------------------------------------------------------------


class _BoomOnNthSectionsUpdate:
    """db 代理：第 n 条 `UPDATE sections` 抛异常（模拟写库中途故障），其余委托真连接。"""

    def __init__(self, conn, boom_on: int):
        self._conn = conn
        self._boom_on = boom_on
        self._n = 0

    async def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE SECTIONS"):
            self._n += 1
            if self._n == self._boom_on:
                raise RuntimeError("模拟第 2 章写库失败")
        return await self._conn.execute(sql, params)

    def __getattr__(self, item):
        return getattr(self._conn, item)


class TestFullbookAtomicity:
    def _conflict(self):
        return {"id": "c1", "conflict_type": "value", "severity": "high",
                "topic": "混凝土强度", "authoritative_value": "C35",
                "repair_instruction": "", "status": "pending",
                "occurrences": [{"section_id": "s1", "value": "C30"},
                                {"section_id": "s2", "value": "C30"}]}

    async def _seed(self, db_conn):
        for sid in ("s1", "s2"):
            await db_conn.execute(
                "INSERT INTO sections (id, scheme_id, title, content, level,"
                " sort_order) VALUES (?, 'sch-fb', ?, ?, 2, ?)",
                (sid, f"第{sid}节", "本章采用 C30 混凝土浇筑施工。", sid))
        await db_conn.commit()

    async def test_b1_midway_failure_leaves_no_partial_state(
            self, db_conn, monkeypatch):
        """第 2 章 UPDATE 炸 → 异常上抛；回滚后章节/冲突/批次**全部**保持原状
        （旧实现第 1 章已 commit，留下无批次记录的半生效态）。"""
        await self._seed(db_conn)
        reply = cfr.build_fullbook_workspace(
            facts="F", conflicts_digest="D",
            sections=[{"id": "s1", "title": "第s1节",
                       "content": "本章采用 C35 混凝土浇筑施工。"},
                      {"id": "s2", "title": "第s2节",
                       "content": "本章采用 C35 混凝土浇筑施工。"}])

        async def _fake(messages, **kw):
            return reply
        monkeypatch.setattr(cfr, "chat_with_fallback", _fake)
        proxy = _BoomOnNthSectionsUpdate(db_conn, boom_on=2)
        with pytest.raises(RuntimeError, match="模拟第 2 章写库失败"):
            await cfr.run_fullbook_repair(
                proxy, scheme_id="sch-fb", scan_id="scan-fb",
                conflicts=[self._conflict()], facts="F")
        await db_conn.rollback()  # 模拟 get_db 归还前的兜底回滚
        rows = {r["id"]: r["content"] for r in
                (await (await db_conn.execute(
                    "SELECT id, content FROM sections WHERE scheme_id='sch-fb'")
                ).fetchall())}
        assert all(v == "本章采用 C30 混凝土浇筑施工。" for v in rows.values()), \
            "中途失败不得留下任何章节改写"
        confl = (await (await db_conn.execute(
            "SELECT status FROM consistency_conflicts WHERE id='c1'")
        ).fetchall())
        assert not confl or confl[0]["status"] == "pending", \
            "中途失败不得回写冲突状态"
        n_repairs = (await (await db_conn.execute(
            "SELECT COUNT(*) AS n FROM consistency_repairs WHERE scheme_id='sch-fb'"
        )).fetchone())["n"]
        assert n_repairs == 0, "中途失败不得留下批次记录"

    async def test_b2_success_path_still_lands_everything(
            self, db_conn, monkeypatch):
        """正向对照：同样的输入，全部成功时章节/冲突状态/批次一次性落库。"""
        await self._seed(db_conn)
        reply = cfr.build_fullbook_workspace(
            facts="F", conflicts_digest="D",
            sections=[{"id": "s1", "title": "第s1节",
                       "content": "本章采用 C35 混凝土浇筑施工。"},
                      {"id": "s2", "title": "第s2节",
                       "content": "本章采用 C35 混凝土浇筑施工。"}])

        async def _fake(messages, **kw):
            return reply
        monkeypatch.setattr(cfr, "chat_with_fallback", _fake)
        result = await cfr.run_fullbook_repair(
            db_conn, scheme_id="sch-fb", scan_id="scan-fb",
            conflicts=[self._conflict()], facts="F")
        assert result["repaired"] == 1 and result["failed"] == 0
        rows = {r["id"]: r["content"] for r in
                (await (await db_conn.execute(
                    "SELECT id, content FROM sections WHERE scheme_id='sch-fb'")
                ).fetchall())}
        assert all("C35" in v for v in rows.values())
        n_repairs = (await (await db_conn.execute(
            "SELECT COUNT(*) AS n FROM consistency_repairs WHERE scheme_id='sch-fb'"
        )).fetchone())["n"]
        assert n_repairs == 1

    def test_b3_no_intermediate_commit_source_lock(self):
        """run_fullbook_repair 函数体内不得出现 db.commit —— 提交权唯一归
        save_repair（快照 create_snapshot 自带 commit 先行独立生效）。"""
        src = Path(cfr.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "run_fullbook_repair"]
        assert fn, "run_fullbook_repair 缺失"
        seg = ast.get_source_segment(src, fn[0]) or ""
        assert "db.commit" not in seg, \
            "回写阶段不得中途 commit（原子性由 save_repair 单点提交保证）"

# ---------------------------------------------------------------------------
# C 组 · FIX-3 stage→confirm 内容漂移守卫
# ---------------------------------------------------------------------------


def _sha16(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class TestAutofixContentDriftGuard:
    async def _seed(self, db_conn):
        await db_conn.execute(
            "INSERT INTO schemes (id, name, project_id) VALUES "
            "('sch-dr', '漂移方案', 'p1')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, title, content, level,"
            " sort_order) VALUES ('s1', 'sch-dr', '第一节', '原始正文。', 1, 1)")
        await db_conn.commit()

    async def _stage_batch(self, db_conn, content_sha: str) -> str:
        return (await repair_record.save_repair(
            db_conn, scheme_id="sch-dr", scan_id="", mode="review_autofix_batch",
            items=[{"rule_id": "DLV-05", "section_id": "s1",
                    "section_title": "第一节", "status": "repaired",
                    "before": "原始正文。", "after": "暂存修复后的正文。",
                    "content_sha_before": content_sha}],
            snapshot_id="", total_conflicts=1,
            stats={"repaired": 1, "failed": 0, "skipped": 0}))["repair_id"]

    async def test_c1_stale_content_skips_section(self, db_conn, monkeypatch):
        """暂存后正文被改（sha 漂移）→ confirm 前缀路径不落库、记 skipped。"""
        await self._seed(db_conn)
        batch_id = await self._stage_batch(db_conn, content_sha="deadbeefdeadbeef")
        persisted = []
        async def _spy_persist(db, **kw):
            persisted.append(kw.get("pending") or [])
            return "snap-x"
        monkeypatch.setattr(raf_router, "_persist_fixed", _spy_persist)
        resp = await raf_router.confirm(
            "sch-dr", {"batch_id": batch_id, "accept_all": True}, db_conn)
        assert resp["status"] == "confirmed"
        assert persisted == [], "漂移章节不得落库"
        assert len(resp["skipped"]) == 1
        sk = resp["skipped"][0]
        assert sk["section_id"] == "s1" and sk["status"] == "content_changed"
        assert "已被修改" in sk["detail"]

    async def test_c2_matching_sha_still_persists(self, db_conn, monkeypatch):
        """正向对照：正文未漂移 → 前缀合并照常落库（守卫不误伤）。"""
        await self._seed(db_conn)
        batch_id = await self._stage_batch(
            db_conn, content_sha=_sha16("原始正文。"))
        persisted = []
        async def _spy_persist(db, **kw):
            persisted.append(kw.get("pending") or [])
            return "snap-x"
        monkeypatch.setattr(raf_router, "_persist_fixed", _spy_persist)
        resp = await raf_router.confirm(
            "sch-dr", {"batch_id": batch_id, "accept_all": True}, db_conn)
        assert resp["status"] == "confirmed"
        assert len(persisted) == 1 and len(persisted[0]) == 1
        assert persisted[0][0][2] == "暂存修复后的正文。"
        assert resp["skipped"] == []

    async def test_c3_stage_stamps_content_sha(self, db_conn, monkeypatch):
        """stage_fixes 必须给 items 盖 content_sha_before（守卫的输入端）。"""
        from app.services import review_autofix as svc
        sections = [{"id": "s1", "title": "第一节",
                     "content": "本章存在 DDIT 控制字符\x07。", "level": 1}]
        finding = {"rule_id": "DLV-05", "severity": "block",
                   "title": "控制字符", "section_id": "s1"}
        res = await svc.stage_fixes(
            db_conn, scheme_id="sch-sha", findings=[finding],
            sections=sections, scheme={"id": "sch-sha", "name": "方案"})
        items = res["items"]
        assert items, "AUTO 模式（控制字符）应产出暂存项"
        assert all(it.get("content_sha_before") == _sha16(
            "本章存在 DDIT 控制字符\x07。") for it in items), \
            "stage 必须记录暂存时原始正文指纹"

    def test_c4_legacy_batch_without_sha_keeps_old_behavior_source_lock(self):
        """旧批次（无 content_sha_before）必须保持旧行为 —— 守卫只在指纹存在时生效。"""
        seg = Path(raf_router.__file__).read_text(encoding="utf-8")
        fn = [n for n in ast.parse(seg).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "confirm"]
        assert fn, "confirm 缺失"
        body = ast.get_source_segment(seg, fn[0]) or ""
        assert "content_sha_before" in body
        assert 'it.get("content_sha_before") or ""' in body, \
            "无指纹的旧批次必须回退旧行为（守卫不得误伤历史暂存）"

# ---------------------------------------------------------------------------
# D 组 · FIX-4 content_progress 收敛为纯常量事实源
# ---------------------------------------------------------------------------


class TestContentProgressConstantsOnly:
    def test_d1_dormant_functions_gone(self):
        for name in ("section_partial", "calibrate_stage_expect",
                     "monotonic_progress", "weighted_progress", "fmt_duration",
                     "snapshot_stats", "make_content_progress_state"):
            assert not hasattr(cp, name), f"休眠函数回流：{name}"

    def test_d2_constants_intact(self):
        assert cp.SECTION_PHASE_MAX == 0.95
        assert cp.STAGE_MODEL["draft"]["expect"] == 90.0
        assert cp.STAGE_FILL_MAX == 0.85
        assert cp.STATS_PUSH_INTERVAL == 3.0

    def test_d3_live_aliases_unaffected(self):
        """sse_handlers 的常量别名仍指向本模块（单一事实源未被破坏）。"""
        from app.routers import sse_handlers as sh
        assert sh._STAGE_MODEL is cp.STAGE_MODEL
        assert sh._SECTION_PHASE_MAX is cp.SECTION_PHASE_MAX
        assert callable(sh._section_partial)  # 活实现（_sse_outline_build）仍可达

# ---------------------------------------------------------------------------
# E 组 · FIX-5 审计分页参数脏值容错
# ---------------------------------------------------------------------------


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class _Db:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return _Cur([(0,)] if "COUNT" in sql else [])


class TestAuditServicePagingGuard:
    async def test_e1_dirty_paging_params_fall_back(self):
        """limit/offset 传非数字字符串 → 回退默认 (50, 0)，不再 ValueError 500。"""
        db = _Db()
        result = await audit_service.list_prompt_audit_logs(
            db, "some.prompt.key", limit="abc", offset="xyz")  # type: ignore[arg-type]
        assert result["total"] == 0
        sql, params = db.calls[-1]
        assert params[1:] == (50, 0), f"脏分页参数应回退默认 (50, 0)，实得 {params[1:]}"

    async def test_e2_out_of_range_clamped(self):
        db = _Db()
        await audit_service.list_prompt_audit_logs(db, "k", limit=999, offset=-5)
        assert db.calls[-1][1][1:] == (200, 0)


import asyncio  # noqa: E402  （E 组同步驱动协程用）

# ---------------------------------------------------------------------------
# F 组 · FIX-6 导出在飞登记注销
# ---------------------------------------------------------------------------


class TestExportInflightUnregister:
    def _docx_src(self) -> str:
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "export_docx"]
        assert fn, "export_docx 缺失"
        return ast.get_source_segment(src, fn[0]) or ""

    def test_f1_cancelled_branch_unregisters(self):
        seg = self._docx_src()
        i0 = seg.index("except ExportCancelled:")
        i1 = seg.index("except Exception:")
        assert "_export_inflight_unregister(_inflight_final)" in seg[i0:i1]

    def test_f2_plain_exception_branch_unregisters(self):
        seg = self._docx_src()
        i1 = seg.index("except Exception:")
        tail = seg[i1:seg.index("import os", i1)]
        assert "_export_inflight_unregister(_inflight_final)" in tail, \
            "普通异常分支必须注销在飞登记（否则失败一次即永久残留 GC 盲区）"

# ---------------------------------------------------------------------------
# G 组 · FIX-7 围栏闭合判据对齐
# ---------------------------------------------------------------------------


class TestFenceCloseCriterionParity:
    def test_g1_info_string_line_is_not_a_close(self):
        """「```js」带信息串是内容不是闭合（R76 判据）—— 预检必须与渲染侧一致。"""
        assert _count_unclosed_chart_fences(
            "```mermaid\ngraph TB\nA-->B\n```js\n后续正文") == 1
        assert _count_unclosed_chart_fences(
            "```mermaid\ngraph TB\nA-->B\n```\n后续正文") == 0

    def test_g2_length_mismatch_still_tolerated(self):
        """4 开 + 3 闭（信息串为空）仍按容错口径判闭合（不回归 R76 前的误报）。"""
        assert _count_unclosed_chart_fences(
            "````mermaid\ngraph TB\n````\n```\n```\n尾") == 0

    def test_g3_single_criterion_source_lock(self):
        src = Path(
            __import__("app.routers._export_issue_scan",
                       fromlist=["_x"]).__file__).read_text(encoding="utf-8")
        assert "is_fence_close" in src
        assert "pf_end[0] == open_char" not in src, \
            "旧「同种字符即闭合」判据不得回流"

# ---------------------------------------------------------------------------
# H 组 · FIX-8 方案存在性读取严格化
# ---------------------------------------------------------------------------


class _BoomCur:
    async def fetchone(self):
        raise RuntimeError("connection gone")


class _NoneCur:
    async def fetchone(self):
        return None


class _FakeDb:
    def __init__(self, cur):
        self._cur = cur

    async def execute(self, sql, params=()):
        return self._cur


class TestExportSchemeReadStrict:
    async def test_h1_db_failure_is_503_not_404(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await export_mod._prepare_export("sch-x", {}, _FakeDb(_BoomCur()))
        assert ei.value.status_code == 503, \
            "库故障不得伪装成 404「方案不存在」"
        assert "读取失败" in str(ei.value.detail)

    async def test_h2_missing_scheme_still_404(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await export_mod._prepare_export("sch-x", {}, _FakeDb(_NoneCur()))
        assert ei.value.status_code == 404
        assert "方案不存在" in str(ei.value.detail)

# ---------------------------------------------------------------------------
# I 组 · 导出 DOCX 可打开性端到端（WPS / MS Office 兼容基线，R168 用户验收点）
# ---------------------------------------------------------------------------
#   用户指令：「导出文档符合 WPS 格式与 Windows Office 文档格式标准，修复导出
#   文档无法打开问题」。本组走**全链路** export_docx（取数 → 渲染段 → 构建 →
#   R109 严格序列归一 → 落盘 → 原子替换 → 缓存行），对产物做四层「打不开」体检：
#   zip CRC 完整性 / 必需包部件在场 / 全部 XML 部件良构 / python-docx 回读。


class TestExportDocxOpenabilityE2E:
    async def _export(self, db_conn) -> Path:
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES "
            "('s-op', 'p1', '可打开性探针方案')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level,"
            " title, status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("o1", "s-op", "", 0, 1, "第一章 总体要求", "generated",
             "## 总体安排\n正文段落，含中文标点与 English terms。\n\n"
             "| 项目 | 参数 |\n| --- | --- |\n| 混凝土强度 | C35 |\n\n"
             "```mermaid\ngraph TB\n A[开始]-->B[结束]\n```\n"))
        await db_conn.commit()
        resp = await export_mod.export_docx(
            "s-op",
            {"project_name": "示例项目", "bid_section": "第一标段",
             "config": {}, "chart_images": [], "fe_codes": []},
            db=db_conn)
        return Path(resp.path)

    async def test_i1_zip_structure_and_content_types(self, db_conn):
        import zipfile
        path = await self._export(db_conn)
        assert path.exists() and path.stat().st_size > 0
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None, "zip CRC 校验失败（文件损坏，Word/WPS 必打不开）"
            names = set(z.namelist())
            for required in ("[Content_Types].xml", "_rels/.rels",
                             "word/document.xml", "word/styles.xml"):
                assert required in names, f"OPC 包缺必需部件 {required}"
            import xml.etree.ElementTree as ET
            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    ET.fromstring(z.read(n))  # 任一部件非良构 XML 即打不开

    async def test_i2_pythondocx_roundtrip_with_real_table(self, db_conn):
        import zipfile

        from docx import Document
        path = await self._export(db_conn)
        doc = Document(str(path))
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        assert texts, "回读段落为空"
        assert doc.tables, "GFM 表格未成为真 Word 表格"
        assert any("C35" in c.text for row in doc.tables[0].rows
                   for c in row.cells), "表格单元格内容缺失"
        # 渲染依赖外部服务的 mermaid：默认失败即整块跳过不占号（既有纪律），
        # 产物中不得出现源码围栏残片
        assert "```" not in "\n".join(texts), "围栏残片进入交付文档"

    async def test_i3_strict_sequence_normalizer_wired(self):
        """R109 修复链（WPS 兼容核心）必须仍在落盘前执行。"""
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_build_docx_sync"]
        assert fn, "_build_docx_sync 缺失"
        seg = ast.get_source_segment(src, fn[0]) or ""
        i_norm = seg.index("_normalize_docx_strict_sequence")
        i_save = seg.index("doc.save(")
        assert i_norm < i_save, "严格序列归一必须在 doc.save 之前"

