"""导入目录 → 软件生成标准结构 整理服务

背景（断链）：`upload_outline.parse` 只做「规则 / AI 识别 + 三级裁剪 + 编号重排」，
识别结果是上传文档结构的**原样镜像**：
  - 没有 description（编写要点）—— 软件 AI 生成 / 标准模板目录每个节点都带编写要点；
  - 章节框架可能偏离住建部令第 37 号要求的「危大工程九/十章法定内容」；
  - 命名、顺序、粗细粒度与软件生成标准不一致。

本服务把识别出的目录**重新整理成与软件生成标准相同**的结构：
  - 以 `outline_templates.build_outline(scheme_name)` 产出的标准目录为骨架
    （标准章节标题 + 编写要点 description 全覆盖）；
  - 按章节别名把识别出的真实章节**归位**到对应标准章节之下，保留用户的细分内容；
  - 标准骨架中识别结果未覆盖的章节，保留标准骨架（确保法定框架完整）；
  - 识别结果中无法对应到任何标准章节的内容，归入「补充章节」，避免内容丢失；
  - 输出结构与软件生成 / 标准模板完全一致（再经 normalize_outline 三级裁剪即可落库）。
"""
from __future__ import annotations

import copy
import logging
import re

from app.services.numbering import strip_outline_numbering
from app.services.outline_utils import MAX_OUTLINE_DEPTH

logger = logging.getLogger("outline_reorganize")

# ✅ BUG 修复（2026-10-05 · D3 深度上限分叉）：旧实现在此**重新定义**
#    `MAX_OUTLINE_DEPTH = 3`，与唯一事实源 `outline_utils.MAX_OUTLINE_DEPTH`
#    形成第二份副本 —— 一旦把目录上限调整为 4，本模块仍按 3 裁剪（归位结果被
#    静默截断），与本仓「目录深度唯一事实源」的约定相矛盾。
#    现改为从 outline_utils 导入（`_reorg_children` 的 depth 判定随之同源）。


# ---------------------------------------------------------------------------
# 章节别名映射：用于把识别出的真实章节标题归位到标准章节
# 键为「标准章节分组」，值为常见别名子串（命中即视为同一章）
# ---------------------------------------------------------------------------
CHAPTER_ALIASES: dict[str, list[str]] = {
    "工程概况": [
        "工程概况", "工程简介", "工程简况", "工程概述", "工程情况", "项目概况",
        "工程建设概况", "项目简介", "工程基本情况", "工程基本概况", "工程总概况",
        "工程主要概况", "工程综述", "项目基本情况", "项目概述", "综合概况",
        # ✅ BUG 修复（2026-09-16）：下列 4 个别名原写在**重复的 `"工程概况"` 键**
        #    里（同一个 dict 字面量出现两次同名键）—— Python 会静默丢弃前一个定义，
        #    后续维护者在第一处增删别名将完全没有效果。此处合并为唯一一份。
        "概况", "基本情况", "设计概况", "项目情况",
    ],
    "编制依据": [
        "编制依据", "编制说明", "编制原则", "编制根据", "编制基础", "依据",
        "编制范围", "编制目的",
    ],
    "施工计划": [
        "施工计划", "施工部署", "施工安排", "总体施工部署", "施工总体部署",
        "总体部署", "施工进度计划", "施工进度安排", "进度计划", "进度安排",
        "施工总体安排", "施工组织", "施工总体筹划",
    ],
    "施工工艺技术": [
        "施工工艺技术", "施工工艺", "施工方法", "施工技术方案", "施工技术",
        "主要施工方法", "施工技术要求", "施工工艺要求", "施工工艺及方法",
        "施工技术方案", "施工要点", "工艺流程",
    ],
    "施工安全保证措施": [
        "施工安全保证措施", "安全保证措施", "安全保障措施", "安全措施",
        "安全生产保证措施", "施工安全", "安全保证", "安全保障", "安全保证体系",
        "安全管理措施", "安全施工",
    ],
    "人员分工": [
        "人员分工", "人员配备和分工", "施工管理人员", "作业人员配备",
        "作业人员配备和分工", "劳动力计划", "劳动力安排", "人员配备",
        "作业人员", "劳动力", "施工人员配置", "人员配置", "人员组织",
        "施工管理及作业人员", "施工管理人员及作业人员",
    ],
    "验收要求": [
        "验收要求", "质量验收", "验收标准", "检查验收", "验收", "质量验收要求",
        "质量要求与验收", "质量验收标准", "验收标准及方法",
    ],
    "应急处置措施": [
        "应急处置措施", "应急预案", "应急救援", "应急响应", "事故应急处置",
        "应急", "突发", "应急准备与响应", "应急保障措施", "事故应急处理",
    ],
    "计算书及相关图纸": [
        "计算书", "计算书及图纸", "计算书与相关图纸", "计算", "相关图纸",
        "施工图纸", "附图", "计算书及附图", "设计计算", "计算书及图纸",
    ],
    "监测方案": [
        "监测方案", "施工监测", "监测", "监控量测", "监测监控", "监测方案",
        "变形监测", "施工监测方案", "监测与监控",
    ],
    "绿色施工": [
        "绿色施工", "环境保护", "环保", "节能减排", "绿色施工与环境保护",
    ],
    "质量保证": [
        "质量保证", "质量目标", "质量计划", "质量控制", "质量措施", "质量保证措施",
        "创优", "质量通病",
    ],
}

