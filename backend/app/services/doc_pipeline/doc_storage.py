"""文档四层存储架构（原文层 / 解析层 / 提取层 / 语义层）

依据《文件上传解析结果存储格式与项目功能提取工作流程规范》实现：

1. **双格式存储**：原始文件（保真）+ 结构化格式（可解析）双份保存；
2. **分层存储**：每个文档一个目录，四层子目录物理分离：

   data/projects/{project_id}/documents/
   ├── documents_index.json          # 文档索引（该项目全部文档档案）
   └── {doc_id}/
       ├── raw/                      # 第一层 原文层（原始文件副本）
       │   └── {doc_id}_original.{ext}
       ├── parsed/                   # 第二层 解析层
       │   ├── {doc_id}_parsed.md    # 结构化 Markdown（含页标记）
       │   ├── {doc_id}_pages.json   # 分页文本（页码/表格/图片/bbox）
       │   ├── {doc_id}_tables.json  # 表格 JSON（行列结构 + source_ref）
       │   ├── {doc_id}_images.json  # 图片清单（OCR 文本 + 页码）
       │   └── {doc_id}_meta.json    # 元数据（指纹/版本/耗时/质量分）
       ├── extracted/                # 第三层 提取层（AI 提取结果按类落盘）
       │   ├── {doc_id}_project_info.json
       │   ├── {doc_id}_engineering.json
       │   ├── {doc_id}_design_params.json
       │   ├── {doc_id}_geology.json
       │   ├── {doc_id}_standards.json
       │   ├── {doc_id}_boq.json
       │   └── {doc_id}_global_facts.json
       └── semantic/                 # 第四层 语义层
           └── {doc_id}_semantic.json  # 向量嵌入索引（chunk 偏移，向量待嵌入服务接入）

3. **可追溯**：所有解析/提取产物携带 source_ref（`{doc_id}#page:N#table:tK` 等格式）；
4. **时效性**：MD5/SHA256 文件指纹 + parse_version 版本号 + parse/extract 时间戳，
   支持过期检测（expires_at）与文件变更检测（supersedes/superseded_by）。

写入安全：一律 tempfile + os.replace 原子写入，进程中断不会留下半截文件。
本模块所有 IO 均为同步实现，调用方用 asyncio.to_thread 包裹。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("doc_storage")

# 四层存储根目录（e2e 测试可通过 monkeypatch 本常量重定向）
from app.config import DATA_DIR

DOCS_ROOT: Path = DATA_DIR / "projects"

# 解析层文件后缀约定
LAYER_RAW = "raw"
LAYER_PARSED = "parsed"
LAYER_EXTRACTED = "extracted"
LAYER_SEMANTIC = "semantic"
ALL_LAYERS = (LAYER_RAW, LAYER_PARSED, LAYER_EXTRACTED, LAYER_SEMANTIC)

#: 提取层标准类别（规范 §2.3 / §3.1 阶段4 的七类提取）
EXTRACT_TYPES = (
    "project_info",       # 项目基本信息
    "engineering",        # 工程概况
    "design_params",      # 设计参数
    "geology",            # 地勘参数
    "standards",          # 规范标准
    "boq",                # 工程量清单（⚠️ 预留，见 RESERVED_EXTRACT_TYPES）
    "global_facts",       # 全局事实变量
)

#: ✅ 显式化（2026-09-25）：当前**没有任何解析项映射**到这些提取类别
#: （见 pipeline._ITEM_TO_EXTRACT_TYPE），sync_extract_layer 对其恒跳过、
#: doc_extractions 永不出现对应行 —— 属「留待后续专项提取接入」的预留容量，
#: 不是缺陷。保留在 EXTRACT_TYPES 里的原因：① 查询端点 /types 契约稳定；
#: ② 历史库可能已有该类别行，type= 过滤需继续可查。
#: 护栏：tests/test_doc_pipeline.py::TestExtractTypeCoverage 会校验
#: 「每个非预留类别必须至少被一个解析项物化」，防止新增类别时静默漏接。
RESERVED_EXTRACT_TYPES = ("boq",)

#: 默认有效期（解析结果时效性，超期提示重新解析）
DEFAULT_TTL_DAYS = 365


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 目录布局
# ---------------------------------------------------------------------------

def _safe_id(raw: str) -> str:
    """project_id / doc_id 参与路径拼接前的清洗（防目录穿越）。"""
    s = str(raw or "").strip()
    cleaned = "".join(ch for ch in s if ch.isalnum() or ch in "-_.")
    return cleaned or "unknown"


def doc_dir(project_id: str, doc_id: str) -> Path:
    return DOCS_ROOT / _safe_id(project_id) / "documents" / _safe_id(doc_id)


def layer_dir(project_id: str, doc_id: str, layer: str) -> Path:
    return doc_dir(project_id, doc_id) / layer


def index_path(project_id: str) -> Path:
    return DOCS_ROOT / _safe_id(project_id) / "documents_index.json"


def parsed_md_path(project_id: str, doc_id: str) -> Path:
    return layer_dir(project_id, doc_id, LAYER_PARSED) / f"{doc_id}_parsed.md"


# ---------------------------------------------------------------------------
# 原子写入 / 读取
# ---------------------------------------------------------------------------

def atomic_write_bytes(dest: Path, data: bytes) -> Path:
    """tempfile + os.replace 原子写入，防半截文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def atomic_write_json(dest: Path, payload: Any) -> Path:
    return atomic_write_bytes(
        dest, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: Path) -> dict | list | None:
    """读取 JSON 产物；不存在/损坏返回 None（读取侧容错，不让坏文件拖崩流程）。"""
    try:
        with path.open("rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 文件指纹（时效性：变更检测）
# ---------------------------------------------------------------------------

def file_fingerprints(content: bytes) -> dict[str, str]:
    """一次遍历同时得到 MD5 / SHA256。"""
    h5, h256 = hashlib.md5(), hashlib.sha256()
    h5.update(content)
    h256.update(content)
    return {"md5": h5.hexdigest(), "sha256": h256.hexdigest()}


def hashes_differ(a: dict | None, b: dict | None) -> bool:
    """指纹比对：任一方缺失按"已变更"处理（保守触发重解析）。"""
    if not a or not b:
        return True
    return (a.get("md5") or "") != (b.get("md5") or "")


# ---------------------------------------------------------------------------
# 元数据（{doc_id}_meta.json）
# ---------------------------------------------------------------------------

def build_meta(*, doc_id: str, project_id: str, file_name: str, file_type: str,
               file_size: int, md5: str, sha256: str,
               doc_type: str = "", doc_category: str = "",
               page_count: int = 0, tags: list[str] | None = None,
               parse_status: str = "pending", parse_version: str = "v1",
               parse_time: str = "", parse_duration_ms: int = 0,
               parse_engine: str = "", parse_warnings: list[str] | None = None,
               extract_status: str = "pending", quality_score: float | None = None,
               supersedes: str | None = None,
               related_docs: list[str] | None = None) -> dict:
    """按规范 §2.1 元数据格式构造文档档案。"""
    now = _utcnow()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(days=DEFAULT_TTL_DAYS)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError):  # pragma: no cover
        expires = ""
    return {
        "doc_id": doc_id,
        "project_id": project_id,
        "file_name": file_name,
        "file_type": file_type,
        "file_size": file_size,
        "file_hash_md5": md5,
        "file_hash_sha256": sha256,
        "upload_time": now,
        "doc_type": doc_type,
        "doc_category": doc_category,
        "doc_version": doc_version_of(parse_version),
        "page_count": page_count,
        "language": "zh-CN",
        "parse_status": parse_status,
        "parse_version": parse_version,
        "parse_time": parse_time,
        "parse_duration_ms": parse_duration_ms,
        "parse_engine": parse_engine,
        "parse_warnings": parse_warnings or [],
        "extract_status": extract_status,
        "extract_time": "",
        "quality_score": quality_score,
        "expires_at": expires,
        "tags": tags or [],
        "related_docs": related_docs or [],
        "supersedes": supersedes,
        "superseded_by": None,
    }


