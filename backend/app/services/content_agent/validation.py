"""Agent 修复后的结构校验（R135 · 程序校验优先，不信任 Agent 自报）。

对齐易标 ``contentGenerationAgent.cjs`` 的提交校验语义：

- 「还原被改动的输入资料和非目标小节」→ :meth:`WorkspaceBaseline.restore`
  （在 repair 层调用）+ 本模块「本批以外文件不得出现」的范围检查；
- 「校验不通过时退回问题清单……保留原正文」→ 返回 ``invalid`` 判定，
  回写层据此**不写库**。

刻意不做（避免与既有链路重复实现判据）：
- L1~L5 层级 / 九大章节覆盖 / 编号一致性 —— 由审核预检
  （``preflight_engine`` / ``numbering``）既有判据承担；Agent 修复只改
  小节正文、不动目录结构，本模块只需保证「目录结构未被 Agent 改动」
  （即：小节文件集合 = 物化集合，无新增无缺失）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.services.content_agent.workspace import (
    SECTION_DIR,
    parse_section_content,
    read_section_file,
    section_relpath,
)

#: SectionValidation.status 的封闭值域。
STATUS_OK = "ok"              # 标记完整且内容有变化（允许回写）
STATUS_UNCHANGED = "unchanged"  # 标记完整但内容与原正文一致
STATUS_INVALID = "invalid"    # 标记/文件/范围不合法（绝不回写）


@dataclass
class SectionValidation:
    """一个小节的校验结论。"""

    section_id: str
    status: str
    changed: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return self.status == STATUS_OK and self.changed


def validate_workspace_sections(
    workspace_dir: str | Path,
    sections: list[dict],
) -> tuple[list[SectionValidation], list[str]]:
    """对隔离工作区做提交前结构校验。

    Args:
        sections: 物化时的同一份 ``[{"section_id", "title", "content"}]``
            （原正文即基准）。

    Returns:
        ``(逐节结论, 全局问题)``。全局问题（工作区级）包括：
        - ``正文/`` 出现**物化集合之外**的文件（Agent 新增章节文件）；
        - 物化过的小节文件**缺失**（Agent 删除章节文件）。
        任何全局问题都会让所有小节判 ``invalid``（修改范围不合法）。
    """
    root = Path(workspace_dir)
    expected_ids = [str(s.get("section_id") or "") for s in sections]
    expected_set = set(expected_ids)
    expected_files = {section_relpath(sid) for sid in expected_ids}

    global_problems: list[str] = []
    sec_root = root / SECTION_DIR
    actual_files: set[str] = set()
    if sec_root.is_dir():
        for entry in sorted(sec_root.iterdir()):
            if entry.is_file():
                actual_files.add(f"{SECTION_DIR}/{entry.name}")
            else:
                global_problems.append(
                    f"{SECTION_DIR}/ 下出现非法目录：{entry.name}")
    for extra in sorted(actual_files - expected_files):
        global_problems.append(f"工作区出现本批以外的小节文件（禁止新增章节）：{extra}")
    for missing in sorted(expected_files - actual_files):
        global_problems.append(f"小节文件缺失（禁止删除章节）：{missing}")

    results: list[SectionValidation] = []
    for sec in sections:
        sid = str(sec.get("section_id") or "")
        problems: list[str] = list(global_problems)
        raw = read_section_file(root, sid)
        if raw is None:
            results.append(SectionValidation(
                section_id=sid, status=STATUS_INVALID,
                problems=problems or ["小节文件缺失"]))
            continue
        content = parse_section_content(raw, sid)
        if content is None:
            problems.append(
                "章节标记缺失/重复/次序颠倒（必须保留唯一的 "
                "SECTION:BEGIN 与 SECTION:END 标记行）")
            results.append(SectionValidation(
                section_id=sid, status=STATUS_INVALID,
                changed=False, problems=problems))
            continue
        if not content.strip():
            problems.append("小节正文为空（禁止清空章节）")
            results.append(SectionValidation(
                section_id=sid, status=STATUS_INVALID,
                changed=False, problems=problems))
            continue
        if problems:
            # 工作区级问题（新增/删除文件等）= 修改范围不合法，全节不回写。
            results.append(SectionValidation(
                section_id=sid, status=STATUS_INVALID,
                changed=False, problems=problems))
            continue
        changed = content != str(sec.get("content") or "")
        results.append(SectionValidation(
            section_id=sid,
            status=STATUS_OK if changed else STATUS_UNCHANGED,
            changed=changed, problems=problems))
    return results, global_problems