# 反查：别名子串 → 分组
_ALIAS_LOOKUP: dict[str, str] = {}
for _grp, _toks in CHAPTER_ALIASES.items():
    for _t in _toks:
        # 长别名优先：后写的会被短的覆盖，故按长度降序注册
        _ALIAS_LOOKUP[_t] = _grp
_ALIAS_SORTED = sorted(_ALIAS_LOOKUP.keys(), key=len, reverse=True)


def _group_of(title: str) -> str | None:
    """返回标题命中的标准章节分组（无则返回 None）"""
    t = title or ""
    for tok in _ALIAS_SORTED:
        if tok in t:
            return _ALIAS_LOOKUP[tok]
    return None


def _strip_number(title: str) -> str:
    """去除标题前缀的章节编号，仅用于匹配评分与落库前剥离。

    ✅ BUG 修复（2026-10-05 · D1 编号剥离第三份分叉副本）：
    旧实现自带一份 `_NUM_RE` 正则（与 numbering.py 分叉），其中
    「中文编号」「点分编号」两个分支过度激进，实测产生用户可见的标题损坏：

        _strip_number("十二层平面布置")  → "层平面布置"
        _strip_number("三层梁板施工")    → "层梁板施工"
        _strip_number("十个人")          → "个人"
        _strip_number("1.2.3钢筋工程")   → "3钢筋工程"   # 点分只剥单段

    而 _strip_tree 的剥离结果会被写入 description（"（含：…）"）与
    补充/附录章节标题，最终随 outline_json 落库 —— 属数据损坏级缺陷。
    现统一委托 `numbering.strip_outline_numbering`（编号剥离的唯一实现），
    删除本地 `_NUM_RE`，杜绝「改一处、另一处漂移」的第三份副本。
    """
    return strip_outline_numbering(title or "")


def _strip_tree(nodes: list) -> list:
    """递归去除识别结果标题前缀编号（落库前会统一重排编号，避免双重编号）"""
    out = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        nn = copy.deepcopy(n)
        nn["title"] = _strip_number(nn.get("title") or "")
        nn["children"] = _strip_tree(nn.get("children", []))
        out.append(nn)
    return out


