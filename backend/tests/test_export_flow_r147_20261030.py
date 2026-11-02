# -*- coding: utf-8 -*-
"""R147 两轮次导出护栏：检查器逐类检出 / 修复收敛 / 回滚 / 任务化流程 E2E。

用户指令 §五 验证矩阵：
- 构造错误初稿验证修复能力（docx_broken_fixtures，16 类可独立注入缺陷）；
- 修复失败回滚初稿（fallback_draft 契约）；
- 所有检查和修复动作记录日志（RepairReport.actions 全量留痕）；
- 源码锚一律断**调用形态**（R116/R144 教训：关键字锚会被注释满足）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import zipfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))

from app.services.docx_namespace import check_docx_file  # noqa: E402
from app.services.docx_repair import inspector as insp  # noqa: E402
from app.services.docx_repair import run_two_round_repair  # noqa: E402
from docx_broken_fixtures import (  # noqa: E402
    ALL_DEFECTS,
    build_broken_docx,
    build_clean_docx,
    tiny_png,
)

#: 缺陷 → 期望检出的 finding code（单缺陷注入必命中）
_DEFECT_CODE = {
    "level_jump": "num_level_jump",
    "num_seq": "num_sequence",
    "no_indent": "para_no_indent",
    "bad_align": "para_bad_align",
    "num_conflict": "para_num_conflict",
    "no_eastasia": "font_no_eastasia",
    "sz_odd": "font_sz_odd",
    "heading_italic": "heading_italic",
    "broken_media": "fig_broken_media",
    "not_centered": "fig_not_centered",
    "oversize": "fig_oversize",
    "caption_fmt": "fig_caption_format",
    "seq_gap": "fig_seq_gap",
    "missing_rel": "compat_missing_rel",
    "content_type": "compat_missing_content_type",
    # R149 真实形态
    "l3_depth": "num_sequence",
    "rel_break": "compat_broken_rel_target",
    "mixed_font": "font_ascii_cjk",
}
#: 真实稿「伪标题」形态：Heading6 列表项 / 列表拍平 → 不得产生任何编号 finding
_NO_FINDING_DEFECTS = ("heading6_fake", "inline_list")


def _codes(findings):
    return {f.code for f in findings}


# ---------------------------------------------------------------------------
# A. 检查器：逐类缺陷必检出（无跳变场景下 num_sequence 才可独立检出）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("defect,code", sorted(_DEFECT_CODE.items()))
def test_a1_inspector_detects_each_defect(tmp_path, defect, code):
    p = str(tmp_path / "d.docx")
    build_broken_docx(p, {defect})
    findings = insp.inspect_parts(insp.zip_to_parts(p))
    assert code in _codes(findings), \
        f"缺陷 {defect} 未检出（实得：{sorted(_codes(findings))}）"


@pytest.mark.parametrize("defect", _NO_FINDING_DEFECTS)
def test_a1b_pseudo_headings_no_numbering_finding(tmp_path, defect):
    """Heading6 列表项 / 列表拍平伪标题：不得产生任何编号类 finding。"""
    p = str(tmp_path / "d.docx")
    build_broken_docx(p, {defect})
    parts = insp.zip_to_parts(p)
    from app.services.docx_repair.pipeline import _sanitize_parts_inplace
    _sanitize_parts_inplace(parts)  # 与管线同序：w14/w15 预清理后才是检查基线
    findings = insp.inspect_parts(parts)
    bad = _codes(findings) & {
        "num_sequence", "num_level_jump", "num_outline_mismatch",
        "num_scheme_unknown"}
    assert not bad, f"{defect} 误报编号类 finding：{sorted(bad)}"
    assert [f for f in findings if f.severity == "error"] == [], \
        [f.to_dict() for f in findings if f.severity == "error"]


def test_a2_clean_docx_zero_error_baseline(tmp_path):
    """零误报基线：合法初稿（经管线同款部件预清理后）检查零 error。"""
    p = str(tmp_path / "c.docx")
    build_clean_docx(p)
    parts = insp.zip_to_parts(p)
    from app.services.docx_repair.pipeline import _sanitize_parts_inplace
    _sanitize_parts_inplace(parts)  # 与 run_two_round_repair 同序
    findings = insp.inspect_parts(parts)
    errs = [f for f in findings if f.severity == "error"]
    assert errs == [], [f.to_dict() for f in errs]


def test_a3_ns_decl_pre_sanitized_not_flagged(tmp_path):
    """w15 声明在部件预清理后不再误报（管线自洽性；终稿由 sanitize 兜底）。"""
    p = str(tmp_path / "n.docx")
    build_broken_docx(p, {"ns_decl"})
    parts = insp.zip_to_parts(p)
    from app.services.docx_repair.pipeline import _sanitize_parts_inplace
    changed = _sanitize_parts_inplace(parts)
    assert changed, "预清理未命中 w15 声明"
    assert "compat_ns_decl" not in _codes(insp.inspect_parts(parts))


# ---------------------------------------------------------------------------
# B. 修复收敛：全缺陷初稿 → 终稿合法 / 命名空间干净 / 动作全留痕
# ---------------------------------------------------------------------------

def test_b1_full_defect_repair_convergence(tmp_path):
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, set(ALL_DEFECTS))
    rep = run_two_round_repair(
        draft, final, agent_repair=False,
        media_sources={"word/media/image1.png": tiny_png()},
        # 夹具实际层级（heading_italic 另插 H1；含 H1→H3 跳变；
        # Heading6/列表项伪标题不计入）
        expected_outline=[1, 1, 3, 2, 2, 3, 3, 1, 2],
        emit=lambda e: None)
    # 终稿可重开 + zip 完整 + 命名空间干净（WPS/Office 兼容性程序化判据）
    from docx import Document
    Document(final)  # 不抛即合法
    assert check_docx_file(final)["clean"], check_docx_file(final)["issues"]
    assert rep["checks"]["compatibility"]["zip_intact"] is True
    # 唯一不可程序修复项 = 结构性层级跳变（不能凭空造中间标题）
    unresolved_codes = {u["code"] for u in rep["unresolved"]}
    assert unresolved_codes == {"num_level_jump"}, rep["unresolved"]
    # 全部动作留痕（用户指令 §三）
    assert rep["actions"], "修复动作未留痕"
    fixed_codes = {a["code"] for a in rep["actions"] if a["result"] == "fixed"}
    assert {"para_format", "font_fix", "fig_fix",
            "compat_missing_rel", "compat_missing_content_type"} <= fixed_codes


def test_b2_renumber_without_level_jump(tmp_path):
    """无跳变时编号虚跳强制重排：1.9 → 1.2（安全阀不触发）。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"num_seq"})
    rep = run_two_round_repair(draft, final, agent_repair=False,
                               emit=lambda e: None)
    with zipfile.ZipFile(final) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "1.2 施工部署" in doc and "1.9 施工部署" not in doc
    assert rep["summary"]["unresolved"] == 0
    seq_actions = [a for a in rep["actions"] if a["code"] == "num_sequence"]
    assert seq_actions and seq_actions[0]["result"] == "fixed"


