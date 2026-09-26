"""「提取项目」来源位置（evidence）溯源 + 关联项目（scheme_name）回归测试。

覆盖（2026-09-23 展示方式变更的遗留项落地）：
1. build_evidence（markdown）：整句包含命中 → 记录 文档名/行号/标题路径/原文摘录；
2. build_evidence（锚点）：轻度改写但共享特征 token（C30 类规格号）仍能溯源；
3. build_evidence（json）：按字段值反查并携带 field 路径；「没有提及」不产证据；
4. 宁缺勿滥：匹配不上返回 []，"未提取到" 不产证据（绝不编造出处）；
5. _update_item_status：evidence 显式传入才写列（None 不动，成功重跑覆盖旧证据）；
6. 人工校正 PUT / 清空 DELETE：evidence 同步清零（旧证据对新内容不再成立）；
7. /results 与单项 GET：逐行附 scheme_name（来源方案名，关联项目展示口径）。

测试直接调用路由/服务函数（与 test_bid_analysis.py 风格一致）。
"""
import json
import uuid

import pytest

import app.routers.bid_analysis as ba
from app.services.bid_analysis_service import (
    build_evidence, build_evidence_json, is_missing_result,
)

# 与 _combine_doc_texts 的产物格式一致：`# 文档：xxx（分类：yyy）` 头分隔文档
_COMBINED = (
    "# 文档：招标文件.pdf（分类：招标文件）\n\n"
    "# 第一章 招标公告\n\n"
    "## 1.1 工程概况\n"
    "基坑深度 8.5m，支护形式为钻孔灌注桩，混凝土强度等级 C30。\n\n"
    "---\n\n"
    "# 文档：答疑纪要.docx（分类：澄清文件）\n\n"
    "塔吊型号为 QTZ80，臂长 55 米，共布置 2 台。\n"
)


# =========================================================================
# 一、build_evidence 纯函数
# =========================================================================
def test_build_evidence_markdown_containment_hit():
    """整句抄写的内容必须命中原文出处：文档名/行号/标题/摘录齐全。"""
    result = (
        "## 工程概况\n"
        "- 基坑深度 8.5m，支护形式为钻孔灌注桩，混凝土强度等级 C30。\n"
    )
    ev = build_evidence(result, "markdown", _COMBINED)
    assert ev, "逐字抄写的结果句必须能反查到出处"
    hit = ev[0]
    assert hit["doc"] == "招标文件.pdf"
    assert hit["quote"].startswith("基坑深度 8.5m")
    assert "工程概况" in hit["heading"]
    assert isinstance(hit["line"], int) and hit["line"] > 0
    # 行号口径：全文第 6 行（含分隔空行）
    assert hit["line"] == 6


def test_build_evidence_token_anchor_hit():
    """被改写但共享特征 token（QTZ80）的内容，靠数字锚点仍能溯源到答疑纪要。"""
    result = "现场拟采用塔吊 QTZ80 型号设备完成垂直运输任务安排。"
    ev = build_evidence(result, "markdown", _COMBINED)
    assert ev and ev[0]["doc"] == "答疑纪要.docx"


def test_build_evidence_json_field_paths():
    """json 项按字段值反查，证据需携带 field 路径；「没有提及」不产证据。"""
    content = json.dumps({
        "支护形式": "钻孔灌注桩，混凝土强度等级 C30",
        "开工日期": "没有提及",
    }, ensure_ascii=False)
    ev = build_evidence(content, "json", _COMBINED)
    assert ev, "有实义的字段值必须能溯源"
    assert ev[0]["field"] == "支护形式"
    assert all(e.get("field") != "开工日期" for e in ev)


def test_build_evidence_no_fabrication():
    """原文里找不到出处时如实留空 —— 宁缺勿滥，绝不编造来源。"""
    ev = build_evidence("本项目坐落于火星基地，采用反重力施工工艺保障进度。",
                        "markdown", _COMBINED)
    assert ev == []


def test_build_evidence_missing_marker_empty():
    """「未提取到」占位内容不产证据（与缺失判定同口径）。"""
    assert build_evidence("未提取到", "markdown", _COMBINED) == []
    assert build_evidence_json("", "markdown", _COMBINED) == ""


def test_build_evidence_respects_max_items():
    """候选再多也按 max_items 截断（成本护栏）。"""
    result = "\n".join(
        f"第{i}条：基坑深度 8.5m，支护形式为钻孔灌注桩，混凝土强度等级 C30。"
        for i in range(30))
    ev = build_evidence(result, "markdown", _COMBINED, max_items=5)
    assert len(ev) <= 5