def _shared_alias_len(a: str, b: str) -> int:
    """两标题共有的最长别名子串长度（用于归位评分；<2 视为无关）"""
    best = 0
    for tok in _ALIAS_SORTED:
        if tok in a and tok in b:
            best = max(best, len(tok))
    return best


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) <= 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _title_sim(a: str, b: str) -> float:
    """标题相似度 0~1（字符 2-gram Jaccard）"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / len(sa | sb)


def _match_node(std_title: str, candidates: list[dict], used: set[int],
                 min_score: float = 0.34) -> tuple[dict | None, float]:
    """在 candidates 中为某个标准章节挑选最匹配的真实章节。

    命中规则（优先级）：
      1) 同一分组（group）且共享长别名 → 强匹配（score=1.0）
      2) 共享别名子串长度 >=3（如都含"监测"）→ 中强匹配
      3) 2-gram 相似度 >= min_score → 弱匹配
    返回 (节点, score)；无匹配返回 (None, 0)
    """
    sg = _group_of(std_title)
    best: dict | None = None
    best_score = 0.0
    for c in candidates:
        if id(c) in used:
            continue
        ct = c.get("title") or ""
        cg = _group_of(ct)
        if sg and cg and sg == cg:
            return c, 1.0
        shared = _shared_alias_len(std_title, ct)
        if shared >= 3:
            score = 0.9
        else:
            score = _title_sim(std_title, ct)
        if score > best_score:
            best_score = score
            best = c
    if best and best_score >= min_score:
        return best, best_score
    return None, best_score


def _mk_node(title: str, desc: str = "", children: list | None = None,
             rec_desc: str = "") -> dict:
    """构造标准节点：标准 description 为主，识别出的真实说明追加其后（不覆盖）"""
    d = (desc or "").strip()
    rd = (rec_desc or "").strip()
    final = d
    if rd and rd not in d:
        final = f"{d}；{rd}" if d else rd
    return {
        "title": title,
        "description": final,
        "children": children or [],
    }


def _clone(nodes: list | None) -> list:
    return [copy.deepcopy(n) for n in (nodes or []) if isinstance(n, dict)]


def _loose_titles(nodes: list, used: set[int], limit: int = 12) -> list[str]:
    """收集**未被归位**的真实章节标题（含其后代），供并入父节点描述。

    `used` 里记录的是已被标准章节消费掉的真实节点（`id()` 标识）。
    返回按原始顺序去重后的标题，最多 limit 条（描述体量可控）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def walk(ns, depth: int = 0) -> None:
        if depth > 10 or len(out) >= limit:
            return
        for n in ns or []:
            if not isinstance(n, dict) or len(out) >= limit:
                continue
            if id(n) not in used:
                t = str(n.get("title") or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            walk(n.get("children", []), depth + 1)

    walk(nodes)
    return out


def _with_loose(desc: str, loose: list[str]) -> str:
    """把未归位标题并入描述（沿用 outline_utils 的「（含：…）」既有写法）。"""
    if not loose:
        return desc
    merged = "；".join(loose)
    return f"{desc}（含：{merged}）" if desc else f"含：{merged}"


def _reorg_children(recognized_children: list, std_children: list,
                    used: set[int], depth: int) -> list:
    """把真实子章节归位到标准子章节之下（递归，输出 <=MAX_OUTLINE_DEPTH 级）

    ✅ BUG 修复（2026-09-16）：旧实现只在**标准骨架存在对应子章节**时才保留
    识别到的真实子章节；凡标准分支缺失或更浅（标准模板只到三级、而用户上传的
    目录是四级），这些真实章节就被**静默丢弃** —— 与模块文档「避免内容丢失」
    的承诺相矛盾。现补一道兜底：未被归位的真实子章节标题（含其后代）按系统既有
    约定并入所属节点的 description（"（含：…）"），内容线索不再凭空消失。
    """
    out: list[dict] = []
    if depth >= MAX_OUTLINE_DEPTH:
        return out
    for sc in std_children:
        m, _ = _match_node(sc.get("title") or "", recognized_children, used)
        if m:
            used.add(id(m))
            kids = _reorg_children(m.get("children", []), sc.get("children", []), used, depth + 1)
            # 归位完成后仍未消费的真实子章节 → 标题并入描述（宁并入，不丢弃）
            loose = _loose_titles(m.get("children", []), used)
            out.append(_mk_node(sc["title"], _with_loose(sc.get("description", ""), loose),
                                kids, m.get("description", "")))
        else:
            # 标准子章节未匹配到真实章节：保留标准骨架
            out.append(_mk_node(sc["title"], sc.get("description", ""), _clone(sc.get("children", []))))
    return out


def reorganize_to_standard(recognized: list, scheme_name: str,
                           preserve_unmatched: bool = True) -> dict:
    """把识别出的目录整理成与软件生成标准相同的结构。

    Args:
        recognized: 识别出的目录树（list[dict]，可能任意层级、无 description）
        scheme_name: 方案名称（用于匹配标准模板）
        preserve_unmatched: 是否把无法归位的内容放入「补充章节」

    Returns:
        {"outline": [...标准结构...], "report": {...}, "template": str}
    """
    from app.services.outline_templates import build_outline, match_template

    template_key = match_template(scheme_name)
    std = _clone(build_outline(scheme_name))
    rec_nodes = _strip_tree(recognized)

    report = {
        "template": template_key,
        "standard_chapters": len(std),
        "matched_chapters": 0,
        "kept_standard_skeleton": 0,
        "appended_extras": 0,
    }

    used: set[int] = set()
    out: list[dict] = []
    for sc in std:
        m, _ = _match_node(sc.get("title") or "", rec_nodes, used)
        if m:
            used.add(id(m))
            report["matched_chapters"] += 1
            kids = _reorg_children(m.get("children", []), sc.get("children", []), used, 1)
            # ✅ 兜底（2026-09-16）：该章下未被归位的真实子章节，标题并入描述，
            #    避免"标准骨架没有对应分支"时用户内容静默丢失
            loose = _loose_titles(m.get("children", []), used)
            out.append(_mk_node(sc["title"], _with_loose(sc.get("description", ""), loose),
                                kids, m.get("description", "")))
        else:
            report["kept_standard_skeleton"] += 1
            out.append(_mk_node(sc["title"], sc.get("description", ""), _clone(sc.get("children", []))))

    # 未归位的真实顶层章节：优先并入最相似的标准章节，否则进「补充章节」
    extras = [n for n in rec_nodes if id(n) not in used]
    if extras and preserve_unmatched:
        appendix: list[dict] = []
        for ex in extras:
            host, score = _match_node(ex.get("title") or "", out, set())
            if host and score >= 0.34:
                host["children"].append(
                    _mk_node(ex["title"], "", _clone(ex.get("children", [])), ex.get("description", "")))
            else:
                appendix.append(
                    _mk_node(ex["title"], ex.get("description", ""), _clone(ex.get("children", []))))
        if appendix:
            out.append(_mk_node(
                "补充章节（导入补充）",
                "导入文档中未对应到标准章节的其它内容，已保留以免丢失",
                appendix))
            report["appended_extras"] = len(appendix)

    return {"outline": out, "report": report, "template": template_key}
