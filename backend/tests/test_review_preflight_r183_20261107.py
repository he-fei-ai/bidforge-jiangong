"""R183（2026-11-07）· 审核与预检模块三态口径补强。

本轮修复的两个「无法判定被折叠成通过」缺陷，均先复现红、再验证绿：

  1. 交叉引用预检的中间态（export.py::collect_export_issues）
     B-17 只在「图、表集合都为空」时判 undetermined；方案**只有表**、正文却
     引用「图9-9」时，图号分支整体被跳过 → issues 为空 → 前端显示「预检通过」。
     实际是**无法判定**（真实图集合为空，无从核对）。现按引用类型分别判定，
     回传 reason=no_fig_set / no_tbl_set / no_fig_and_tbl_set。

  2. 程序化检查项异常（compliance.py::_readiness_overview_compute）
     checker 抛错只降级为一条 low 级 engine_error finding（-3 分），总分仍可能
     ≥75、grade=A/B、released=True → 用户看到「建议放行」，实际有体检项没跑。
     现把异常计入 partial_sources → incomplete → released 必为 False。

铁律：三态 = 通过 / 不通过 / **无法判定**，不得把「无法判定」折叠成「通过」。
"""

import app.routers.compliance as C
import app.routers.export as E
import app.services.preflight_engine as PE

SCHEME = "r183-s1"
PROJ = "r183-p1"
SEC = "r183-sec1"

# 只有表、没有图，但正文引用了「图9-9」——旧实现会返回 0 问题并当通过
TABLE_ONLY_WITH_FIG_REF = (
    "| 项目 | 数值 |\n| --- | --- |\n| 工期 | 120 |\n\n施工流向详见 图9-9。\n"
)
# 只有表、且正文没有图号引用——不应误报「无法判定」
TABLE_ONLY_NO_FIG_REF = "| 项目 | 数值 |\n| --- | --- |\n| 工期 | 120 |\n"
# 只有图、正文引用图号——图集合非空，必须判为可核对（不得误报无法判定）
CHART_ONLY_WITH_FIG_REF = "```mermaid\nflowchart LR\n  A-->B\n```\n\n施工流向详见 图1-1。\n"


async def _seed(db, content: str) -> None:
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (SCHEME, PROJ, "R183审核预检"))
    await db.execute(
        "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
        "word_status,word_budget,status,level,sort_order,parent_id,review_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SEC, SCHEME, PROJ, "施工工艺", content, max(len(content), 1), "ok", 5000, "generated", 1, 1, "", "pending"),
    )
    await db.commit()


class TestCrossrefUndeterminedMiddleState:
    """缺陷 1：只表/只图时，另一种引用类型必须回传「无法判定」。"""

    async def test_table_only_with_fig_ref_is_undetermined_no_fig_set(self, db_conn):
        await _seed(db_conn, TABLE_ONLY_WITH_FIG_REF)
        result = await E.collect_export_issues(SCHEME, db_conn, strict_read=True)
        check = result["crossref_check"]
        assert check["fig_count"] == 0
        assert check["tbl_count"] >= 1, "GFM 表必须被识别为真实表集合"
        assert check["undetermined"] is True, "只有表却引用了图号：不得返回「通过」"
        assert check["reason"] == "no_fig_set"
        # 无法判定不产生 finding、不扣分（与 B-17 既有语义一致）
        assert not [i for i in result["issues"] if i.get("type") == "stale_cross_reference"]

    async def test_table_only_without_fig_ref_stays_determined(self, db_conn):
        await _seed(db_conn, TABLE_ONLY_NO_FIG_REF)
        result = await E.collect_export_issues(SCHEME, db_conn, strict_read=True)
        check = result["crossref_check"]
        assert check["tbl_count"] >= 1
        assert check["undetermined"] is False, "正文没有图号引用时不应误报无法判定"

    async def test_chart_only_with_fig_ref_stays_determined(self, db_conn):
        await _seed(db_conn, CHART_ONLY_WITH_FIG_REF)
        result = await E.collect_export_issues(SCHEME, db_conn, strict_read=True)
        check = result["crossref_check"]
        assert check["fig_count"] >= 1
        assert check["undetermined"] is False, "图集合非空、正文引用可核对，必须判为可判定"

    async def test_no_chart_set_keeps_b17_reason(self, db_conn):
        await _seed(db_conn, "正文描述施工流程，详见 图9-9 与 表3-2。\n")
        result = await E.collect_export_issues(SCHEME, db_conn, strict_read=True)
        check = result["crossref_check"]
        assert check["fig_count"] == 0 and check["tbl_count"] == 0
        assert check["undetermined"] is True
        # B-17 既有 reason 取值与前端/历史测试契约不变
        assert check["reason"] == "no_chart_set"


