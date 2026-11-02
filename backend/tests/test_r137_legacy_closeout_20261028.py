"""R137（2026-10-28）遗留问题收口专项护栏。

覆盖四组本轮修复，全部为「引入前必红、引入后必绿」的反向锚：

A 组 —— AI 场景登记缺口（R136 遗留 L7-a）
     `outline_agent` / `content_agent_repair` 两个真实调用点未登记进
     `provider_factory.KNOWN_SCENES` ⇒ 该路径只能共用主配置、无法单独指定
     模型（静默失效）。由 `TestKnownScenesDrift` 双向护栏在全量回归中查出。

B 组 —— 缺陷类 ruff 棘轮归零（R136 遗留 L7-b）
     三处死代码（F841 ×2 / F401 ×1）。此处只锚「别修回去」，
     全量扫描仍由 `test_static_hygiene_ratchet_r57` 的棘轮负责。

C 组 —— 检查点 `diag_json` 限长截出非法 JSON（R136 遗留 L4 · 真缺陷）
     旧实现对 JSON **字符串**直接 `[:4000]` 切片，超长即得到半截 JSON，
     读侧 `json.loads` 必 `ValueError` ⇒ 整个诊断被丢弃（不是只丢尾巴）。
     扫描件逐页 OCR 告警可达数百条，是本仓真实可触发路径。

D 组 —— 上传响应扫描件 + 本地 OCR 缺失前置告警（R136 遗留 L2）
     走既有 `warnings` 通道，前端 `workflowDerived` 已消费，零前端改动。

E 组 —— E2E 脚本可选依赖缺失时 skip（R136 遗留 L6）
     缺 Pillow/PyMuPDF 时应计「未执行」（E2E_EXIT=2）而不是「失败」。

F 组 —— 格式契约 fail-soft 静默失效的收尾修复（R137 全量回归新发现）
     `_check_formulas` 曾用 `10 ** 9` 当「未闭合块到文末」哨兵，构造十亿
     元素 set 抛 MemoryError → 被 `except Exception` 吞 → 整份自检降级为空报告。

⚠️ 刻意不新建测试数据库：C/D 组复用 `tmp_path` + `init_db()` 的既有口径。
"""
import importlib.util
import inspect
import io
import json
import re
import time
import uuid
from pathlib import Path

import app.routers.global_facts as gf
import app.routers.global_facts_docs as gf_docs
import app.services.ai.mermaid_flowchart as mflow
import app.services.ai.provider_factory as pf
import app.services.content_agent.repair as ca_repair
import app.services.content_agent.validation as ca_validation
import pytest
from app.db import get_conn, init_db
from starlette.datastructures import UploadFile

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A 组：AI 场景登记
# ---------------------------------------------------------------------------

def test_outline_agent_scenes_registered():
    """A1：工具调用链的两个 scene 必须在 KNOWN_SCENES 白名单内且有中文描述。

    双向漂移（少登记 / 白登记）由既有 `TestKnownScenesDrift` 负责，此处
    只补它不覆盖的一点：描述不能为空 —— 配置页靠它展示用途，空描述等于
    用户面对一条不知该配什么的选项。
    """
    for scene in ("outline_agent", "content_agent_repair"):
        assert scene in pf.KNOWN_SCENES, f"scene {scene!r} 未登记进 KNOWN_SCENES"
        desc = str(pf.KNOWN_SCENES[scene] or "").strip()
        assert desc, f"scene {scene!r} 的描述为空（配置页无法展示用途）"


def test_known_scenes_drift_guardrail_is_wired():
    """A2：双向漂移护栏自身必须存在且在岗（防止被误删后无人察觉）。

    本轮两个 scene 缺口正是由它查出（R136 全量回归），所以它必须长期
    在岗；此处只锁「存在两条方向断言」这个事实，扫描实现不重写。
    """
    import test_ai_config_security_routing as drift_mod

    cls = getattr(drift_mod, "TestKnownScenesDrift", None)
    assert cls is not None, "KNOWN_SCENES 双向漂移护栏类丢失"
    names = dir(cls)
    assert "test_all_used_scenes_registered" in names, "少登记方向护栏丢失"
    assert "test_registered_scenes_are_actually_used" in names, \
        "白登记方向护栏丢失（登记了却没人用 = 界面死选项）"


