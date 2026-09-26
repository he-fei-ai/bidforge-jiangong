"""七模块跨模块深度探索 · 修复项回归（2026-09-24）

本轮修复的三处「有代码依据、且此前无任何用例锁定」的缺陷：

1. 完整性报告 project_info 必备字段口径与实际落库键名不符
   —— pipeline._REQUIRED_PROJECT_INFO_FIELDS 曾写
   project_code / location / client，而 bid_analysis_service 的
   projectBasicInfo 真实键为 project_number / project_location /
   construction_unit → field_coverage 恒 ≤0.4、missing_fields 恒报幻影字段。
2. 审核与预检模块三处 AI 调用漏传 scene（compliance_check / expert_review /
   consistency_audit）→ /ai/stats 场景聚合与「场景模型路由」双双失效。
3. 上传目录 AI 兜底识别漏传 scene（outline_recognition）。

用例口径：不抛异常 + 语义正确 + 反例回归（旧键名仍可用 = 向后兼容）。
"""
import json
import uuid

import pytest

from app.services.ai import provider_factory as pf
from app.services.doc_pipeline import pipeline


# ---------------------------------------------------------------------------
# 1. 完整性报告：project_info 字段覆盖率口径
# ---------------------------------------------------------------------------

def _mk_payload(fields: dict) -> str:
    """构造 sync_extract_layer 落库形状的 doc_extractions.extract_data。"""
    return json.dumps({
        "doc_id": "d1",
        "extract_time": "2026-09-24T00:00:00Z",
        "extract_schema_version": "extract-v1",
        # _merge_item_contents 会把字符串值归一为 {value, confidence, source}
        "project_info": {
            k: {"value": v, "confidence": 0.9, "source": ""}
            for k, v in fields.items()
        },
        "source_items": [],
    }, ensure_ascii=False)


async def _seed(db_conn, *, doc_id: str, project_id: str, extract_data: str | None):
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)",
                          (project_id, "p"))
    await db_conn.execute(
        "INSERT INTO project_documents(id, project_id, file_name, parse_status,"
        " page_count) VALUES(?,?,?,?,?)",
        (doc_id, project_id, "招标文件.pdf", "success", 10))
    if extract_data is not None:
        await db_conn.execute(
            "INSERT INTO doc_extractions(extraction_id, doc_id, project_id,"
            " extract_type, extract_data, status) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, doc_id, project_id, "project_info",
             extract_data, "success"))
    await db_conn.commit()


#: 18 项 projectBasicInfo 的真实键（见 bid_analysis_service._ITEM_PROMPTS）
_REAL_KEYS = {
    "project_name": "XX 大厦",
    "project_number": "XM-2026-001",
    "construction_unit": "XX 建设集团",
    "contractor": "XX 建筑公司",
    "project_location": "XX 市 XX 区",
}


async def test_field_coverage_full_with_real_bid_keys(db_conn):
    """正例：真实 18 项键名齐全 → 覆盖率 1.0，无缺失字段。"""
    pid, did = uuid.uuid4().hex, uuid.uuid4().hex
    await _seed(db_conn, doc_id=did, project_id=pid,
                extract_data=_mk_payload(_REAL_KEYS))
    rep = await pipeline.build_completeness_report(
        db_conn, doc_id=did, project_id=pid)
    comp = rep["completeness"]
    assert comp["required_fields"] == 5
    assert comp["extracted_fields"] == 5
    assert comp["field_coverage"] == 1.0
    assert comp["missing_fields"] == []


async def test_field_coverage_backward_compatible_with_legacy_alias(db_conn):
    """反例回归：历史库里的旧键名（project_code/client/location）仍须被承认。"""
    pid, did = uuid.uuid4().hex, uuid.uuid4().hex
    legacy = {
        "project_name": "XX 大厦",
        "project_code": "XM-2026-001",
        "client": "XX 建设集团",
        "contractor": "XX 建筑公司",
        "location": "XX 市 XX 区",
    }
    await _seed(db_conn, doc_id=did, project_id=pid,
                extract_data=_mk_payload(legacy))
    comp = (await pipeline.build_completeness_report(
        db_conn, doc_id=did, project_id=pid))["completeness"]
    assert comp["field_coverage"] == 1.0
    assert comp["missing_fields"] == []