def doc_version_of(parse_version: str) -> str:
    """解析版本号 → 文档版本号（同代次）。"""
    v = (parse_version or "v1").lstrip("vV") or "1"
    return f"v{v}"


def bump_parse_version(parse_version: str) -> str:
    """v1 → v2 → ...（每次重新解析递增代次，旧代次产物被覆盖）。"""
    try:
        n = int((parse_version or "v1").lstrip("vV"))
    except (TypeError, ValueError):
        n = 1
    return f"v{n + 1}"


def write_meta(project_id: str, doc_id: str, meta: dict) -> Path:
    return atomic_write_json(
        layer_dir(project_id, doc_id, LAYER_PARSED) / f"{doc_id}_meta.json", meta)


def read_meta(project_id: str, doc_id: str) -> dict | None:
    return read_json(layer_dir(project_id, doc_id, LAYER_PARSED)
                     / f"{doc_id}_meta.json")


# ---------------------------------------------------------------------------
# 解析层产物写入
# ---------------------------------------------------------------------------

def write_parsed_layer(project_id: str, doc_id: str, *, markdown: str,
                       pages: dict, tables: dict, images: dict) -> dict[str, str]:
    """写解析层四份结构化产物 + Markdown，返回落盘相对路径清单。"""
    base = layer_dir(project_id, doc_id, LAYER_PARSED)
    written: dict[str, str] = {}
    p = atomic_write_bytes(base / f"{doc_id}_parsed.md",
                           markdown.encode("utf-8"))
    written["markdown"] = str(p)
    for key, payload in (("pages", pages), ("tables", tables), ("images", images)):
        p = atomic_write_json(base / f"{doc_id}_{key}.json", payload)
        written[key] = str(p)
    return written