# ---------------------------------------------------------------------------
# B 组：死代码不得复活
# ---------------------------------------------------------------------------

def test_legacy_flowchart_renderer_has_no_dead_max_rows():
    """B1：legacy 渲染器只用 max_cols 算宽，总高走 _content_span。

    `max_rows = max_level + 1` 在该函数内从未被读取（F841）。注意
    `_render_flowchart_v2` 侧的 `max_rows` 是**真实使用**的（算 total_h），
    本断言只锁 legacy 渲染器，不可写成全文件级别。
    """
    src = inspect.getsource(mflow._render_flowchart_with_edges)
    # 只看「真赋值行」：本轮修复的说明注释里本身就引用了这串代码，
    # 用 `in` 判断会被自己的注释误伤。
    dead = re.findall(r"^\s*max_rows\s*=\s*max_level\s*\+\s*1\s*$",
                      src, re.M)
    assert not dead, "legacy 渲染器的死赋值复活（ruff F841 棘轮会红）"


def test_content_agent_no_dead_import_or_var():
    """B2：content_agent 的两处死代码不得复活（F401 死导入 / F841 死变量）。"""
    assert not hasattr(ca_repair, "Path"), \
        "repair.py 无用的 `from pathlib import Path` 复活（F401）"
    src = inspect.getsource(ca_validation)
    assert "expected_set = set(expected_ids)" not in src, \
        "validation.py 的死变量 expected_set 复活（F841）"
    # 反向：真正在用的两个派生量不得被误删
    assert "expected_ids" in src and "expected_files" in src


# ---------------------------------------------------------------------------
# C 组：检查点 diag_json 结构化裁剪
# ---------------------------------------------------------------------------

_SCALARS = ("file_type", "truncated", "page_count",
            "source_page_count", "text_len")


def _big_diag(n: int = 500) -> dict:
    return {
        "file_type": "pdf",
        "truncated": True,
        "page_count": 500,
        "source_page_count": 620,
        "text_len": 398_000,
        "warnings": [f"第 {i} 页为纯图片且 OCR 不可用，内容可能缺失"
                     for i in range(1, n + 1)],
    }


def test_diag_compact_is_always_valid_json():
    """C1（核心反向锚）：超长诊断也必须产出**可解析**的 JSON 且不超限。

    引入前：`json.dumps(...)[:4000]` ⇒ 半截字符串 ⇒ `json.loads` 抛
    ValueError ⇒ 4000 字以外的全部诊断（含标量键）一起丢失。
    """
    diag = _big_diag(500)
    raw = gf_docs._compact_diag_for_checkpoint(diag)
    assert isinstance(raw, str) and raw
    assert len(raw) <= gf_docs._CHECKPOINT_DIAG_LIMIT, \
        f"超长未裁剪：{len(raw)} > {gf_docs._CHECKPOINT_DIAG_LIMIT}"
    parsed = json.loads(raw)  # 引入前此处必抛 ValueError
    assert isinstance(parsed, dict)


def test_diag_compact_keeps_all_scalars():
    """C2：标量键是覆盖率/截断判定的输入，裁剪时一条都不许丢。"""
    diag = _big_diag(500)
    parsed = json.loads(gf_docs._compact_diag_for_checkpoint(diag))
    for key in _SCALARS:
        assert key in parsed, f"裁剪后丢失标量键 {key}"
        assert parsed[key] == diag[key], f"裁剪后 {key} 值被改动"