async def test_missing_fields_report_canonical_keys(db_conn):
    """语义正确：缺字段时只报规范键名（不报历史别名）。"""
    pid, did = uuid.uuid4().hex, uuid.uuid4().hex
    await _seed(db_conn, doc_id=did, project_id=pid,
                extract_data=_mk_payload({"project_name": "XX 大厦"}))
    comp = (await pipeline.build_completeness_report(
        db_conn, doc_id=did, project_id=pid))["completeness"]
    assert comp["extracted_fields"] == 1
    assert comp["missing_fields"] == [
        "project_number", "construction_unit", "contractor", "project_location",
    ]
    # 三要素结构里 value 为空串也算缺失
    assert comp["field_coverage"] == 0.2


async def test_no_project_info_extraction_keeps_none_coverage(db_conn):
    """边界：无 project_info 提取层数据时不崩，field_coverage 为 None。"""
    pid, did = uuid.uuid4().hex, uuid.uuid4().hex
    await _seed(db_conn, doc_id=did, project_id=pid, extract_data=None)
    rep = await pipeline.build_completeness_report(
        db_conn, doc_id=did, project_id=pid)
    assert rep["completeness"]["field_coverage"] is None
    assert rep["completeness"]["required_fields"] == 0


def test_required_fields_match_bid_analysis_prompt_keys():
    """口径锁定：必备字段必须能在 18 项提示词里找到（防再次漂移）。"""
    from app.services import bid_analysis_service as bas
    prompt = bas.get_item_prompt("projectBasicInfo") or ""
    for aliases in pipeline._REQUIRED_PROJECT_INFO_FIELDS:
        assert any(f'"{a}"' in prompt for a in aliases), (
            f"{aliases} 在 projectBasicInfo 提示词里都不存在 —— 口径已漂移")


# ---------------------------------------------------------------------------
# 2. AI 调用 scene 覆盖（审核与预检 / 上传目录识别）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scene", [
    "compliance_check", "expert_review", "consistency_audit",
    "outline_recognition",
])
def test_new_scenes_registered(scene):
    """新增的 4 个 scene 必须登记进 KNOWN_SCENES，否则场景路由配不上。"""
    assert scene in pf.KNOWN_SCENES


def test_every_known_scene_has_call_site():
    """反向护栏：登记了却没人用 = 界面上的死选项（复用漂移护栏同一算法）。"""
    import re
    from pathlib import Path
    app_dir = Path(__file__).resolve().parents[1] / "app"
    pat = re.compile(r'scene\s*=\s*["\']([A-Za-z0-9_\-]+)["\']')
    used: set[str] = set()
    for p in app_dir.rglob("*.py"):
        if ".bak-" in p.name:
            continue
        used |= set(pat.findall(p.read_text(encoding="utf-8", errors="ignore")))
    assert not (set(pf.KNOWN_SCENES) - used)


def test_compliance_ai_calls_pass_scene():
    """根因回归：compliance.py 三处 AI 调用必须显式传 scene（原为空串）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "routers"
           / "compliance.py").read_text(encoding="utf-8")
    for scene in ("compliance_check", "expert_review", "consistency_audit"):
        assert f'scene="{scene}"' in src, f"compliance.py 漏传 scene={scene}"


def test_upload_outline_ai_call_passes_scene():
    src_scene = "outline_recognition"
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "routers"
           / "upload_outline.py").read_text(encoding="utf-8")
    assert f'scene="{src_scene}"' in src


# ---------------------------------------------------------------------------
# 3. 正文生成 stopped 事件携带失败明细
# ---------------------------------------------------------------------------

def test_stopped_event_carries_failed_sections():
    """根因回归：stopped 事件必须与 completed 同口径下发 failed_sections。

    此前只落 checkpoint、不下发事件 → 前端在线路径拿不到失败明细。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "routers"
           / "sse_handlers.py").read_text(encoding="utf-8")
    assert "_stopped_payload" in src
    assert "'failed_sections'" in src