# ---------------------------------------------------------------------------
# 提取层 / 语义层产物写入
# ---------------------------------------------------------------------------

def write_extraction(project_id: str, doc_id: str, extract_type: str,
                     payload: dict) -> Path:
    """写提取层单项结果（规范 §2.3：value/confidence/source 三要素结构）。"""
    if extract_type not in EXTRACT_TYPES:
        raise ValueError(f"未知提取类别: {extract_type}")
    return atomic_write_json(
        layer_dir(project_id, doc_id, LAYER_EXTRACTED)
        / f"{doc_id}_{extract_type}.json", payload)


def read_extraction(project_id: str, doc_id: str, extract_type: str) -> dict | None:
    return read_json(layer_dir(project_id, doc_id, LAYER_EXTRACTED)
                     / f"{doc_id}_{extract_type}.json")


def write_semantic_index(project_id: str, doc_id: str, *, chunks: list[dict],
                         embedding_model: str = "", dimension: int = 0) -> Path:
    """写语义层 chunk 索引（向量嵌入的 offset/length 预留位）。

    嵌入服务尚未选型（平台此前无 RAG，检索走结构化提取），本阶段先落
    chunk 级索引：text 偏移 + 页码 + source_ref，未来嵌入就绪按 offset 填充向量。

    ⚠️ 下游消费边界（数据流审计 2026-09-23 文档化）：当前无任何读取方——语义/向量检索
    未接入，本层为预留归档，不参与目录/正文生成，也不参与完整性报告（与 doc_extractions 同属归档层）。
    """
    payload = {
        "doc_id": doc_id,
        "embedding_model": embedding_model,
        "dimension": dimension,
        "chunk_count": len(chunks),
        "status": "index_ready" if chunks else "empty",
        "generated_at": _utcnow(),
        "chunks": [
            {
                "chunk_id": c.get("chunk_id", ""),
                "page_num": c.get("page_num", 0),
                "source_ref": c.get("source_ref", ""),
                "text_offset": c.get("text_offset", 0),
                "text_length": c.get("text_length", 0),
                "embedding_offset": None,
                "embedding_length": 0,
            } for c in chunks
        ],
    }
    return atomic_write_json(
        layer_dir(project_id, doc_id, LAYER_SEMANTIC)
        / f"{doc_id}_semantic.json", payload)


# ---------------------------------------------------------------------------
# 文档索引（documents_index.json）
# ---------------------------------------------------------------------------

def update_index(project_id: str, entry: dict) -> Path:
    """按 doc_id upsert 一条索引记录（幂等）。"""
    path = index_path(project_id)
    items = read_json(path)
    if not isinstance(items, list):
        items = []
    doc_id = entry.get("doc_id", "")
    replaced = False
    for i, it in enumerate(items):
        if isinstance(it, dict) and it.get("doc_id") == doc_id:
            items[i] = {**it, **entry}
            replaced = True
            break
    if not replaced:
        items.append(entry)
    return atomic_write_json(path, items)


def remove_from_index(project_id: str, doc_id: str) -> None:
    path = index_path(project_id)
    items = read_json(path)
    if isinstance(items, list):
        items = [it for it in items
                 if not (isinstance(it, dict) and it.get("doc_id") == doc_id)]
        atomic_write_json(path, items)


# ---------------------------------------------------------------------------
# 删除与清理
# ---------------------------------------------------------------------------

def delete_doc_tree(project_id: str, doc_id: str) -> bool:
    """删除文档的四层目录树（幂等；只作用于 DOCS_ROOT 管理下的路径）。"""
    d = doc_dir(project_id, doc_id)
    try:
        root = DOCS_ROOT.resolve()
        target = d.resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        logger.warning("拒绝删除管理层外的文档目录: %s", d)
        return False
    if not target.exists():
        return False
    import shutil
    shutil.rmtree(target, ignore_errors=True)
    return True
