"""R176 基准数据集生成器（确定性种子 · 不提交生成物）。

设计目标
--------
1. 同一 ``--tier`` + ``--seed`` 在本机/CI 生成**逐字节一致**的数据集（内容、顺序、
   哈希全部确定），便于跨会话比较性能数字。
2. 只提交生成器；生成物写入 ``dataset/_generated/``（已被同目录 .gitignore 覆盖）。
3. 生成内容刻意覆盖导出/审核链路的真实形态：GFM 表格、mermaid 围栏、公式、
   占位符、编号标题、列表、引用 —— 让 ``audit_content`` / ``_parse_content_blocks``
   的微基准不是"空跑正则"。

三档规模（与 docs/性能与工作机制审计_R176_20261103.md §7.2 一致）
--------------------------------------------------------------
S: 30 节 / 100 事实 / 3 文档 / 4 图表
M: 120 节 / 400 事实 / 10 文档 / 12 图表
L: 300 节 / 1200 事实 / 30 文档 / 30 图表

用法
----
    python backend/tools/perf/dataset/generate_dataset.py --tier all
    python backend/tools/perf/dataset/generate_dataset.py --tier M --seed 20261104
    python backend/tools/perf/dataset/generate_dataset.py --tier S --out D:/tmp/perf

注意：生成器本身不做任何 DB / 网络访问，可随时安全执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from pathlib import Path

DEFAULT_SEED = 20261104
DEFAULT_OUT = Path(__file__).resolve().parent / "_generated"

TIERS: dict[str, dict] = {
    "S": {"sections": 30, "chapters": 5, "facts": 100, "docs": 3, "charts": 4,
          "chars_per_section": 1400},
    "M": {"sections": 120, "chapters": 12, "facts": 400, "docs": 10, "charts": 12,
          "chars_per_section": 1800},
    "L": {"sections": 300, "chapters": 25, "facts": 1200, "docs": 30, "charts": 30,
          "chars_per_section": 1800},
}

# 章节标题素材：(分组名, [子项...])
_TITLE_GROUPS: list[tuple[str, list[str]]] = [
    ("工程概况", ["项目背景", "建设规模", "总体布置", "周边环境", "施工条件"]),
    ("编制依据", ["招标文件", "设计图纸", "现行规范", "企业标准", "现场勘察"]),
    ("施工部署", ["组织机构", "区段划分", "施工顺序", "资源配置", "进度安排"]),
    ("主要工艺", ["测量放线", "基础施工", "主体结构", "装饰装修", "设备安装"]),
    ("质量保障", ["质量管理体系", "过程控制", "检验试验", "质量通病防治", "验收标准"]),
    ("安全文明", ["安全责任制", "危险源辨识", "临时用电", "消防管理", "文明施工"]),
    ("进度管理", ["计划编制", "里程碑控制", "资源保障", "纠偏措施", "赶工预案"]),
    ("成本管理", ["成本目标", "材料管控", "机械管控", "劳务管控", "结算管理"]),
]

_PARA_POOL: list[str] = [
    "本工程位于{loc}，{scope}。项目部依据招标文件与设计图纸组织施工，"
    "严格执行现行国家及行业标准，确保工程质量、安全、工期目标全面受控。",
    "施工前完成{scope}的技术交底与现场复核，核对坐标、标高及轴线关系，"
    "形成测量复核记录并经监理确认后方可进入下道工序。",
    "材料进场执行“先报验、后使用”原则，核对合格证、检测报告与实物标识，"
    "按规定批次见证取样送检，不合格材料一律清退出场并留存处置记录。",
    "本工序采用{method}组织流水施工，作业面按区段划分，劳动力与机械按"
    "日计划动态调配，避免交叉作业干扰与窝工现象。",
    "质量控制实行“三检制”，作业班组自检、专业工长复检、专职质检员专检，"
    "关键节点报监理验收，验收资料与施工进度同步形成、同步归档。",
    "安全管理的重点是{risk}，现场设置明显的安全警示标识，作业前进行"
    "危险源告知与防护设施检查，特种作业人员必须持证上岗。",
    "进度控制以里程碑计划为主线，周计划滚动分解到班组，出现偏差时"
    "从资源、工艺、作业面三个维度分析原因并采取纠偏措施。",
    "成品保护由专人负责，对已完成的{scope}采取覆盖、围挡、隔离等措施，"
    "交叉施工时办理交接手续，损坏成品按责任归属修复并追溯。",
]

_AUDIT_NOISE: list[str] = [
    "本节约{num}万元，具体以现场签证为准。",
    "根据以往类似工程经验，该工序合格率大概在百分之九十以上。",
    "详见图 3-2 所示节点做法。",
    "【待补充：具体品牌型号】",
    "首先完成基层处理，其次进行找平层施工，最后进行面层铺贴。",
]

_LOCS = ["本市高新区", "城东新区", "滨江片区", "工业园区", "老城区改造地块"]
_SCOPES = ["土建与安装工程", "主体结构施工", "市政配套工程", "装饰装修工程", "综合管廊工程"]
_METHODS = ["分段流水", "平行作业", "依次施工", "穿插施工"]
_RISKS = ["高处坠落", "临时用电", "起重吊装", "深基坑作业", "动火作业"]


def _stable_rng(seed: int, tier: str, idx: int, salt: str = "") -> random.Random:
    """按 (seed, tier, idx, salt) 派生独立随机源 —— 与遍历顺序无关，保证可复现。"""
    key = f"{seed}:{tier}:{idx}:{salt}".encode()
    digest = hashlib.sha256(key).hexdigest()
    return random.Random(int(digest[:16], 16))


def _section_title(tier: str, idx: int, chapters: int) -> tuple[str, str, int]:
    """返回 (编号标题, 纯标题, 层级)。根章节为 1 级，其余 2/3 级。"""
    if idx < chapters:
        group = _TITLE_GROUPS[idx % len(_TITLE_GROUPS)][0]
        return f"第{idx + 1}章 {group}", group, 1
    chapter_idx = idx % chapters
    group, items = _TITLE_GROUPS[chapter_idx % len(_TITLE_GROUPS)]
    item = items[(idx // chapters) % len(items)]
    level = 3 if idx % 11 == 0 else 2
    return f"{idx + 1}. {group}——{item}", f"{group}——{item}", level


def _build_content(tier: str, idx: int, target_chars: int, seed: int) -> str:
    """生成一节正文；固定用 idx 派生随机源，内容规模仅由 target_chars 决定。"""
    rng = _stable_rng(seed, tier, idx, "content")
    parts: list[str] = []
    size = 0
    para_no = 1
    while size < target_chars:
        tpl = _PARA_POOL[(idx + para_no) % len(_PARA_POOL)]
        text = tpl.format(
            loc=rng.choice(_LOCS), scope=rng.choice(_SCOPES),
            method=rng.choice(_METHODS), risk=rng.choice(_RISKS),
            num=rng.choice([120, 260, 480, 760]),
        )
        parts.append(f"{para_no}. {text}")
        size += len(text)
        para_no += 1

    # 真实形态覆盖：表格 / mermaid / 公式 / 审核噪声
    if idx % 7 == 0:
        parts.append(
            "| 工序 | 控制指标 | 允许偏差 | 检验方法 |\n"
            "| --- | --- | --- | --- |\n"
            "| 轴线定位 | ±5mm | ±3mm | 全站仪复核 |\n"
            "| 截面尺寸 | +8/-5mm | ±4mm | 钢尺检查 |\n"
            "| 表面平整度 | 8mm | 5mm | 2m 靠尺 |"
        )
    if idx % 9 == 0:
        parts.append(
            "```mermaid\ngraph TD\n  A[施工准备] --> B[测量放线]\n"
            "  B --> C[基础施工]\n  C --> D[主体结构]\n"
            "  D --> E[装饰装修]\n  E --> F[竣工验收]\n```"
        )
    if idx % 11 == 0:
        parts.append(
            "承载力验算按式 $f = \\frac{N}{A} \\leq [f]$ 控制，"
            "其中 N 为轴向荷载，A 为有效截面积。"
        )
    if idx % 5 == 0:
        parts.append(_AUDIT_NOISE[idx % len(_AUDIT_NOISE)])
    return "\n\n".join(parts)


def _build_sections(spec: dict, seed: int, tier: str) -> list[dict]:
    total = int(spec["sections"])
    chapters = int(spec["chapters"])
    sections: list[dict] = []
    for idx in range(total):
        title, plain, level = _section_title(tier, idx, chapters)
        if idx < chapters:
            parent_id = ""
        else:
            parent_id = f"sec-{tier}-{idx % chapters:04d}"
        sections.append({
            "id": f"sec-{tier}-{idx:04d}",
            "parent_id": parent_id,
            "title": title,
            "plain_title": plain,
            "level": level,
            "sort_order": idx,
            "word_budget": 1200,
            "status": "done",
            "content": _build_content(tier, idx, int(spec["chars_per_section"]), seed),
        })
    return sections


def _build_facts(spec: dict, seed: int, sections: list[dict]) -> list[dict]:
    groups = [g for g, _ in _TITLE_GROUPS]
    kinds = ["确定值", "推断值", "区间值"]
    facts: list[dict] = []
    for i in range(int(spec["facts"])):
        rng = _stable_rng(seed, "facts", i)
        group = groups[i % len(groups)]
        chapter = sections[i % len(sections)]["plain_title"] if sections else ""
        facts.append({
            "gt": group,
            "title": f"{group}关键参数 {i + 1:03d}",
            "content": (
                f"该项参数依据现场勘察与设计文件确定，取值 {rng.choice([12, 18, 25, 32, 45])}"
                f"{rng.choice(['MPa', 'mm', 'm', 'kN', 'm³'])}，"
                f"允许偏差按现行规范执行，施工中按批次抽检并留存记录。"
            ),
            "confidence": rng.choice([0.72, 0.86, 0.95]),
            "chapter": chapter,
            "value_kind": kinds[i % len(kinds)],
        })
    return facts


def _build_docs(spec: dict, seed: int, sections: list[dict]) -> list[dict]:
    docs: list[dict] = []
    count = int(spec["docs"])
    step = max(1, len(sections) // count)
    for d in range(count):
        start = d * step
        chunk = sections[start:start + max(3, step)]
        lines = [f"# 项目资料 {d + 1:02d}", "", "## 一、工程说明", ""]
        for sec in chunk[:6]:
            lines.append(f"### {sec['plain_title']}")
            lines.append("")
            lines.extend(sec["content"].split("\n\n")[:3])
            lines.append("")
        docs.append({
            "name": f"doc_{d + 1:02d}.md",
            "text": "\n".join(lines),
        })
    return docs


def build_corpus(tier: str, seed: int = DEFAULT_SEED) -> dict:
    """纯内存构建一份数据集（不落盘），供 bench_micro / bench_e2e 复用。"""
    if tier not in TIERS:
        raise ValueError(f"未知规模档位：{tier}（可选 {', '.join(TIERS)}）")
    spec = dict(TIERS[tier])
    sections = _build_sections(spec, seed, tier)
    facts = _build_facts(spec, seed, sections)
    docs = _build_docs(spec, seed, sections)
    return {"tier": tier, "seed": seed, "spec": spec,
            "sections": sections, "facts": facts, "docs": docs}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _outline_tree(sections: list[dict]) -> list[dict]:
    roots = [s for s in sections if not s["parent_id"]]
    by_parent: dict[str, list[dict]] = {}
    for s in sections:
        if s["parent_id"]:
            by_parent.setdefault(s["parent_id"], []).append(s)

    def node(sec: dict) -> dict:
        return {
            "title": sec["title"],
            "level": sec["level"],
            "sort_order": sec["sort_order"],
            "children": [node(c) for c in by_parent.get(sec["id"], [])],
        }

    return [node(r) for r in roots]


def write_tier(tier: str, out_dir: Path = DEFAULT_OUT, seed: int = DEFAULT_SEED,
               force: bool = False) -> dict:
    """把一档数据集写到磁盘，返回 manifest（含每个文件的 sha256）。"""
    corpus = build_corpus(tier, seed)
    dest = out_dir / tier
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} 已存在；如需覆盖请加 --force")
        shutil.rmtree(dest)
    (dest / "docs").mkdir(parents=True, exist_ok=True)

    outline_path = dest / "outline.json"
    outline_path.write_text(
        json.dumps(_outline_tree(corpus["sections"]), ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")

    sections_path = dest / "sections.jsonl"
    with sections_path.open("w", encoding="utf-8", newline="\n") as f:
        for s in corpus["sections"]:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    facts_path = dest / "facts.jsonl"
    with facts_path.open("w", encoding="utf-8", newline="\n") as f:
        for fact in corpus["facts"]:
            f.write(json.dumps(fact, ensure_ascii=False) + "\n")

    for doc in corpus["docs"]:
        (dest / "docs" / doc["name"]).write_text(
            doc["text"], encoding="utf-8", newline="\n")

    files = sorted(p for p in dest.rglob("*") if p.is_file())
    manifest = {
        "tier": tier,
        "seed": seed,
        "spec": corpus["spec"],
        "files": [{"path": str(p.relative_to(dest)).replace("\\", "/"),
                   "bytes": p.stat().st_size,
                   "sha256": _sha256(p)} for p in files],
        "totals": {"sections": len(corpus["sections"]),
                   "facts": len(corpus["facts"]),
                   "docs": len(corpus["docs"])},
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="R176 基准数据集生成器")
    ap.add_argument("--tier", default="all", choices=["S", "M", "L", "all"])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的生成物")
    args = ap.parse_args()

    tiers = list(TIERS) if args.tier == "all" else [args.tier]
    for tier in tiers:
        manifest = write_tier(tier, args.out, args.seed, args.force)
        t = manifest["totals"]
        print(f"[{tier}] sections={t['sections']} facts={t['facts']} "
              f"docs={t['docs']} -> {args.out / tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