def test_b3_broken_media_without_source_stays_unresolved(tmp_path):
    """损坏图无源字节：不伪造内容 → unresolved（由调用方决定回滚）。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"broken_media"})
    rep = run_two_round_repair(draft, final, agent_repair=False,
                               emit=lambda e: None)
    assert "fig_broken_media" in {u["code"] for u in rep["unresolved"]}


def test_b4_repair_failure_rolls_back_to_draft(tmp_path, monkeypatch):
    """修复链异常 → 异常上抛（由 export_flow 回滚初稿，绝不交付半成品）。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_clean_docx(draft)
    import app.services.docx_repair.pipeline as pl
    monkeypatch.setattr(pl, "_sanitize_parts_inplace",
                        lambda parts: (_ for _ in ()).throw(RuntimeError("修复爆炸")))
    with pytest.raises(RuntimeError):
        run_two_round_repair(draft, final, agent_repair=False, emit=lambda e: None)
    assert not os.path.exists(final), "失败修复不得留下半成品终稿"


def test_b5_realistic_forms_converge(tmp_path):
    """真实稿形态（伪标题 / L3 深度错配 / 列表拍平 / 中英混杂）→ 编号清零、可重开。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"heading6_fake", "l3_depth", "inline_list",
                              "mixed_font", "caption_fmt"})
    rep = run_two_round_repair(draft, final, agent_repair=False,
                               emit=lambda e: None)
    from docx import Document
    Document(final)
    with zipfile.ZipFile(final) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "1.3.2 资源配置" not in doc, "深度错配编号未重排"
    assert "宋体\" w:hAnsi=\"宋体" not in doc, "中英混杂西文槽未修正"
    s = rep["summary"]
    assert s["detected"] > 0 and s["fixed"] > 0
    assert s["fixed"] + s["skipped"] + s["manual"] == s["detected"]


def test_b6_category_rollback_isolation(tmp_path, monkeypatch):
    """单类修复引入回归 → 仅该类整类回滚，其它类成果不受影响。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"no_indent", "bad_align", "no_eastasia"})
    import app.services.docx_repair.pipeline as pl
    orig_fonts = pl._fixes.fix_fonts

    def broken_fonts(parts, findings, **kw):
        changed, acts, unres = orig_fonts(parts, findings, **kw)
        # 人为制造回归：给标题 run 注入倾斜 → 字体类复检 error 必然上升
        xml = parts["word/document.xml"].decode("utf-8")
        xml = re.sub(
            r"<w:r><w:t>(1\.2 施工部署)</w:t></w:r>",
            r"<w:r><w:rPr><w:i/></w:rPr><w:t>\1</w:t></w:r>", xml, count=1)
        parts["word/document.xml"] = xml.encode("utf-8")
        return changed, acts, unres

    monkeypatch.setattr(pl._fixes, "fix_fonts", broken_fonts)
    rep = run_two_round_repair(draft, final, agent_repair=False,
                              emit=lambda e: None)
    rollbacks = [a for a in rep["actions"] if a["code"] == "category_rollback"]
    assert rollbacks and rollbacks[0]["category"] == "fonts", rep["actions"]
    # 其它类（正文排版）成果保留
    assert any(a["code"] == "para_format" and a["result"] == "fixed"
               for a in rep["actions"])
    from docx import Document
    Document(final)  # 回滚后终稿仍可打开