async def _seed_two_chapters(db, scheme_id: str, *, drift: bool) -> None:
    """两章平级目录：正常时 outline_json.id 为 1/2；drift 时第 2 章被改成 1。"""
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (scheme_id, PROJ, "编号漂移样本")
    )
    for sid, title, sort, oj in (
        (f"{scheme_id}-a", "第一章 工程概况", 1, '{"id":"1","level":1}'),
        (
            f"{scheme_id}-b",
            "第二章 施工部署",
            2,
            '{"id":"1","level":1}' if drift else '{"id":"2","level":1}',
        ),
    ):
        await db.execute(
            "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
            "word_status,word_budget,status,level,sort_order,parent_id,review_status,outline_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, scheme_id, PROJ, title, "", 0, "ok", 100, "generated", 1, sort, "", "pending", oj),
        )
    await db.commit()


class TestDlv13SeesOutlineStructureDrift:
    """DLV-13 结构漂移盲区：30 组「第 2 章存储编号被改成 1」。

    修复前：DLV-13 只调 validate_scheme_content_numbering（只解析正文），
    结构漂移 30/30 漏检；修复后：只读复用 sections.py::_outline_structure_report，
    30/30 命中。
    """

    async def test_30_of_30_structure_drift_detected(self, db_conn):
        detected = 0
        for i in range(30):
            sid = f"r183-num-{i}"
            await _seed_two_chapters(db_conn, sid, drift=True)
            findings, checked = await C._numbering_consistency_findings(db_conn, sid)
            assert checked is True
            if any(
                f["rule_id"] == "DLV-13" and "结构漂移" in f["detail"] for f in findings
            ):
                detected += 1
        assert detected == 30, f"结构漂移漏检 {30 - detected}/30"

    async def test_consistent_outline_reports_no_dlv13(self, db_conn):
        await _seed_two_chapters(db_conn, "r183-num-ok", drift=False)
        findings, checked = await C._numbering_consistency_findings(db_conn, "r183-num-ok")
        assert checked is True
        assert findings == [], "结构一致时不得误报 DLV-13"


class TestQualityWhitespaceBoundary:
    """✅ R184（2026-11-07）：纯空白正文不得算「已有正文 / 已扫描」。

    旧谓词 `COALESCE(content,'')!=''` 把「空格 / 换行 / 制表符 / 全角空格 / NBSP」
    算成有正文：质量自检 checked 虚高、空章节被报成「已覆盖」，正是「没检查显示成
    没问题」。现与 preflight_engine.content_coverage_stats 的 `.strip()` 对齐。
    """

    async def test_whitespace_only_is_not_checked(self, db_conn):
        from app.routers.sections import sections_quality

        sid = "r184-q"
        rows = [
            ("a", "A 纯空白", "   \n\t ", "", 1),
            ("a2", "A2 全角空格", "\u3000\u3000", "", 2),
            ("b", "B 父节点空白", "\n", "", 3),
            ("c", "C 叶子有正文", "正常正文。", "b", 4),
            ("d", "D 空字符串", "", "b", 5),
        ]
        for rid, title, content, parent, sort in rows:
            await db_conn.execute(
                "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
                "word_status,word_budget,status,level,sort_order,parent_id,review_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, sid, PROJ, title, content, len((content or "").strip()), "ok", 100,
                 "generated", 1, sort, parent, "pending"),
            )
        await db_conn.commit()

        result = await sections_quality(sid, db=db_conn)
        summary = result["summary"]
        # 只有 C 有真实正文；a/a2 是叶子但无正文，故计入分母不计数
        assert summary["checked"] == 1
        assert summary["section_total"] == 4  # 叶子 a/a2/c/d
        assert summary["unchecked"] == 3
        assert result["items"] == []


class TestProgramCheckerCrashIsUndetermined:
    """缺陷 3：检查项异常 = 没检查，不得放行。"""

    async def test_checker_crash_marks_partial_and_unreleased(self, db_conn, monkeypatch):
        await _seed(db_conn, "脚手架搭设与验收按本方案执行。" * 60)

        def _boom(ctx):  # noqa: ANN001 - 模拟 checker 内部崩溃
            raise RuntimeError("R183 模拟 check_deliverability 崩溃")

        monkeypatch.setattr(PE, "check_deliverability", _boom)
        payload = await C._readiness_overview_compute(db_conn, SCHEME, persist=False)

        # 故障本身仍作为 engine_error 条目可见（用于定位是哪个 checker）
        assert any(bool(f.get("engine_error")) for f in payload["findings"])
        # 但整体必须进入「无法判定」：不完整 + 不放行
        assert payload["incomplete"] is True
        assert payload["released"] is False
        assert "program" in payload["partial_sources"]
        # 无阻断项时 verdict 必须显式写明「检查不完整」；已有阻断项时 verdict
        # 走「需整改」文案（语义已是不放行），此时至少要有可见阻断项摘要。
        if not payload["blocked"]:
            assert "检查不完整" in payload["verdict"]
        else:
            assert payload["blockers"]