def test_diag_compact_truncates_by_item_and_reports_omitted():
    """C3：告警按**整条**裁剪（绝不切半句），并明确告知省略条数。

    切半句会让用户看到半截中文；静默少条会让「告警变少了」被误读成
    「问题变少了」—— 两者都必须避免。
    """
    diag = _big_diag(500)
    src_list = diag["warnings"]
    parsed = json.loads(gf_docs._compact_diag_for_checkpoint(diag))
    kept = parsed["warnings"]
    assert kept, "至少应保留 1 条告警"
    marker = kept[-1]
    assert "条解析告警未随断点保存" in marker, f"末条不是省略说明：{marker!r}"
    body = kept[:-1]
    # ① 每条都是原告警的完整原文（不切半句）
    for item in body:
        assert item in src_list, f"告警被切断或改写：{item!r}"
    # ② 保留 + 省略 == 原始条数（条数守恒，不得凭空吞掉）
    m = re.search(r"另有 (\d+) 条", marker)
    assert m, f"省略说明缺少条数：{marker!r}"
    assert len(body) + int(m.group(1)) == len(src_list), \
        f"条数不守恒：kept={len(body)} omitted={m.group(1)} total={len(src_list)}"


def test_diag_compact_roundtrip_through_restore():
    """C4（端到端）：compact 产物经 `_restore_diag_from_checkpoint` 仍能还原。

    写入侧修好但读侧白名单不认，等于没修 —— 故必须做一次往返。
    """
    diag = _big_diag(400)
    raw = gf_docs._compact_diag_for_checkpoint(diag)
    restored = gf_docs._restore_diag_from_checkpoint(
        {"diag_json": raw}, "persist", "<!-- page:1 -->\nx")
    for key in _SCALARS:
        assert restored[key] == diag[key], f"往返后 {key} 丢失/变更"
    assert restored["warnings"], "往返后告警全丢"
    assert restored["page_count"] == 500, "page_count 不得被正文页标记覆盖"


def test_diag_compact_small_payload_is_verbatim():
    """C5：未超限时逐字等价（裁剪只在超限时生效，不改变常规形态）。"""
    diag = {
        "file_type": "docx", "truncated": False, "page_count": 3,
        "source_page_count": 0, "text_len": 1200,
        "warnings": ["表格行数超上限，仅保留前 200 行"],
    }
    parsed = json.loads(gf_docs._compact_diag_for_checkpoint(diag))
    assert parsed == diag, f"常规诊断被意外改写：{parsed}"


def test_legacy_truncated_diag_degrades_without_crash(caplog):
    """C6：旧版留下的半截 diag_json → 降级重建，但必须留痕（不再静默）。

    历史行（旧写侧产物）依然可能存在；读侧不能抛，但也不能没有日志 ——
    否则「诊断全丢」在排障时毫无线索。
    """
    full = json.dumps(_big_diag(20), ensure_ascii=False)
    legacy_raw = full[:4000] if len(full) > 4000 else full[:-40]
    assert not legacy_raw.endswith("]}")
    with caplog.at_level("WARNING"):
        restored = gf_docs._restore_diag_from_checkpoint(
            {"diag_json": legacy_raw}, "persist", "<!-- page:1 -->\na")
    assert restored["page_count"] == 1, "非法 JSON 时必须按正文现算页数（不抛）"
    assert any("diag_json" in r.message for r in caplog.records), \
        "非法 diag_json 降级未留 WARNING 日志"