def test_build_evidence_index_cache_rebuild_on_change():
    """索引单槽缓存以原文哈希为键：原文变化必须重建索引（不吃旧缓存）。"""
    other = "# 文档：另一份.pdf（分类：其他）\n\n地下室底板厚度 1200mm，属于大体积混凝土。\n"
    ev = build_evidence("底板厚度 1200mm 按大体积混凝土控制裂缝。",
                        "markdown", other)
    assert ev and ev[0]["doc"] == "另一份.pdf"


# =========================================================================
# 二、_update_item_status 的 evidence 写入契约
# =========================================================================
async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "深基坑专项方案"))
    await db.commit()
    return pid, sid


async def _get_row(db, pid: str, item_id: str) -> dict:
    cur = await db.execute(
        "SELECT * FROM bid_analysis_items WHERE project_id=? AND item_id=?",
        (pid, item_id))
    r = await cur.fetchone()
    return dict(r) if r else {}


@pytest.mark.asyncio
async def test_update_item_status_evidence_contract(db_conn):
    """evidence 显式传入才写列；None 不动；成功重跑以新值（含空串）覆盖旧证据。"""
    db = db_conn
    pid, _sid = await _seed_scheme(db)
    ev_json = json.dumps([{"doc": "招标文件.pdf", "line": 6}], ensure_ascii=False)

    # 新行 + evidence（INSERT 分支）
    await ba._update_item_status(db, pid, "schemeBasicInfo", "success",
                                 "内容一", "", evidence=ev_json)
    row = await _get_row(db, pid, "schemeBasicInfo")
    assert row["evidence"] == ev_json

    # running 传 None → 证据保留（在途项便于对照）
    await ba._update_item_status(db, pid, "schemeBasicInfo", "running")
    assert (await _get_row(db, pid, "schemeBasicInfo"))["evidence"] == ev_json

    # 成功重跑但本轮没匹配到 → 传空串必须覆盖旧证据（不允许陈旧证据残留）
    await ba._update_item_status(db, pid, "schemeBasicInfo", "success",
                                 "内容二", "", evidence="")
    assert (await _get_row(db, pid, "schemeBasicInfo"))["evidence"] == ""


@pytest.mark.asyncio
async def test_safe_evidence_never_raises():
    """_safe_evidence 是不阻断业务的包装：任何异常都收敛为空串。"""
    assert ba._safe_evidence("内容", "markdown", "") == ""
    assert ba._safe_evidence("", "markdown", "任意原文") == ""
    # output_type 非法也不抛（build_evidence 内部按 markdown 处理或直接不命中）
    out = ba._safe_evidence("基坑深度 8.5m 钻孔灌注桩", "unknown_type", _COMBINED)
    assert isinstance(out, str)


# =========================================================================
# 三、人工校正 / 清空 同步清证据；/results 附 scheme_name
# =========================================================================
@pytest.mark.asyncio
async def test_manual_edit_and_clear_invalidate_evidence(db_conn):
    """PUT/DELETE 后 evidence 必须清零 —— 旧出处对人工修改后的内容不再成立。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    await ba._update_item_status(db, pid, "schemeBasicInfo", "success",
                                 "AI 内容", "", evidence='[{"doc":"x"}]')

    res = await ba.update_single_result(
        "schemeBasicInfo", {"content": "## 人工修正内容"},
        scheme_id=sid, project_id="", db=db)
    assert res["item"]["source"] == "manual"
    assert res["item"]["evidence"] == ""

    await ba._update_item_status(db, pid, "schemeBasicInfo", "success",
                                 "AI 内容", "", evidence='[{"doc":"x"}]')
    res2 = await ba.clear_single_result(
        "schemeBasicInfo", scheme_id=sid, project_id="", db=db)
    assert res2["item"]["evidence"] == ""


@pytest.mark.asyncio
async def test_results_and_single_get_attach_scheme_name(db_conn):
    """查询端点逐行附来源方案名；未记录 scheme_id 的行为空且不报错。"""
    db = db_conn
    pid, sid = await _seed_scheme(db)
    # 直接写带 scheme_id + evidence 的行（模拟 SSE 运行体落库）
    await db.execute(
        "INSERT INTO bid_analysis_items "
        "(id, project_id, scheme_id, item_id, label, output_type, required, "
        " status, content, sort_order, source, evidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{pid}_schemeBasicInfo", pid, sid, "schemeBasicInfo", "方案级基本信息",
         "markdown", 1, "success", "深基坑支护方案。", 2, "ai",
         '[{"doc":"招标文件.pdf","line":6}]'))
    await db.commit()

    res = await ba.list_analysis_results(scheme_id="", project_id=pid, db=db)
    item = next(i for i in res["items"] if i["item_id"] == "schemeBasicInfo")
    assert item["scheme_name"] == "深基坑专项方案"
    assert json.loads(item["evidence"])[0]["doc"] == "招标文件.pdf"

    single = await ba.get_single_result(
        "schemeBasicInfo", scheme_id="", project_id=pid, db=db)
    assert single["scheme_name"] == "深基坑专项方案"
