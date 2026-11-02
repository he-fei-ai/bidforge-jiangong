# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮编排：检查 → 修复 → 分类复检 → 回滚 → 落盘（R147/R149）。

职责（用户指令 §三/§四）：
- 按类别顺序执行 C1→C5：检查（inspector）→ 程序精确修复（fixes）→
  **该类别复检**；若该类 error 数上升或出现新 code → **整类回滚**并标
  「需人工复核」（绝不带着新缺陷继续打包）；
- 程序修复两轮仍未收敛的 error 项 → Agent 辅助（agent_fix，可选）；
- 修复报告按 **finding 覆盖数** 统计（检出/已修复/跳过/需人工），
  未解决项按 code 聚合（count + 3 条样本 + 处置建议）；
- 落盘前 opening_gate：XML 可解析 / 关系目标存在 / 媒体 Content-Type /
  zip CRC / 命名空间干净 / python-docx 可重开。任一项失败 → 抛异常，
  由调用方回滚初稿（宁缺勿滥）。

本模块自身不落数据库、不发 SSE —— 通过 ``emit`` 回调把中间事件交给
调用方（export_flow 路由）转发。
"""
from __future__ import annotations

import logging
import re
import time
import zipfile
from typing import Callable

from app.services.docx_repair import fixes as _fixes
from app.services.docx_repair import inspector as _insp
from app.services.docx_repair.model import (
    CATEGORIES,
    SEV_ERROR,
    SEV_WARNING,
    RepairAction,
    RepairFinding,
    RepairReport,
)

logger = logging.getLogger("docx_repair")

#: emit 回调的事件形状：
#: {"stage": "repair", "phase": "inspect"|"fix"|"recheck"|"write"|"done",
#:  "category": str, "progress": float(40-92), "finding"?: dict, "action"?: dict}
EmitFn = Callable[[dict], None]

#: 进度分段（R149 三段式）：40–90 深度检查修复 / 90–100 打包兼容
PROGRESS_START = 40.0
PROGRESS_FIX_SPAN = 50.0
PROGRESS_WRITE = 90.0
PROGRESS_DONE = 92.0


def _noop_emit(_evt: dict) -> None:
    return None


def _error_findings(findings: list[RepairFinding]) -> list[RepairFinding]:
    return [f for f in findings if f.severity == SEV_ERROR]


def _finding_key(f: RepairFinding) -> tuple:
    return (f.code, f.location, str(f.meta.get("para_seq", "")))


#: 待清理的 XML 主部件（与 docx_namespace._PART_RE 同口径）
_PART_RE = re.compile(
    r"^word/(?:document|styles|settings|numbering|fontTable"
    r"|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def _sanitize_parts_inplace(parts: dict[str, bytes]) -> dict[str, list[str]]:
    """检查前对部件字典做 2010+ 扩展预清理（复用 docx_namespace 同一实现）。

    真实初稿在第一轮已被 sanitize_docx_file 清理过（幂等，零变化）；
    自洽性保障：即便绕过第一轮（测试/手工稿），第二轮的 C5 检查也不会
    对「将在落盘时被清理」的 w14/w15 声明误报为不可修复 error。
    """
    from app.services.docx_namespace import _sanitize_part_xml
    changed: dict[str, list[str]] = {}
    for name in list(parts):
        if not _PART_RE.match(name):
            continue
        xml = parts[name].decode("utf-8", errors="replace")
        new_xml, removed, n_attrs = _sanitize_part_xml(
            xml, name_is_settings=(name == "word/settings.xml"))
        if removed or n_attrs:
            parts[name] = new_xml.encode("utf-8")
            changed[name] = removed
    return changed


# ---------------------------------------------------------------------------
# 分类复检（与 inspector.inspect_parts 同口径，按类裁剪）
# ---------------------------------------------------------------------------

def _inspect_category(parts: dict[str, bytes], category: str,
                      expected_outline: list[int] | None) -> list[RepairFinding]:
    doc = parts.get("word/document.xml", b"").decode("utf-8", errors="replace")
    if category == "numbering":
        numbering = parts.get("word/numbering.xml")
        return _insp.inspect_numbering(
            doc, numbering.decode("utf-8", errors="replace") if numbering else None,
            expected_outline)
    if category == "paragraph":
        styles = parts.get("word/styles.xml")
        return _insp.inspect_paragraph(
            doc, styles.decode("utf-8", errors="replace") if styles else None)
    if category == "fonts":
        return _insp.inspect_fonts(doc)
    if category == "figures":
        return _insp.inspect_figures(doc, parts)
    return _insp.inspect_compatibility(parts)


def _run_fix(parts: dict[str, bytes], category: str,
             cat_findings: list[RepairFinding], *,
             media_sources: dict[str, bytes] | None,
             east_asia_font: str, heading_font: str | None
             ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    if category == "numbering":
        return _fixes.fix_numbering(parts, cat_findings)
    if category == "paragraph":
        return _fixes.fix_paragraph(parts, cat_findings)
    if category == "fonts":
        return _fixes.fix_fonts(
            parts, cat_findings, east_asia_font=east_asia_font,
            heading_font=heading_font)
    if category == "figures":
        return _fixes.fix_figures(
            parts, cat_findings, media_sources=media_sources)
    return _fixes.fix_compatibility(parts, cat_findings)


# ---------------------------------------------------------------------------
# opening_gate：交付前的可打开性闸门（宁缺勿滥）
# ---------------------------------------------------------------------------

def _check_xml_and_rel_targets(parts: dict[str, bytes]) -> list[str]:
    """写盘前结构闸：XML 可解析 + 关系目标存在 + 媒体 Content-Type 齐备。"""
    import xml.dom.minidom as _md
    failures: list[str] = []
    for name, data in parts.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            _md.parseString(data)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name} 不是合法 XML：{e}")

    rels_xml = parts.get("word/_rels/document.xml.rels", b"").decode(
        "utf-8", errors="replace")
    if rels_xml:
        rels = _insp._rels_map(rels_xml)
        for rid, info in rels.items():
            if info["external"] or not info["target"]:
                continue
            raw = info["target"].replace("\\", "/").lstrip("/")
            target = raw if raw.startswith("word/") else "word/" + raw
            path = re.sub(r"(?<=/)\./|(?<=^)\./", "", target)
            while True:
                m2 = re.search(r"[^/]+/\.\./", path)
                if not m2:
                    break
                path = path[:m2.start()] + path[m2.end():]
            if path not in parts:
                failures.append(f"关系 {rid} 目标部件缺失：{path}")

    ct = parts.get("[Content_Types].xml", b"").decode("utf-8", errors="replace")
    for name in parts:
        if not name.startswith("word/media/"):
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        if ext and f'Extension="{ext}"' not in ct:
            failures.append(f"媒体 {name} 缺少 Content-Type Default（{ext}）")
    return failures


def _opening_gate_file(final_path: str) -> list[str]:
    """落盘后闸门：zip CRC + 命名空间干净 + python-docx 可重开。"""
    failures: list[str] = []
    try:
        with zipfile.ZipFile(final_path) as z:
            bad = z.testzip()
            if bad:
                failures.append(f"zip CRC 校验失败：{bad}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"zip 不可读：{e}")
    try:
        from app.services.docx_namespace import check_docx_file
        ns = check_docx_file(final_path)
        if not ns.get("clean"):
            failures.append(f"专有命名空间残留：{ns.get('issues')}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"命名空间校验失败：{e}")
    try:
        from docx import Document
        Document(final_path)
    except Exception as e:  # noqa: BLE001
        failures.append(f"python-docx 无法重开：{e}")
    return failures


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_two_round_repair(
    draft_path: str,
    final_path: str,
    *,
    expected_outline: list[int] | None = None,
    media_sources: dict[str, bytes] | None = None,
    east_asia_font: str = "宋体",
    heading_font: str | None = None,
    agent_repair: bool = True,
    emit: EmitFn = _noop_emit,
    round1_stats: dict | None = None,
) -> dict:
    """对初稿执行第二轮检查与修复，产出终稿文件与修复报告（dict）。

    返回 ``RepairReport.to_dict()``。异常一律上抛 —— 调用方（export_flow）
    捕获后回滚初稿交付（mode=fallback_draft），绝不交付半修复产物。
    """
    t0 = time.monotonic()
    emit({"stage": "repair", "phase": "inspect", "category": "start",
          "progress": PROGRESS_START})
    parts = _insp.zip_to_parts(draft_path)

    report = RepairReport(round1_stats=dict(round1_stats or {}))

    # ---------- 预清理：部件级 2010+ 专有扩展（幂等；真实初稿此处零变化） ----------
    _ns = _sanitize_parts_inplace(parts)
    if _ns:
        report.actions.append(RepairAction(
            category="compatibility", code="compat_ns_sanitize",
            location="word/*.xml",
            message=f"剔除 Word 2010+ 专有扩展（部件：{sorted(_ns)}）",
            detail={"parts": sorted(_ns)}))

    initial = _insp.inspect_parts(parts, expected_outline=expected_outline)
    report.findings = list(initial)
    for f in initial:
        emit({"stage": "repair", "phase": "inspect", "category": f.category,
              "finding": f.to_dict()})

    pending_errors: list[RepairFinding] = []
    # ---------- 分类修复循环（最多 2 轮；每类修复后复检，回退有回归的类） ----------
    for round_no in (1, 2):
        findings = _insp.inspect_parts(parts, expected_outline=expected_outline)
        grouped = _insp.group_findings(findings)
        any_changed = False
        for idx, category in enumerate(CATEGORIES):
            cat_findings = grouped.get(category, [])
            progress = (PROGRESS_START
                        + (idx + 0.5) * (PROGRESS_FIX_SPAN / len(CATEGORIES)))
            if not cat_findings:
                report.checks[category] = {"found": 0, "fixed": 0,
                                           "recheck_clean": True}
                continue
            emit({"stage": "repair", "phase": "fix", "category": category,
                  "progress": progress,
                  "message": f"修复 {_fixes_catalog(category)}："
                             f"{len(cat_findings)} 项待处理"})
            snapshot = dict(parts)  # 类别级回滚快照（bytes 不可变，浅拷贝足够）
            before_codes = {f.code for f in cat_findings}
            before_err = len(_error_findings(cat_findings))
            changed, acts, unres = _run_fix(
                parts, category, cat_findings,
                media_sources=media_sources,
                east_asia_font=east_asia_font, heading_font=heading_font)
            # 该类修复后立即复检
            after = _inspect_category(parts, category, expected_outline)
            after_err = len(_error_findings(after))
            new_error_codes = {f.code for f in after if f.severity == SEV_ERROR}
            regression = after_err > before_err or (
                new_error_codes - before_codes)
            if regression:
                parts.clear()
                parts.update(snapshot)
                report.actions.append(RepairAction(
                    category=category, code="category_rollback",
                    location="document.xml",
                    message=(f"{_fixes_catalog(category)}修复后复检出现回归"
                             f"（error {before_err}→{after_err}，新增 code "
                             f"{sorted(new_error_codes - before_codes)}），已整类回滚"
                             "并标记需人工复核"),
                    result="failed",
                    detail={"before_errors": before_err,
                            "after_errors": after_err,
                            "new_codes": sorted(new_error_codes - before_codes)}))
                for a in acts:
                    a.result = "skipped"
                report.actions.extend(acts)
                report.checks[category] = {
                    "found": len(cat_findings), "fixed": 0,
                    "recheck_errors": before_err, "recheck_clean": False,
                    "rolled_back": True}
                continue
            report.actions.extend(acts)
            for a in acts:
                emit({"stage": "repair", "phase": "fix", "category": category,
                      "action": a.to_dict(), "progress": progress})
            if unres:
                pending_errors.extend(
                    u for u in unres if u.severity == SEV_ERROR)
            report.checks[category] = {
                "found": len(cat_findings),
                "recheck_errors": after_err,
                "recheck_findings": len(after),
                "recheck_clean": after_err == 0,
            }
            any_changed = any_changed or changed
        if not any_changed:
            break  # 第二轮无动作 → 已收敛（或全部不可程序修复）

    # ---------- Agent 辅助（复杂排版错乱；可选） ----------
    if agent_repair and pending_errors:
        try:
            from app.services.docx_repair.agent_fix import agent_fix_layout
            new_actions, still_unresolved = agent_fix_layout(
                parts, pending_errors, emit=emit)
            report.actions.extend(new_actions)
            pending_errors = still_unresolved
        except Exception as e:  # noqa: BLE001 —— Agent 失败不阻断程序修复成果
            logger.warning("Agent 辅助修复失败（保留程序修复成果）: %s", e)
            report.actions.append(RepairAction(
                category="numbering", code="agent_fix", location="document.xml",
                message=f"Agent 辅助修复不可用：{e}", result="skipped"))

    # ---------- 终检 + finding 覆盖统计（R149：fixed = 覆盖数） ----------
    final_findings = _insp.inspect_parts(
        parts, expected_outline=expected_outline)
    final_keys = {_finding_key(f) for f in final_findings}
    initial_keys = {_finding_key(f) for f in report.findings}

    fixed_findings: list[RepairFinding] = []
    skipped_findings: list[RepairFinding] = []
    unresolved_findings: list[RepairFinding] = []
    for f in report.findings:
        key = _finding_key(f)
        if key not in final_keys:
            fixed_findings.append(f)
        elif f.severity == SEV_WARNING:
            skipped_findings.append(f)
        else:
            unresolved_findings.append(f)
    # 终检新出现、初始没有的 error（非回滚路径的理论残留）→ 也计入需人工
    for f in final_findings:
        if f.severity == SEV_ERROR and _finding_key(f) not in initial_keys:
            unresolved_findings.append(f)
    for f in pending_errors:
        if _finding_key(f) not in {_finding_key(x) for x in unresolved_findings}:
            unresolved_findings.append(f)

    # 去重保序（两轮循环可能重复登记同类项）
    seen: set[tuple] = set()
    deduped_unresolved: list[RepairFinding] = []
    for f in unresolved_findings:
        key = _finding_key(f)
        if key not in seen:
            seen.add(key)
            f.meta["manual"] = True
            deduped_unresolved.append(f)
    report.unresolved = deduped_unresolved[:500]
    report.skipped = skipped_findings[:500]
    report.fixed_findings = fixed_findings[:1000]

    by_cat_final = _insp.group_findings(final_findings)
    by_cat_initial = _insp.group_findings(report.findings)
    for category in CATEGORIES:
        residue = by_cat_final.get(category, [])
        found = by_cat_initial.get(category, [])
        prev = report.checks.get(category) or {}
        report.checks[category] = {
            **prev,
            "found": len(found),
            "fixed": len([f for f in fixed_findings if f.category == category]),
            "recheck_findings": len(residue),
            "recheck_errors": len(_error_findings(residue)),
        }

    # ---------- 落盘前 opening_gate（结构闸） ----------
    gate_failures = _check_xml_and_rel_targets(parts)
    if gate_failures:
        raise RuntimeError("opening_gate 未通过（结构闸）：" + "；".join(
            gate_failures[:5]))

    emit({"stage": "repair", "phase": "write", "category": "compatibility",
          "progress": PROGRESS_WRITE, "message": "写盘并执行兼容性清理"})
    _fixes.write_parts(final_path, parts)
    try:
        from app.services.docx_namespace import sanitize_docx_file
        ns = sanitize_docx_file(final_path)
        if ns.get("parts_changed"):
            report.actions.append(RepairAction(
                category="compatibility", code="compat_ns_sanitize",
                location="word/*.xml",
                message=f"剔除 Word 2010+ 专有扩展（部件："
                        f"{sorted(ns['parts_changed'])}，属性 {ns['attrs_stripped']} 个）",
                detail={"parts": sorted(ns["parts_changed"]),
                        "attrs": ns["attrs_stripped"]}))
    except Exception as e:  # noqa: BLE001 —— 清理失败不阻断交付（宁多一个声明）
        logger.warning("第二轮：w14/w15 清理失败（不影响交付）: %s", e)

    # ---------- 落盘后闸门：zip CRC / 命名空间 / python-docx 重开 ----------
    file_failures = _opening_gate_file(final_path)
    if file_failures:
        try:
            import os
            os.unlink(final_path)
        except OSError:
            pass
        raise RuntimeError("opening_gate 未通过（文件闸）：" + "；".join(
            file_failures[:5]))
    report.checks["compatibility"] = {
        **report.checks.get("compatibility", {}),
        "ns_clean": True, "zip_intact": True,
    }

    report.mode = "fixed"
    report.duration_ms = int((time.monotonic() - t0) * 1000)
    emit({"stage": "repair", "phase": "done", "category": "done",
          "progress": PROGRESS_DONE, "report": report.to_dict()})
    return report.to_dict()


def _fixes_catalog(category: str) -> str:
    from app.services.docx_repair.model import CATEGORY_LABELS
    return CATEGORY_LABELS.get(category, category)