def test_b7_fixed_equals_finding_coverage(tmp_path):
    """报告「已修复」= 初检存在且终检消失的 finding 覆盖数（可核对）。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"no_indent", "bad_align", "no_eastasia",
                              "caption_fmt"})
    from app.services.docx_repair.pipeline import _finding_key, _sanitize_parts_inplace
    pre = insp.zip_to_parts(draft)
    _sanitize_parts_inplace(pre)
    initial = insp.inspect_parts(pre)
    rep = run_two_round_repair(draft, final, agent_repair=False,
                              emit=lambda e: None)
    post = insp.zip_to_parts(final)
    _sanitize_parts_inplace(post)
    final_keys = {_finding_key(f) for f in insp.inspect_parts(post)}
    expected_fixed = sum(1 for f in initial if _finding_key(f) not in final_keys)
    s = rep["summary"]
    assert s["detected"] == len(initial)
    assert s["fixed"] == expected_fixed
    assert s["fixed"] + s["skipped"] + s["manual"] == s["detected"]


def test_b8_unresolved_aggregated_by_code(tmp_path):
    """需人工项按 code 聚合：count + ≤3 条样本 + 处置建议。"""
    d = str(tmp_path)
    draft = os.path.join(d, "draft.docx")
    final = os.path.join(d, "final.docx")
    build_broken_docx(draft, {"level_jump"})
    rep = run_two_round_repair(draft, final, agent_repair=False,
                               emit=lambda e: None)
    groups = rep["summary"]["unresolved_by_code"]
    assert groups, rep["summary"]
    g = groups[0]
    assert g["code"] == "num_level_jump"
    assert g["count"] >= 1 and len(g["samples"]) <= 3 and g["suggestion"]


# ---------------------------------------------------------------------------
# C. 源码锚：断**调用形态**（R116/R144 教训）
# ---------------------------------------------------------------------------

def test_c1_pipeline_call_shape_anchors():
    import inspect

    import app.services.docx_repair.pipeline as pl
    src = inspect.getsource(pl)
    assert "_sanitize_parts_inplace(parts)" in src          # 部件预清理真调用
    assert "_insp.inspect_parts(parts" in src               # 检查器真调用
    assert "_fixes.write_parts(final_path, parts)" in src   # 落盘真调用
    assert "sanitize_docx_file(final_path)" in src          # 兼容清理真调用
    assert "check_docx_file(final_path)" in src             # 落盘后校验真调用


def test_c2_agent_scene_registered_and_call_shape():
    """新场景 export_docx_repair 已登记 KNOWN_SCENES（R136 双向漂移护栏同口径）；
    agent_fix 走 collect_json_response 且 scene 指定（调用形态锚）。"""
    import inspect

    from app.services.ai.provider_factory import KNOWN_SCENES
    assert "export_docx_repair" in KNOWN_SCENES
    import app.services.docx_repair.agent_fix as ag
    src = inspect.getsource(ag)
    assert 'collect_json_response(' in src
    assert 'scene="export_docx_repair"' in src
    # Agent 永不直接产出 XML：只回白名单决策，替换对由本地计算
    assert "_replace_text_token" in src
    assert "_SCHEMES = " in src


def test_c3_flow_router_call_shape_anchors():
    import inspect

    import app.routers.export_flow as ef
    src = inspect.getsource(ef)
    # 两轮次衔接：第一轮真调既有构建链，第二轮真调修复管线
    # （调用形态：run_two_round_repair 以位置实参传入 asyncio.to_thread）
    assert "_build_docx_sync, *_build_docx_task(" in src
    assert "run_two_round_repair," in src
    assert "_tr.register_task(TASK_TYPE" in src             # 任务注册真调用
    assert "_rollback_to_draft(" in src                     # 回滚路径存在
    assert "EXPORTS_DIR / f\"{task_id}.draft.docx\"" in src  # 初稿临时文件
    assert "os.replace(draft_path, target)" in inspect.getsource(
        ef._rollback_to_draft)                              # 回滚=原子改名
    # R149：初稿清单落盘 + jsonl 双留痕 + 三段进度 + 字体角色透传
    assert "draft.manifest.json" in src
    assert "docx_repair" in src and ".jsonl" in src
    assert 'stage": "packaging"' in src
    assert 'east_asia_font=manifest["font_name"]' in src
    assert 'heading_font=manifest["heading_font"]' in src


# ---------------------------------------------------------------------------
# D. 任务化流程 E2E（monkeypatch 重型导出内部；task_registry 走真实内存库）
# ---------------------------------------------------------------------------

def _fake_prep():
    return {
        "scheme": {"id": "s1", "name": "测试方案"},
        "config": {},
        "roots": [{"id": "a", "level": 1}, {"id": "d", "level": 1}],
        "children_map": {
            "a": [{"id": "b", "level": 2}, {"id": "c", "level": 2}],
            "d": [{"id": "e", "level": 2}],
        },
        "chart_lookup": {}, "rendered_bytes": {}, "blocks_cache": {},
        "docx_options": {}, "heading_styles": {},
        "global_facts": [], "image_bytes": {}, "appendix_sources": [],
        "fences_fixed": 0,
    }


@pytest_asyncio.fixture
async def flow_env(tmp_path, monkeypatch, db_conn):
    """两轮次导出流程的测试环境：重型导出内部件全部替换为可控桩。"""
    import app.routers.export_flow as ef

    async def fake_get_conn():
        return db_conn

    monkeypatch.setattr(ef, "get_conn", fake_get_conn)
    monkeypatch.setattr(ef, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(ef, "_prepare_export", _fake_prep_async)
    monkeypatch.setattr(ef, "_prepare_export_render", _fake_render_async)
    monkeypatch.setattr(ef, "_bump_export_round", _fake_round_async)
    monkeypatch.setattr(ef, "_build_docx_task",
                        lambda out_path, prep: (str(out_path),))
    monkeypatch.setattr(ef, "_content_fingerprint", lambda prep: ("cfg12345678", "cnt12345678"))
    monkeypatch.setattr(ef, "_display_scheme_name", lambda scheme, gf: "测试方案")
    monkeypatch.setattr(ef, "_build_export_filename",
                        lambda name, ext, rnd: f"{name}_第{rnd}轮.docx")
    monkeypatch.setattr(ef, "_export_name_headers", lambda fn, rnd: {})
    cache_rows: list = []
    finalized: list = []

    async def fake_persist(*a, **kw):
        cache_rows.append(kw)

    async def fake_finalize(*a, **kw):
        finalized.append(kw)

    monkeypatch.setattr(ef, "_persist_export_cache_row", fake_persist)
    monkeypatch.setattr(ef, "_finalize_export_delivery", fake_finalize)
    return {"ef": ef, "cache_rows": cache_rows, "finalized": finalized,
            "tmp": tmp_path}


async def _fake_prep_async(scheme_id, body, db, cancel_event=None):
    return _fake_prep()


async def _fake_render_async(prep):
    return prep


async def _fake_round_async(db, scheme_id):
    return 1


def _make_build_docx(clean: bool):
    def _build(out_path: str, *_a, **_kw):
        if clean:
            build_clean_docx(out_path)
        else:
            build_broken_docx(out_path, set(ALL_DEFECTS))
        return {"sections": 2}

    return _build


async def _wait_terminal(ef, task_id, timeout=30.0):
    st = ef._state(task_id)
    for _ in range(int(timeout / 0.05)):
        if st["terminal"]:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError("流程 30s 未到终态")


@pytest.mark.asyncio
async def test_d1_flow_happy_path_two_stages(flow_env, monkeypatch):
    ef = flow_env["ef"]
    monkeypatch.setattr(ef, "_build_docx_sync", _make_build_docx(clean=True))
    resp = await ef.export_docx_v2("s1", {})
    assert resp.status_code == 202
    task_id = json.loads(resp.body)["task_id"]
    st = await _wait_terminal(ef, task_id)
    assert st["mode"] == "fixed"
    assert st["report"]["summary"]["unresolved"] == 0
    # 两段进度：初稿事件与修复事件都在流里
    stages = {e.get("stage") for e in st["buffer"]}
    assert {"draft", "repair"} <= stages
    # 初稿临时文件已清理（成功路径）
    draft_file = flow_env["tmp"] / f"{task_id}.draft.docx"
    assert not draft_file.exists()
    # 缓存 + 留痕收口都被调用
    assert flow_env["cache_rows"], "缓存行未写入"
    assert flow_env["finalized"], "交付收口未执行"
    # 下载可用
    fr = await ef.export_flow_download("s1", task_id)
    assert os.path.exists(fr.path)


@pytest.mark.asyncio
async def test_d2_flow_repair_failure_falls_back_to_draft(flow_env, monkeypatch):
    ef = flow_env["ef"]
    monkeypatch.setattr(ef, "_build_docx_sync", _make_build_docx(clean=True))

    def boom(*a, **kw):  # 同步函数（经 asyncio.to_thread 调用）
        raise RuntimeError("第二轮修复爆炸")

    monkeypatch.setattr(ef, "run_two_round_repair", boom)
    resp = await ef.export_docx_v2("s1", {})
    task_id = json.loads(resp.body)["task_id"]
    st = await _wait_terminal(ef, task_id)
    # 回滚契约：交付初稿，绝不交付半修复产物
    assert st["mode"] == "fallback_draft"
    assert st["final_path"] and os.path.exists(st["final_path"])
    assert st["report"]["mode"] == "fallback_draft"
    assert "第二轮修复爆炸" in st["report"]["error"]
    fr = await ef.export_flow_download("s1", task_id)
    assert os.path.exists(fr.path)


@pytest.mark.asyncio
async def test_d3_flow_cancel_stops_between_stages(flow_env, monkeypatch):
    ef = flow_env["ef"]

    async def slow_prep(*a, **kw):
        await asyncio.sleep(0.6)
        return _fake_prep()

    monkeypatch.setattr(ef, "_prepare_export", slow_prep)
    monkeypatch.setattr(ef, "_build_docx_sync", _make_build_docx(clean=True))
    resp = await ef.export_docx_v2("s1", {})
    task_id = __import__("json").loads(resp.body)["task_id"]
    await asyncio.sleep(0.1)
    await ef.export_flow_cancel("s1", task_id)
    st = await _wait_terminal(ef, task_id)
    assert st["mode"] == "cancelled"
    assert not st.get("final_path"), "取消后不得有交付物"
