"""一致性冲突 ID：内容哈希（R181 · N-08/L1，2026-11-06）。

背景
----
冲突 ID 此前是**位置序号** ``C001/C002…``：同一处冲突在相邻两次扫描里换了
序号，而 ``consistency_conflicts.id`` 是持久化主键 —— 用户对旧 ``C001`` 的
「已确认 / 已驳回」结论会被本轮**另一处**冲突继承（R179 用身份签名做了
兜底重置，但 ID 本身仍不是身份，外部引用 / 修复批次 / 日志都无法稳定追踪
同一处问题）。

口径
----
身份 = ``conflict_type`` + 主题（``strip()``）+ ``(section_id, value)`` 集合
（排序后）。与 :func:`consistency_scanner._conflict_identity` 同一事实源，
本模块是该口径的**唯一定义点**，扫描合并与历史迁移都必须调用这里。

ID 形态
--------
- 有 ``scheme_id``：``{md5(scheme_id)[:6]}-C{sha1(identity)[:10]}``
- 无 ``scheme_id``：``C{sha1(identity)[:10]}``

方案前缀沿用 R13/P0（2026-10-04）的跨方案防碰撞设计，前缀后的哈希由身份
决定，**与扫描顺序无关**，因此同一处冲突跨轮 ID 稳定。
"""

from __future__ import annotations

import hashlib
import json
import re

# 内容哈希 ID 的形态：可选 6 位方案前缀 + C + 10 位小写十六进制
_CONTENT_HASH_ID_RE = re.compile(r"^(?:[0-9a-f]{6}-)?C[0-9a-f]{10}$")


def conflict_id_prefix(scheme_id: str) -> str:
    """方案作用域前缀（空 scheme_id → 空前缀，保持历史无前缀形态）。"""
    if not scheme_id:
        return ""
    return f"{hashlib.md5(str(scheme_id).encode('utf-8')).hexdigest()[:6]}-"


def conflict_identity_tuple(conflict_type, topic, occurrences) -> tuple:
    """冲突身份签名：类型 + 主题（去首尾空白）+ 排序后的（章节, 取值）集合。"""
    occ = sorted(
        (str(o.get("section_id") or ""), str(o.get("value") or "")) for o in (occurrences or []) if isinstance(o, dict)
    )
    return (str(conflict_type or ""), str(topic or "").strip(), tuple(occ))


def conflict_identity_payload(conflict_type, topic, occurrences) -> str:
    """身份签名的稳定序列化（供哈希与跨语言核对使用，不含 list/dict 顺序噪音）。"""
    identity = conflict_identity_tuple(conflict_type, topic, occurrences)
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def conflict_id(scheme_id: str, conflict_type, topic, occurrences) -> str:
    """由内容身份生成冲突 ID（确定性：同一身份、同一方案恒得同一 ID）。"""
    payload = conflict_identity_payload(conflict_type, topic, occurrences).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:10]
    return f"{conflict_id_prefix(scheme_id)}C{digest}"


def is_content_hash_id(value: str) -> bool:
    """是否为内容哈希 ID（用于迁移判定，旧顺序号 ``C001`` 不会被误判）。"""
    return bool(_CONTENT_HASH_ID_RE.match(str(value or "")))