async def test_save_parse_checkpoint_writes_parseable_json(ctx):
    """C7（写路径实证）：走真实 `_save_parse_checkpoint` 落库，回读必须可解析。

    只测 compact helper 不够 —— 写入点若绕过它（例如改回 `[:4000]` 字符串
    切片），库里存的仍是半截 JSON，读侧照样整体丢失。本用例覆盖
    「写 → 库 → 读」全链，是 C1 的必要补充（A/B 变异 M1 证明）。
    """
    db, _pid, _sid = ctx
    doc_id = uuid.uuid4().hex
    diag = _big_diag(500)
    await gf_docs._save_parse_checkpoint(
        db, doc_id, "persist", "failed", "落库瞬时失败", "x" * 10, diag=diag)
    cur = await db.execute(
        "SELECT diag_json FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
    raw = dict(await cur.fetchone())["diag_json"]
    assert raw, "检查点未写入 diag_json"
    assert len(raw) <= gf_docs._CHECKPOINT_DIAG_LIMIT
    parsed = json.loads(raw)  # 引入前 `[:4000]` 切片 ⇒ 此处必抛 ValueError
    assert parsed["page_count"] == 500
    assert parsed["source_page_count"] == 620
    assert parsed["truncated"] is True
    restored = gf_docs._restore_diag_from_checkpoint(
        {"diag_json": raw}, "persist", "x" * 10)
    assert restored["page_count"] == 500
    assert restored["source_page_count"] == 620


# ---------------------------------------------------------------------------
# D 组：上传前置 OCR 告警
# ---------------------------------------------------------------------------

def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, size=len(data))


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    appdb = __import__("app.db", fromlist=["DB_PATH"])
    appdb.DB_PATH = tmp_path / "r137.sqlite"
    await init_db()
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await appdb.close_db()


def test_ocr_only_types_declared_without_pdf():
    """D1：只把「确定无文字层」的图像格式纳入判定，PDF 必须排除。

    PDF 有无文字层需打开才知道（可能是原生文本 PDF）—— 不打开就提前
    告警属误报，会稀释真正该关注的提示。
    """
    types = gf_docs._OCR_ONLY_TYPES
    assert {"png", "jpg", "jpeg", "bmp", "tiff", "tif"} <= types
    assert "pdf" not in types, "PDF 不得进 OCR-only 判定（会误报）"
    assert "docx" not in types and "txt" not in types


def test_local_ocr_available_fail_soft(monkeypatch):
    """D2：探测异常/失败时按「可用/不判定」处理，绝不因探测而阻断上传。"""
    import app.services.ocr as ocr_mod

    def _boom():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(ocr_mod, "tesseract_available", _boom)
    assert gf_docs._local_ocr_available() is True, \
        "探测异常必须 fail-soft 返回 True（advisory 档不得误报/阻断）"


