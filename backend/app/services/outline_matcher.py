"""目录树节点 → 已有章节 的**结构感知匹配器**（单一事实源）。

背景（2026-11-10 · R209）
--------------------------
「整表重建 sections 但尽量保留已有正文」这条能力在三处写入口各写了一份，
且实现强度逐份漂移：

| 写入口 | 实现 | 匹配强度 | 已知缺陷 |
|---|---|---|---|
| ``routers/upload_outline.save_as_outline`` | 本地三层索引（标题路径 / 同父同层 / 同层）+ 全局兜底 + ``consumed`` 集合 | 最强（结构优先 + 消费式） | 能力完整但**前端已不再调用**该端点 |
| ``routers/outline_library.apply_library_and_save`` | R203b #20 引入的「标题 → 首个主键」一维字典 | 最弱（全局一维、不消费） | 同名节点撞同一主键 ⇒ **400 整个套用失败**；跨父级同名 ⇒ 正文张冠李戴 |
| ``routers/sections._save_outline_to_db`` | 只认 ``__original_id`` | 无兜底 | 前端「导入目录 / AI 调整目录」回传的节点带 ``upload_*`` 临时 key ⇒ 匹配不到任何旧行 ⇒ **已有正文被整批级联删除** |

本模块把「按结构匹配旧章节」抽成**唯一实现**，供上述三处共用：

- **消费式**：每个旧章节至多被一个节点认领（``consumed`` 集合）——
  同名节点不会撞成同一主键（这正是旧实现的 400 直接成因）。
- **结构优先**：标题路径（祖先行标题元组）→ 同父同层 → 同层 → 全局同名，
  逐级放宽；结构越接近越优先，避免「第 1 章的正文挂到第 2 章同名节上」。
- **标题强制相等**：任何桶取号都要求「归一化标题相等」，绝不把**不同标题**的
  旧章节认领为「新标题」的正新闻位（否则会误保留 + 留下孤儿行）。

判据与 ``upload_outline`` 2026-10-04 的三层索引修复**逐字同源**（含
``pre_nt_index`` 那种「自动改写前原标题」的用法：调用方把原标题传进来即可）。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

__all__ = [
    "normalize_outline_title",
    "OutlineMatcher",
    "build_outline_matcher",
]

#: 归一化标题：去掉全部空白（与 upload_outline._norm_title 同口径）。
_WS_RE = re.compile(r"\s+")


def normalize_outline_title(title) -> str:
    """归一化标题（比较用，绝不用于展示/落库）。"""
    return _WS_RE.sub("", str(title or "")).strip()


class OutlineMatcher:
    """结构感知的「旧章节认领器」。

    典型用法::

        matcher = build_outline_matcher(existing_rows)
        for node in nodes:
            old = matcher.take(
                title=node["title"],
                level=node.get("level", 1),
                parent_id=parent_db_id,      # 新树里父节点的 DB 主键
                path_titles=ancestor_titles,  # 新树里祖先行标题（已归一化）
            )
            if old:
                ...  # 复用 old["id"]，保留 content / word_count / 元数据

    认领规则（按优先级）：

    1. **标题路径**相同（``path_titles + (本节点标题,)``）—— 最强身份；
    2. **同父 + 同层**且标题相同；
    3. **同层**且标题相同；
    4. **全局同名**且标题相同（兜底）。

    每条规则都要求「归一化标题相等」；已认领的旧章节不再被二次认领。
    """

    __slots__ = ("_by_title", "_by_parent_level", "_by_level", "_by_path", "_old_by_id", "_consumed", "_taken")

    def __init__(self, existing_rows: Iterable[dict]):
        rows = [dict(r) for r in (existing_rows or [])]
        self._by_title: dict[str, list[dict]] = {}
        self._by_parent_level: dict[tuple, list[dict]] = {}
        self._by_level: dict[int, list[dict]] = {}
        self._by_path: dict[tuple, list[dict]] = {}
        self._old_by_id: dict[str, dict] = {}
        self._consumed: set[str] = set()
        self._taken: dict[str, str] = {}  # 新节点索引 -> 旧章节 id（仅供观测）

        for r in rows:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            self._old_by_id[rid] = r
            nt = normalize_outline_title(r.get("title", ""))
            if not nt:
                continue
            self._by_title.setdefault(nt, []).append(r)
            try:
                lv = int(r.get("level") or 1)
            except (TypeError, ValueError):
                lv = 1
            self._by_parent_level.setdefault((str(r.get("parent_id") or ""), lv), []).append(r)
            self._by_level.setdefault(lv, []).append(r)

        # 旧树的「标题路径」索引（祖先行标题元组）——与编号无关，重排后仍稳定
        for r in rows:
            if not normalize_outline_title(r.get("title", "")):
                continue
            self._by_path.setdefault(self._old_title_path(r), []).append(r)

    # ------------------------------------------------------------------ 内部

    def _old_title_path(self, row: dict) -> tuple:
        parts: list[str] = []
        seen: set[str] = set()
        cur_id = str(row.get("id") or "")
        guard = 0
        while cur_id and cur_id not in seen and guard < 50:
            seen.add(cur_id)
            node = self._old_by_id.get(cur_id)
            if node is None:
                break
            t = normalize_outline_title(node.get("title", ""))
            if t:
                parts.append(t)
            cur_id = str(node.get("parent_id") or "")
            guard += 1
        return tuple(reversed(parts))

    def _take_from(self, candidates: Optional[list[dict]], expected_nt: str) -> Optional[dict]:
        """从候选桶里取一个「未认领且标题相等」的旧章节。"""
        if not candidates:
            return None
        while candidates:
            cand = candidates.pop(0)
            cid = str(cand.get("id") or "")
            if cid in self._consumed:
                continue
            if normalize_outline_title(cand.get("title", "")) != expected_nt:
                continue
            self._consumed.add(cid)
            return cand
        return None

    # ------------------------------------------------------------------ 对外

    def take(
        self,
        *,
        title: str,
        level: int = 1,
        parent_id: str = "",
        path_titles: Iterable[str] = (),
        node_key: str = "",
        match_title: Optional[str] = None,
    ) -> Optional[dict]:
        """为一个新树节点认领旧章节；认领不到返回 ``None``。

        Args:
            title: 新节点标题（原始文本，内部会归一化）；缺省下也是**匹配身份**。
            level: 新节点层级。
            parent_id: 新树中父节点的 **DB 主键**（根节点传空串）。
            path_titles: 新树中祖先节点的标题（原始文本，按根→父顺序）。
            node_key: 可选的节点标识（仅用于日志/观测）。
            match_title: ✅ R229（匹配身份与展示标题解耦，upload_outline 的
                「自动外科式改写前原标题」语义下沉为单一事实源）：匹配身份
                **优先**用它（归一化后与旧章节标题比较），缺失时回退 ``title``。
                用途：标题在落库前被外科式改写时，匹配仍按**改写前原标题**
                认领旧章节，从而「改标题」不再等于「丢正文」；展示/落库标题
                仍用改写后的 ``title``。传 None/空串 = 用 ``title``（既有调用方
                逐字不变）。
        """
        nt = normalize_outline_title(match_title if match_title else title)
        if not nt:
            return None
        try:
            lv = int(level or 1)
        except (TypeError, ValueError):
            lv = 1
        path = tuple(normalize_outline_title(t) for t in (path_titles or ()) if normalize_outline_title(t))
        cur_path = path + (nt,)
        for bucket in (
            self._by_path.get(cur_path),
            self._by_parent_level.get((str(parent_id or ""), lv)),
            self._by_level.get(lv),
            self._by_title.get(nt),
        ):
            matched = self._take_from(bucket, nt)
            if matched is not None:
                if node_key:
                    self._taken[node_key] = str(matched.get("id") or "")
                return matched
        return None

    @property
    def consumed(self) -> set[str]:
        """已被认领的旧章节 id 集合（只读快照）。"""
        return set(self._consumed)

    @property
    def taken_map(self) -> dict[str, str]:
        """新节点标识 → 旧章节 id 的认领映射（只读快照）。"""
        return dict(self._taken)


def build_outline_matcher(existing_rows: Iterable[dict]) -> OutlineMatcher:
    """构造匹配器（薄包装，便于调用方一行接入）。"""
    return OutlineMatcher(existing_rows)