class TestSubheadingDetectorCrashIsUndetermined:
    """✅ R185（2026-11-07 · 三态口径）：检测器崩溃 ≠ 无冲突。

    旧实现（export.py::collect_export_issues）异常分支只写 debug 日志并继续，
    issues 里没有 ``body_subheading_namespace_conflict`` → 上层读成「零发现 /
    没问题」。现回传 ``subheading_check.undetermined``，总检据此计入
    ``partial_sources`` → incomplete → 不放行。
    """

    async def test_detector_crash_marks_check_undetermined(self, db_conn, monkeypatch):
        await _seed(db_conn, "正文描述施工流程。\n")

        def _boom(sections):  # noqa: ANN001 - 模拟检测器内部崩溃
            raise RuntimeError("R185 模拟子标题命名空间检测器崩溃")

        monkeypatch.setattr(E, "_detect_body_subheading_namespace_conflict", _boom)
        result = await E.collect_export_issues(SCHEME, db_conn, strict_read=True)

        check = result["subheading_check"]
        assert check["undetermined"] is True, "检测器崩溃不得返回「无冲突」这种干净态"
        assert check["reason"] == "detector_error"
        # 崩溃不产生 finding（无法判定不是不通过），但必须有可见的 undetermined 标记
        assert not [i for i in result["issues"] if i.get("type") == "body_subheading_namespace_conflict"]

    async def test_detector_crash_makes_overview_partial_and_unreleased(self, db_conn, monkeypatch):
        await _seed(db_conn, "脚手架搭设与验收按本方案执行。" * 60)

        def _boom(sections):  # noqa: ANN001
            raise RuntimeError("R185 模拟子标题命名空间检测器崩溃")

        monkeypatch.setattr(E, "_detect_body_subheading_namespace_conflict", _boom)
        payload = await C._readiness_overview_compute(db_conn, SCHEME, persist=False)

        assert payload["incomplete"] is True
        assert payload["released"] is False
        assert "export_check" in payload["partial_sources"]


class TestPreflightRunPersistsIncomplete:
    """✅ R185（2026-11-07）：执行时算出的 incomplete 必须随运行落库。

    旧实现只落 released/blocked，历史回看只剩「通过/不通过」，
    无法区分「当时完整通过」与「当时有一半没查」。口径要求：
    落库的就是执行时那一个值，读取端不得按来源列表重算。
    """

    async def test_executed_incomplete_is_persisted_and_listed(self, db_conn, monkeypatch):
        await _seed(db_conn, "脚手架搭设与验收按本方案执行。" * 60)

        def _boom(ctx):  # noqa: ANN001
            raise RuntimeError("R185 模拟 checker 崩溃")

        monkeypatch.setattr(PE, "check_deliverability", _boom)
        payload = await C._readiness_overview_compute(db_conn, SCHEME, persist=True)
        assert payload["incomplete"] is True

        runs = await C.list_preflight_runs(SCHEME, limit=5, db=db_conn)
        item = runs["items"][0]
        assert item["incomplete"] == 1, "历史记录必须能显示「检查不完整」"
        assert "program" in item["partial_sources"]
        assert item["failed_sources"] == []

    async def test_persisted_value_wins_over_recomputation(self, db_conn):
        """落库值 = 执行时值：即使来源列表为空，也不得在读取端改判为「完整」。"""
        await _seed(db_conn, "正文描述施工流程。\n")
        await C._persist_run(
            db_conn,
            SCHEME,
            {
                "total": 90,
                "grade": "A",
                "verdict": "建议放行",
                "released": True,
                "blocked": False,
                # 执行时判为不完整，但原因未记录（模拟上游只给布尔值）
                "incomplete": True,
                "failed_sources": [],
                "partial_sources": [],
                "findings": [],
                "counts": {},
                "dimensions": [],
            },
            {},
        )
        runs = await C.list_preflight_runs(SCHEME, limit=5, db=db_conn)
        item = runs["items"][0]
        assert item["incomplete"] == 1, "读取端不得按空来源列表重算成「完整」"
        assert item["failed_sources"] == [] and item["partial_sources"] == []

    async def test_legacy_row_stays_unrecorded_instead_of_complete(self, db_conn):
        """本列上线前的历史行 → NULL（未记录），不得伪装成 0（完整）。"""
        await _seed(db_conn, "正文描述施工流程。\n")
        await db_conn.execute(
            "INSERT INTO preflight_runs(id,scheme_id,total,grade,verdict,released,blocked)"
            " VALUES(?,?,?,?,?,?,?)",
            ("legacy-run", SCHEME, 80, "B", "需整改", 0, 1),
        )
        await db_conn.commit()
        runs = await C.list_preflight_runs(SCHEME, limit=5, db=db_conn)
        item = runs["items"][0]
        assert item["incomplete"] is None
        assert item["failed_sources"] == [] and item["partial_sources"] == []