def test_local_ocr_unavailable_when_disabled(monkeypatch):
    """D3：OCR 总开关关闭时即视为不可用（用户显式关闭也应有提示）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_enabled", False, raising=False)
    assert gf_docs._local_ocr_available() is False


async def test_upload_scanned_file_warns_when_ocr_missing(ctx, monkeypatch):
    """D4（功能实证）：上传扫描件 + 无本地 OCR → 响应 warnings 必须带提示。

    这是「上传当场告知」的核心行为：用户不必等解析完才发现引擎缺失。
    走既有 warnings 通道 ⇒ 前端 `workflowDerived.summarizeUploadResult`
    已消费并逐条 `msg.info`，故零前端改动即可见。
    """
    db, pid, _sid = ctx
    monkeypatch.setattr(gf_docs, "_local_ocr_available", lambda: False)
    tif = b"II*\x00" + b"\x00" * 64  # TIFF 文件头（签名校验通过即可，不解析）
    res = await gf_docs.upload_documents(
        scheme_id="", project_id=pid, files=[_upload("扫件.tif", tif)], db=db)
    assert res["saved_count"] == 1, f"上传被拒：{res}"
    joined = " | ".join(res.get("warnings") or [])
    assert "OCR" in joined, f"扫描件 + 无 OCR 未告警：{res.get('warnings')}"
    assert "扫件.tif" in joined, "提示未指名具体文件，用户无法定位"


async def test_upload_scanned_file_silent_when_ocr_ready(ctx, monkeypatch):
    """D5（反向）：OCR 可用时不得添这条告警（避免噪声）。"""
    db, pid, _sid = ctx
    monkeypatch.setattr(gf_docs, "_local_ocr_available", lambda: True)
    tif = b"II*\x00" + b"\x00" * 64
    res = await gf_docs.upload_documents(
        scheme_id="", project_id=pid, files=[_upload("扫件.tif", tif)], db=db)
    assert res["saved_count"] == 1
    joined = " | ".join(res.get("warnings") or [])
    assert "无文字层的图片/扫描件" not in joined, \
        f"OCR 可用时仍误报：{res.get('warnings')}"


async def test_upload_text_file_never_warns_about_ocr(ctx, monkeypatch):
    """D6（反向）：纯文本证据即使 OCR 缺失也不得告警（它不是扫描件）。"""
    db, pid, _sid = ctx
    monkeypatch.setattr(gf_docs, "_local_ocr_available", lambda: False)
    res = await gf_docs.upload_documents(
        scheme_id="", project_id=pid,
        files=[_upload("说明.txt", "工程名称：某某产业园".encode("utf-8"))], db=db)
    assert res["saved_count"] == 1
    joined = " | ".join(res.get("warnings") or [])
    assert "无文字层的图片/扫描件" not in joined, \
        f"文本文件被误判为扫描件：{res.get('warnings')}"


def test_frontend_consumes_upload_warnings():
    """D7（跨端契约锚）：前端必须仍在消费上传响应的 warnings 数组。

    后端加告警、前端不展示，等于没做 —— 这条锁住「零前端改动」成立的
    前提（一旦前端改成只读 details/saved，本告警就静默失效）。
    """
    ts = (BACKEND.parent / "frontend" / "src" / "utils" / "workflowDerived.ts")
    assert ts.is_file(), f"未找到前端折算模块：{ts}"
    src = ts.read_text(encoding="utf-8", errors="ignore")
    assert "d.warnings" in src, \
        "前端不再读取上传响应的 warnings（后端新增告警将不可见）"


# ---------------------------------------------------------------------------
# E 组：E2E 脚本可选依赖缺失 skip
# ---------------------------------------------------------------------------

def _current_e2e_script() -> Path:
    """当前最新 E2E 脚本（R136 起按轮次递进命名：_e2e_r136 → _e2e_r144）。

    护栏锚定「最新脚本」而非写死 R136 文件名：脚本随轮次更名属于正常演进，
    写死旧名会把演进误判成「脚本缺失」，反而掩盖真正的问题。
    """
    cands = sorted(BACKEND.glob("_e2e_r*.py"))
    return cands[-1] if cands else BACKEND / "_e2e_r136.py"


def test_e2e_script_skips_on_missing_optional_deps():
    """E1：缺 Pillow/PyMuPDF 时应显式 skip（E2E_EXIT=2），而非 ImportError 崩。

    「未执行」与「失败」必须可区分：否则未装可选包的机器上，一次正常的
    「跳过」看起来就像回归，CI 里会把人引向错误的排查方向。
    """
    script = _current_e2e_script()
    assert script.is_file(), f"未找到 E2E 脚本：{script}"
    src = script.read_text(encoding="utf-8", errors="ignore")
    assert "_missing_optional_deps" in src, "缺少可选依赖探测 helper"
    assert '"PIL"' in src and '"fitz"' in src
    assert "[SKIP]" in src, "缺少可读的跳过标记"
    assert "E2E_EXIT" in src, "缺少 E2E 退出码输出"
    assert "return 2" in src, "跳过时未给出独立退出码（与失败不可区分）"
    # 探测必须在造素材之前（否则先进素材构造就抛了）
    assert src.index("_missing_optional_deps()") < src.index(
        "tmp = Path(tempfile.mkdtemp"), "依赖探测晚于素材构造，形同虚设"
    # 早退分支（return 2）+ 素材目录构造（tmp = ...）都必须还在
    assert "tmp = Path(tempfile.mkdtemp" in src


def test_e2e_dep_probe_returns_pip_names():
    """E2：探测结果必须是 pip 可安装名（用户能直接复制命令去装）。"""
    script = _current_e2e_script()
    spec = importlib.util.spec_from_file_location(
        "_e2e_probe_current", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 只执行常量/函数定义，不跑 main()
    missing = mod._missing_optional_deps()
    assert isinstance(missing, list)
    assert all(isinstance(x, str) and x for x in missing)
    # 本机若已装齐，必须为空（反之说明探测口径把已装依赖误判为缺失）
    for modname, pkg in mod._OPTIONAL_DEPS:
        try:
            __import__(modname)
        except ImportError:
            assert pkg in missing
        else:
            assert pkg not in missing


# ---------------------------------------------------------------------------
# F 组：格式契约 fail-soft 静默失效（R137 全量回归发现，既有缺陷）
# ---------------------------------------------------------------------------

def test_unclosed_fence_still_runs_full_contract_check():
    """F1：含未闭合围栏的正文，自检必须真正跑完（不得整体降级为空报告）。

    回归锚：`_check_formulas` 曾用 `10 ** 9` 当「未闭合块到文末」哨兵，
    构造十亿元素 `set` 抛 MemoryError → 被 `check_format_contract` 的
    `except Exception` 吞掉 → `checked=False` + `issues=[]` —— 恰恰是
    「最需要自检」的未闭合围栏，反而什么都报不出来。
    """
    from app.services.content_format_contract import check_format_contract

    rep = check_format_contract("正文\n```mermaid\nflowchart TD\n  A-->B")
    assert rep["checked"] is True, "未闭合围栏导致整份自检降级为空报告"
    assert any(i["category"] == "chart" and i["severity"] == "error"
               and "未闭合" in i["message"]
               for i in rep["issues"]), rep["issues"]


def test_fence_protection_has_single_source():
    """F2：保护行集合只能有一份判据（三处共用 helper，且无裸哨兵）。

    该缺陷的成因就是「同一判据三份副本」，只改错的那一行等于留着下一次
    分叉的机会 ⇒ 锁「只有一份」，而不只锁「那一行是对的」。
    """
    import app.services.content_format_contract as cc

    src = inspect.getsource(cc)
    # 按**真赋值行**锚定，不做全文子串匹配 —— 说明注释里也会提到旧哨兵，
    # 全文匹配会被自己的注释误伤（R137 教训③）。
    bare = re.findall(r"^\s*stop = .*\b10 \*\* 9\b", src, re.M)
    assert not bare, f"裸哨兵复活（未闭合块会被展开成十亿行）：{bare}"
    sites = re.findall(
        r"^\s+protected = _protected_line_indexes\(content\)$", src, re.M)
    assert len(sites) == 3, (
        f"散文/表格/公式三处应共用同一 helper，实得 {len(sites)} 处")
    upstreams = re.findall(r"^\s+protected\.update\(range\(", src, re.M)
    assert len(upstreams) == 1, (
        f"就地拼 range 的副本又出现了（实得 {len(upstreams)} 处）："
        "新增消费方必须复用 _protected_line_indexes")


def test_protected_indexes_stop_at_real_last_line():
    """F3：未闭合块截到**真实末行**（不越界、但要盖到最后一行）。"""
    from app.services.content_format_contract import _protected_line_indexes

    content = "第一行\n```mermaid\nA-->B\nC-->D"
    idx = _protected_line_indexes(content)
    assert idx == {1, 2, 3}, idx
    assert max(idx) < len(content.split("\n")), "保护行集合越界（哨兵展开）"


def test_protected_indexes_bounded_on_large_content():
    """F4：大正文（2万行 + 未闭合围栏）下时间与集合规模都有界。

    它是 F1 的**性能侧**佐证：MemoryError 不一定每次先抛，也可能先慢到超时，
    两种表现都是同一个哨兵错误；所以规模与耗时都要锁。
    """
    from app.services.content_format_contract import _protected_line_indexes, check_format_contract

    body = "\n".join(f"第 {i} 段正文内容" for i in range(20000))
    content = body + "\n```mermaid\nflowchart TD\n  A-->B"
    n_lines = content.count("\n") + 1
    t0 = time.perf_counter()
    idx = _protected_line_indexes(content)
    rep = check_format_contract(content)
    cost = time.perf_counter() - t0
    assert len(idx) <= n_lines, f"保护行集合 {len(idx)} 超过总行数 {n_lines}"
    assert rep["checked"] is True
    assert cost < 10.0, f"大正文自检耗时 {cost:.2f}s（疑似仍展开巨量 range）"
