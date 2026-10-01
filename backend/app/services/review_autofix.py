"""审核与预检 · 问题定向自动修复（定位矛盾位置 → 最小必要改写）

背景
----
「审核与预检」页把程序化预检、导出预检、AI 规范符合性、一致性审计、专家论证
预检五路结论聚合成一份带评分的 ``findings`` 清单。此前这份清单**只能看**：
每条问题只给一句「整改建议」，用户必须自己判断改哪一章、哪一段、手工编辑。
本模块补上闭环的最后一步 —— **问题 → 定位矛盾位置 → AI 改写 → 校验 → 落库（可回滚）**。

与既有修复链路的分工
--------------------
- ``services/repair_agent.py``（全文一致性 Agent 修复）：输入是
  ``consistency_conflicts`` 表的**冲突行**（需先跑扫描 + 仲裁拿到权威值），
  只覆盖数值 / 术语冲突，且以「冲突 id」为单位批量修复；
- 本模块：输入是**统一 finding**（``rule_id`` + ``section_id``），
  覆盖六维全量问题，以「单条问题」为单位交互式修复。

两者互不替代：本模块不写 ``consistency_conflicts``，repair_agent 也不认
``rule_id``。混为一谈会出现「总检说有问题、一致性工作台说没有」的双体系分叉。

三条硬约束
----------
1. **问题来源由服务端重新派生**（见 ``routers/review_autofix.py``），
   不接受前端回传的 ``detail`` / ``evidence`` —— 否则任意文本都能被塞进
   AI 提示词（提示词注入）。
2. **失败不写库**：AI 改写结果必须通过 ``validate_fixed``
   （复用 ``repair_validator`` 的结构校验 + 能力表的语义校验），
   任一硬伤即判失败并**保留原文**。
3. **可回滚**：改写前为涉及章节存 ``scheme_snapshots`` 快照。

修复方式（能力表的唯一事实源）
-----------------------------
``_CAPABILITY`` 逐规则声明修复方式，前端据此渲染按钮，**不允许前端自行判断**
—— 否则「按钮显示可修、后端拒绝」的分叉会再次出现（同 ``rollbackable`` 教训）：

- ``auto``：纯程序化确定性修复，**不调用 AI**、不花钱、不改语义
  （控制字符 DLV-05 / 口语化 DLV-06 / 未闭合围栏 DLV-07）；
- ``ai``：定位矛盾位置后调 AI 改写（需要语义判断才能改对的各类）；
- ``manual``：不支持自动修复，必须给出**用户可执行的下一步**。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from app.config import settings
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import chat_with_fallback
from app.services.content_polish import find_colloquial_hits, sanitize_ai_content
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET, auto_fix_unclosed_fences, find_unclosed_fences,
    strip_fenced_code_blocks, text_word_count, word_status_for,
)
from app.services.audit_rules import SEVERITY_ORDER
from app.services.repair_record import save_repair
from app.services.repair_validator import validate_repair
from app.services.standards_registry import ABOLISHED_STANDARDS

# ⚠️ 留痕出口唯一性：`save_repair` 是「修复留痕」的唯一入口。
#   ``apply_fix`` 与 ``stage_fixes`` 曾各写一份引用方式（其中 ``stage_fixes``
#   漏了 import → ``ruff F821`` Undefined name `repair_record`，批量修复走到
#   留痕那一步才 NameError 500，前面的 AI 修复全部白烧）。
#   故统一在顶部 import，函数内不再有局部 import。

logger = logging.getLogger("review_autofix")

#: 单次 AI 调用超时（秒）。单章改写，不拼多章上下文。
AUTOFIX_TIMEOUT = 120
#: 低温度保证「最小必要修改」，不做创造性重写。
AUTOFIX_TEMPERATURE = 0.2
#: 单次修复涉及章节数上限（0 = 不限），防止「点了修复」把整方案重写一遍。
AUTOFIX_MAX_SECTIONS = max(
    0, int(getattr(settings, "review_autofix_max_sections", 10) or 0))
#: 单条 finding 最多返回多少个定位点（避免大方案 CON-01 刷屏）
MAX_TARGETS = 8
#: 定位回显的上下文行数（矛盾位置前后各取几行）
TARGET_CONTEXT_LINES = 1
#: 送 AI 的单章正文字符上限（超出截断，防上下文爆炸）
SECTION_CONTENT_CAP = 6000
#: 补充类修复（anchor=section）允许的篇幅放大上限（防「补一段」变成「重写一章」）。
#: 与 ``repair_validator.MAX_LEN_RATIO``（1.15，最小必要修改口径）区分使用。
SUPPLEMENT_MAX_LEN_RATIO = 3.0
#: 补充类修复的最小绝对增量（字）：极短章节按纯比例判定会误杀（补一段即超 300%），
#: 故无论原章多短，至少允许增加这么多字。
SUPPLEMENT_MIN_GROWTH = 600

FIX_MODE_AUTO = "auto"       # 纯程序化修复（不调 AI）
FIX_MODE_AI = "ai"           # 定位后调 AI 改写
FIX_MODE_MANUAL = "manual"   # 不支持自动修复（必须给出替代路径）

#: 控制字符（与 ``preflight_engine._CTRL_CHARS_RE`` 逐字符一致 —— 判据单一来源；
#: 两侧口径必须相同，否则会出现「预检报了、自动修复却定位不到」的分叉）
_CTRL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd\ufffe\uffff]")


@dataclass(frozen=True)
class Capability:
    """一条规则的自动修复能力声明。"""

    mode: str                                    # auto / ai / manual
    reason: str = ""                             # manual 时的用户可执行替代路径
    instruction: str = ""                        # ai 模式给模型的整改要求
    must_contain: tuple = ()                     # 修复后必须出现的要点
    must_not_contain: tuple = ()                 # 修复后不得残留的表述
    anchor: str = ""                             # 定位方式（见 locate_targets）


def _ai(*, instruction: str, anchor: str = "section",
        must_contain=(), must_not_contain=()) -> Capability:
    """构造一条「AI 改写」能力（``ai`` 模式的统一入口，避免散落字面量）。"""
    return Capability(mode=FIX_MODE_AI, anchor=anchor, instruction=instruction,
                      must_contain=tuple(must_contain),
                      must_not_contain=tuple(must_not_contain))


def _manual(reason: str) -> Capability:
    """构造一条「不支持自动修复」能力（``reason`` 必须是用户可执行的下一步）。"""
    return Capability(mode=FIX_MODE_MANUAL, reason=reason)


#: ✅ 规则 → 修复能力 的**唯一事实源**。键为基规则号（派生编号 CON-05-1 /
#: CON-SCAN-2 / CON-04-3 经 ``audit_rules._resolve_base_rule`` 归一）。
#: 未登记的规则落到 ``_unsupported``（明确告知不支持 + 去哪处理），**不静默**。
_CAPABILITY: dict[str, Capability] = {
    # ---------- 一、规范符合性（STD-*） ----------
    "STD-01": _ai(
        anchor="standard",
        instruction=(
            "把正文中已废止 / 已被替代的标准编号，替换为该标准的现行有效版本"
            "（现行版本见「现行有效标准参考清单」与全局事实）；标准名称同步更新；"
            "不得保留任何废止编号，也不得凭空编造清单外的编号。"),
        # 与 STD-01 判据同源：修复后不得再出现任何废止编号
        must_not_contain=tuple(ABOLISHED_STANDARDS.keys())),
    "STD-02": _ai(
        instruction=(
            "在「编制依据」相关章节补充现行全文强制性工程建设规范"
            "（GB 55xxx 系列）的引用，格式为《名称》（编号 年号），"
            "只引用清单内标准；并写明该规范在本方案中控制什么内容。")),
    "STD-03": _manual(
        "标准编号真伪需人工核实（系统无法判断某个清单外编号是否真实有效），"
        "请对照全国标准信息公共服务平台核对后手工修正；确认无误的可保留原文。"),
    "STD-04": _ai(
        instruction=(
            "在「编制依据」章节补充危大工程安全管理法规的引用"
            "（《危险性较大的分部分项工程安全管理规定》住建部令第37号、"
            "《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》"
            "建办质〔2018〕31号），并写明本方案据此执行的条款要点。")),
    "STD-05": _ai(
        instruction=(
            "在「编制依据」章节补充与本方案专业类别匹配的现行专业技术标准"
            "（只取清单内标准），并写明各标准对应的施工工序或验收环节。")),
    # ---------- 二、安全措施有效性（SAF-*） ----------
    "SAF-03": _ai(
        instruction=(
            "在应急处置章节补充「应急组织机构及职责」：明确应急领导小组/指挥机构"
            "组成、职责分工、响应启动与终止条件、报告与上报路径，职责写到岗位。")),
    "SAF-04": _ai(
        instruction=(
            "在应急处置章节补充「应急物资装备保障」：列明应急物资/装备的品种、"
            "规格型号、数量、存放位置、责任人与检查维护周期。")),
    "SAF-05": _ai(
        instruction=(
            "在应急处置章节补充「应急预案演练要求」：明确演练类型"
            "（综合/专项/现场处置）、频次、参演人员范围、演练评估与改进闭环。")),
    "SAF-06": _ai(
        instruction=(
            "在监测章节补充「预警值 / 报警值 / 控制值」：按监测项目（沉降、位移、"
            "倾角、支护内力等）分别给出三级控制指标、监测频次与超限处置动作；"
            "数值须与本方案监测项目一致，不得编造与设计工况矛盾的指标。")),
    # ---------- 三、全文一致性（CON-*） ----------
    "CON-01": _ai(
        anchor="value",
        instruction=(
            "统一该主题在全篇的取值口径：以「全局事实变量」中已确认的取值为权威值，"
            "把正文中与之矛盾的取值改为权威值（含单位），并保证同一主题在所有章节"
            "表述一致；若权威值确实无法确定，保留原值并避免出现第二种表述。")),
    "CON-02": _ai(
        anchor="term",
        instruction="统一全文对该专业术语的表述为行业规范用语，其余表述改为同一术语。"),
    "CON-03": _ai(
        anchor="term",
        instruction="统一全文的计量单位与符号写法（如 m 与 米），同一含义只保留一种写法。"),
    "CON-04": _ai(
        anchor="value",
        instruction="按全局事实变量修正正文中与已确认事实不一致的表述，只改冲突处。"),
    "CON-05": _manual(
        "两章内容高度重复需要人工判断「保留哪一份、另一份重写什么」，"
        "自动改写有丢失独有内容的风险；建议人工合并或删除其一后调整目录。"),
    # ---------- 四、可追溯性（TRC-*） ----------
    "TRC-01": _ai(
        instruction=(
            "在计算书章节补齐计算过程：计算简图说明、参数取值及其来源（规范条文号"
            "或设计参数）、计算公式与代入过程、结论与安全系数取值及验算结论；"
            "参数必须来自全局事实或设计文件，不得编造。"),
        must_contain=("计算依据", "计算过程")),
    "TRC-03": _ai(
        instruction=(
            "在正文中补充与本方案相关的附图 / 节点详图 / 平面布置图引用"
            "（含图号与图名），并说明各图对应的部位与用途；图表代码块保持原样。")),
    # ---------- 五、内容完整性（CMP-*） ----------
    "CMP-01": _ai(
        instruction="在工程概况章节补充工程基本情况、周边环境（地质/水文/邻近建构筑物"
                     "与管线）及危大工程特点。"),
    "CMP-02": _ai(
        instruction="在编制依据章节补齐法律、法规、规范性文件、标准、施工图设计文件与"
                     "施工组织设计等编制依据清单（标准只取现行有效版本）。"),
    "CMP-03": _ai(
        instruction="在施工计划章节补充进度计划（关键节点与工期安排）以及材料、设备计划"
                     "（规格、数量、进场时间）。"),
    "CMP-04": _ai(
        instruction="在施工工艺技术章节补充技术参数、工艺流程、施工方法、操作要求与"
                     "检查要求，工序需可落地执行。"),
    "CMP-05": _ai(
        instruction="在安全保证措施章节补充针对本工程主要风险源的具体措施：组织保障、"
                     "技术措施、监测监控措施，措施要写到部位、责任人与频次。"),
    "CMP-06": _ai(
        instruction="在人员配备章节补充管理人员、专职安全生产管理人员、特种作业人员"
                     "及其他作业人员的配备数量、资格要求与分工职责。"),
    "CMP-07": _ai(
        instruction="在验收要求章节补充可执行的验收标准与程序：检验批划分、验收项目与"
                     "允许偏差、验收方法、验收人员与记录要求。"),
    "CMP-08": _ai(
        instruction="在应急处置章节补齐 GB/T 29639-2020 要求的三要素：应急组织机构及职责、"
                     "应急物资装备保障、应急预案演练要求。"),
    "CMP-09": _ai(
        instruction="为计算书章节补齐可识别的计算过程：计算依据、参数取值及来源、"
                     "计算公式与代入、结论与安全系数（须含公式、参数代入或验算步骤）。",
        must_contain=("计算依据",)),
    # ---------- 六、可交付性（DLV-*） ----------
    # 这三条**不调用 AI**：预检判据与清洗实现都已有唯一实现，走 AI 反而有
    # 「改坏正文 + 花钱」的双重损失。
    "DLV-05": Capability(
        mode=FIX_MODE_AUTO, anchor="ctrl",
        instruction="清除控制字符 / 替换字符（不改变任何技术内容）。"),
    "DLV-06": Capability(
        mode=FIX_MODE_AUTO, anchor="colloquial",
        instruction="改写口语化 / AI 腔表述为工程书面语（不改变技术含义与数据）。"),
    "DLV-07": Capability(
        mode=FIX_MODE_AUTO, anchor="fence",
        instruction="补齐未闭合的 Markdown 代码围栏（仅追加结束标记，不改正文）。"),
    "DLV-01": _manual(
        "空章节需要**生成**内容而非改写，自动修复会产出编造内容；"
        "请到「正文生成」页补全生成，或删除多余空章节。"),
    "DLV-02": _manual(
        "字数不足属于内容缺失，需要按预算**扩写**而非改写；"
        "请到「正文生成」页使用「补全生成 / 续写」。"),
    "DLV-03": _manual(
        "孤立节点是**目录结构**问题（父节点不存在），与正文内容无关；"
        "请在目录树中重新挂载或删除该章节。"),
    "DLV-04": _manual(
        "图表在导出文档时自动生成，无需在正文改写；"
        "请确认图表代码块是否完整，或在「图表」页单独修复。"),
    "DLV-08": _manual(
        "整体篇幅不足是方案级问题，需要逐章扩写；"
        "请到「正文生成」页对目标章节执行「补全生成」。"),
    "DLV-09": _manual(
        "这是**人工审核**结论而非正文问题；请到「章节审核工作流」"
        "逐章标记通过 / 驳回并填写评审意见。"),
    "DLV-10": _manual(
        "章节状态与正文不一致属于数据一致性问题（多为生成中断残留）；"
        "请重新生成该章节或重置该章节状态。"),
    "DLV-11": _manual(
        "上游项目资料结构化提取不完整，需回到「上传解析 · 项目提取」"
        "补齐必选项，正文才会拿到真实参数。"),
    "DLV-12": _manual(
        "同级编号 / 标题重复属于**目录**问题，请到「目录生成」页检查"
        "同级是否存在重名或重号章节。"),
    "DLV-13": _manual(
        "正文子标题与真实子章节撞号，请到「目录生成」页调整子章节结构，"
        "或把正文内的点分子标题改为无编号小标题。"),
    "DLV-14": _manual(
        "交叉引用（图号 / 表号 / 节号）失效需人工确认应指向哪一处；"
        "请在正文中修正引用，或在「图表」页确认该图是否仍需保留。"),
    "DLV-15": _manual(
        "全局事实存在未确认 / 模拟 / 冲突项，正文不会引用它们；"
        "请到「全局事实」页核对并确认来源，或解决多来源冲突。"),
}

#: 未登记规则的兜底文案（按前缀给出可执行的下一步，绝不只说"不支持"）
_FALLBACK_REASON = {
    "STD": "标准类问题需人工核实编号与年号后修正正文（见整改建议）。",
    "CMP": "内容缺失需补充整节内容，请到「正文生成」页生成或扩写对应章节。",
    "SAF": "安全措施需结合现场实际针对性补充，请人工修订对应章节。",
    "CON": "一致性问题请先在「一致性修复」工作台查看冲突明细与权威值来源。",
    "TRC": "可追溯性问题需补齐计算书或附图，请人工补充。",
    "DLV": "可交付性问题请按整改建议人工处理。",
}


def _unsupported(rule_id: str) -> Capability:
    prefix = (rule_id or "").split("-")[0].upper()
    return Capability(
        mode=FIX_MODE_MANUAL,
        reason=_FALLBACK_REASON.get(
            prefix, "该问题暂不支持自动修复，请按整改建议人工处理。"))


def capability_of(rule_id: str) -> Capability:
    """按 rule_id 取修复能力（派生编号归一到基规则，勿另写一套解析）。"""
    from app.services.audit_rules import _resolve_base_rule
    base = _resolve_base_rule(rule_id or "")
    rid = base.rule_id if base else (rule_id or "")
    return _CAPABILITY.get(rid) or _unsupported(rid)


def capability_summary(findings: list) -> list:
    """为一批 finding 补充 ``autofix`` 字段（前端按钮的唯一判据）。

    只**新增**字段：``score_findings`` / ``merge_findings`` 对未知键无感，
    前端旧版本读不到该键时行为与现在完全一致（向后兼容）。
    """
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        cap = capability_of(f.get("rule_id") or "")
        f["autofix"] = {
            "mode": cap.mode,
            "fixable": cap.mode in (FIX_MODE_AUTO, FIX_MODE_AI),
            "reason": cap.reason,
        }
    return findings


# ---------------------------------------------------------------------------
# 定位矛盾位置
# ---------------------------------------------------------------------------
def _line_of(content: str, pos: int) -> int:
    """命中位置所在行号（1-based）。"""
    return content.count("\n", 0, max(pos, 0)) + 1


def _context(content: str, pos: int, length: int) -> str:
    """取命中位置前后各 ``TARGET_CONTEXT_LINES`` 行，拼成可回显的上下文片段。"""
    start = content.rfind("\n", 0, pos) + 1
    end = content.find("\n", pos + max(length, 1))
    if end < 0:
        end = len(content)
    for _ in range(TARGET_CONTEXT_LINES):
        prev = content.rfind("\n", 0, start - 1)
        if prev >= 0:
            start = prev + 1
        nxt = content.find("\n", end + 1)
        if nxt >= 0:
            end = nxt
    return content[start:end].strip()[:300]


#: 句子分隔符（中英文句末 + 全角分号 + 换行）。
#: ⚠️ **不含 ASCII 冒号** —— 施工文本里 ``:`` 绝大多数是**比值**（坡度 1:0.75、
#:    配筋比 1:2、时间 8:30），当作句末会把一句话切成半句，导致「第 N 句」
#:    定位指向半句话。全角分号是中文正常分句符，保留。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?;\n\r]+")


def _sentence_pos(text: str, offset: int) -> tuple[int, int]:
    """把字符偏移细化为「(第几句, 共几句)」，**1-based**。

    为什么定位到行还不够：专项方案正文一行常有多个并列分句（如
    「基坑深度 3m，边坡采用 1:0.75 放坡。」），用户要能定位到**具体哪一句**。

    Returns:
        ``(0, 0)`` 表示不可用（空文本 / 偏移越界）—— 调用方据此省略，
        不得用 ``(0, 1)`` 之类伪造「第 1 句」，那会让前端显示错误位置。
    """
    if not text or offset < 0:
        return 0, 0
    offset = min(offset, len(text))
    spans: list[tuple[int, int]] = []
    cur = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        spans.append((cur, m.start()))
        cur = m.end()
    spans.append((cur, len(text)))
    spans = [(s, e) for s, e in spans if text[s:e].strip()]
    if not spans:
        return 0, 0
    for idx, (s, e) in enumerate(spans, 1):
        if s <= offset < e:
            return idx, len(spans)
    # 偏移落在分隔符上（如命中的是句号本身）→ 归到前一句
    return len(spans), len(spans)


def _hit(content: str, pos: int, matched: str, why: str = "") -> dict:
    sentence_idx, sentence_total = _sentence_pos(content, pos)
    return {
        "line": _line_of(content, pos),
        "sentence_idx": sentence_idx,
        "sentence_total": sentence_total,
        "matched": (matched or "")[:120],
        "context": _context(content, pos, len(matched or "")),
        "why": why,
    }


def _target(s: dict, content: str, pos: int, matched: str,
            why: str, value: str = "") -> dict:
    return {
        "section_id": s.get("id") or "",
        "section_title": s.get("title") or "",
        "value": value or (matched or "")[:120],
        **_hit(content, pos, matched, why),
    }


def _value_pattern(value: str) -> re.Pattern | None:
    """按「数值 + 单位」构造容错正则。

    ⚠️ 必须容错的原因（实测）：``preflight_engine.check_consistency`` 的 evidence
    里取值是**单位归一化后**的 key（``_norm_unit`` 把「日历天」并成「天」），
    即 evidence 为 ``120天``，而正文写的是 ``120 日历天``。若按字面 ``find``
    定位，永远定位不到 → 用户点「自动修复」只会得到一句「未能定位」。

    这里以数值部分为锚，单位允许「数值 + 空白 + 任意短后缀」，既覆盖
    ``120 日历天`` / ``120天`` / ``50 m``，又不会误命中别的数字。
    """
    m = re.match(r"\s*(\d+(?:\.\d+)?)", value)
    if not m:
        return None
    num = re.escape(m.group(1))
    # 单位：紧跟数值、可含空白、最多 6 个非数字非标点字符（日历天 / 米 / m）。
    # `(?!\d)` 防前缀误命中：evidence「120天」不得命中正文里的「1200 天」。
    return re.compile(num + r"(?!\d)[ \t　]*[^\d，。；、,;]{0,6}")


def _locate_value(finding: dict, sections: list[dict]) -> list[dict]:
    """CON-01 / CON-04：定位每个**取值**在正文中的出现位置。

    ``evidence`` 形如 ``["90天（施工进度计划）", "120天（工程概况）"]``
    （``preflight_engine.check_consistency`` 的产出格式：``值（章节标题）``）。
    把「值」抽出来在全文反查每一处出现 —— 这就是需求要的「定位矛盾位置」：
    **具体到行**，而不只是「工期不一致」。
    """
    targets: list[dict] = []
    for ev in (finding.get("evidence") or [])[:MAX_TARGETS]:
        value = str(ev or "").split("（")[0].split("(")[0].strip()
        if not value or len(value) > 40:
            continue
        rx = _value_pattern(value)
        for s in sections:
            content = s.get("content") or ""
            if not content:
                continue
            for m in (rx.finditer(content) if rx else []):
                targets.append(_target(
                    s, content, m.start(), m.group(0).strip(),
                    "该主题在此处的取值", value=value))
                if len(targets) >= MAX_TARGETS:
                    return targets
            if not rx:  # 无数字锚（如纯术语）→ 退回字面查找
                pos = content.find(value)
                if pos >= 0:
                    targets.append(_target(
                        s, content, pos, value, "该主题在此处的取值", value=value))
                if len(targets) >= MAX_TARGETS:
                    return targets
    return targets


def _locate_term(finding: dict, sections: list[dict]) -> list[dict]:
    """CON-02 / CON-03：按 evidence 中的术语 / 单位写法定位。"""
    targets: list[dict] = []
    keys: list[str] = []
    for ev in (finding.get("evidence") or []):
        for part in re.split(r"[（(、,，/]", str(ev or "")):
            part = part.strip()
            # 术语 / 单位一般较短；过长的整句不做逐字定位（易误命中）
            if 1 < len(part) <= 12:
                keys.append(part)
    for key in dict.fromkeys(keys):
        for s in sections:
            content = s.get("content") or ""
            pos = content.find(key)
            if pos < 0:
                continue
            targets.append(_target(
                s, content, pos, key, "该术语 / 单位写法在此处出现", value=key))
            if len(targets) >= MAX_TARGETS:
                return targets
    return targets


def _locate_standard(finding: dict, sections: list[dict]) -> list[dict]:
    """STD-01：定位每个**已废止标准编号**在正文中的出现位置。"""
    targets: list[dict] = []
    for code in (finding.get("evidence") or [])[:MAX_TARGETS]:
        code = str(code or "").strip()
        if not code:
            continue
        for s in sections:
            content = s.get("content") or ""
            # 写法不同（GB50202-2002 / GB 50202—2002）时逐字找不到就跳过，
            # 不做模糊猜测 —— 宁可少定位，不可错定位（错定位会改坏正文）
            pos = content.find(code)
            if pos < 0:
                continue
            targets.append(_target(
                s, content, pos, code, "已废止标准编号出现在此", value=code))
            if len(targets) >= MAX_TARGETS:
                return targets
    return targets


def _locate_first_hit(sections: list[dict], matcher, why: str) -> list[dict]:
    """通用定位：在全文找满足 ``matcher`` 的位置（matcher 产出 (pos, matched)）。"""
    targets: list[dict] = []
    for s in sections:
        content = s.get("content") or ""
        if not content:
            continue
        for pos, matched in matcher(content):
            targets.append(_target(s, content, pos, matched, why))
            if len(targets) >= MAX_TARGETS:
                return targets
    return targets


def _match_ctrl(content: str):
    for m in _CTRL_CHARS_RE.finditer(content):
        yield m.start(), "控制字符"


def _match_colloquial(content: str):
    scan = strip_fenced_code_blocks(content)
    for hit in find_colloquial_hits(content):
        pos = scan.find(hit)
        if pos < 0:
            # 命中项被围栏剔除逻辑跳过了位置对齐，退回全文查找
            pos = content.find(hit)
        if pos >= 0:
            yield pos, hit


def _match_fence(content: str):
    lines = content.split("\n")
    for f in find_unclosed_fences(content):
        pos = sum(len(ln) + 1 for ln in lines[: max(f.get("line", 1) - 1, 0)])
        yield pos, f"{f.get('marker', '```')}（第 {f.get('line')} 行开围栏未闭合）"


#: anchor → 定位函数（``section`` 型在 locate_targets 内单独处理）
_ANCHORS = {
    "value": _locate_value,
    "term": _locate_term,
    "standard": _locate_standard,
    "ctrl": lambda f, secs: _locate_first_hit(secs, _match_ctrl, "控制字符出现在此"),
    "colloquial": lambda f, secs: _locate_first_hit(
        secs, _match_colloquial, "口语化 / AI 腔表述出现在此"),
    "fence": lambda f, secs: _locate_first_hit(secs, _match_fence, "未闭合围栏的开围栏在此"),
}


def _locate_whole_section(finding: dict, sections: list[dict]) -> list[dict]:
    """补充类问题（anchor=section）：问题范围是整章，锚点取该章首段。

    「缺三要素 / 缺计算过程」不存在「矛盾的那一行」，故不做逐字定位。
    """
    sid = finding.get("section_id") or ""
    out = []
    for s in [x for x in sections if x.get("id") == sid] if sid else []:
        content = (s.get("content") or "").strip()
        if not content:
            continue
        out.append({
            "section_id": s.get("id") or "",
            "section_title": s.get("title") or "",
            "value": "", "line": 1,
            # 整章型问题不存在「具体哪一句」，显式给 0 而非省略：
            # 前端据此显示「整章」而不是「第 1 句」（那会误导用户逐句核对）。
            "sentence_idx": 0, "sentence_total": 0,
            "matched": content.split("\n")[0][:120],
            "context": content[:200],
            "why": "问题范围为整章（补充缺失内容，无单点矛盾位置）",
        })
    return out


def locate_targets(finding: dict, sections: list[dict]) -> list[dict]:
    """在正文中定位该问题对应的**具体位置**（章节 + 行号 + 上下文）。

    定位不到时返回空列表，调用方据此**拒绝盲改** —— 宁可让用户手工处理，
    也不要 AI 在没有矛盾位置的情况下盲写整章。
    """
    cap = capability_of(finding.get("rule_id") or "")
    if not cap.anchor or cap.anchor == "section":
        return _locate_whole_section(finding, sections)
    fn = _ANCHORS.get(cap.anchor)
    return fn(finding, sections) if fn else []


# ---------------------------------------------------------------------------
# 修复执行
# ---------------------------------------------------------------------------
def _clean_output(text: str, section_title: str) -> str:
    """剥离模型可能误加的代码块包裹与重复标题（与 repair_agent 同口径）。"""
    from app.services.repair_agent import _clean_repair_output
    return _clean_repair_output(text or "", section_title)


def fix_programmatic(cap: Capability, content: str) -> tuple[str, list[str]]:
    """``auto`` 模式：纯程序化确定性修复，**不调用 AI**。

    三条实现全部复用既有唯一实现（与预检判据同源），不另写清洗逻辑：
    - ctrl       → 正则剔除控制字符 / 替换字符
    - colloquial → ``content_polish.sanitize_ai_content``（与生成落库清洗同函数）
    - fence      → ``content_utils.auto_fix_unclosed_fences``（与导出前置补齐同函数）

    Returns:
        ``(修复后正文, 问题列表)``；无需修复时问题列表非空且正文为原文。
    """
    if cap.anchor == "ctrl":
        out = _CTRL_CHARS_RE.sub("", content or "")
        return out, ([] if out != (content or "") else ["未发现控制字符，无需修复"])
    if cap.anchor == "colloquial":
        out = sanitize_ai_content(content or "")
        return out, ([] if out != (content or "") else ["未发现口语化残留，无需修复"])
    if cap.anchor == "fence":
        out, log = auto_fix_unclosed_fences(content or "")
        return out, ([] if log else ["未发现未闭合围栏，无需修复"])
    return content or "", ["该规则未配置程序化修复实现"]


async def fix_by_ai(*, cap: Capability, finding: dict, scheme: dict,
                    section: dict, targets: list[dict],
                    facts: str, standards_text: str) -> tuple[str, list[str]]:
    """``ai`` 模式：把**定位结果**连同章节原文交给 AI 做最小必要改写。"""
    content = section.get("content") or ""
    system = render("review_autofix_system")
    user = render(
        "review_autofix_user",
        scheme_name=scheme.get("name") or "",
        scheme_type=scheme.get("type") or "",
        section_id=section.get("id") or "",
        section_title=section.get("title") or "",
        global_facts=facts or "（无）",
        standards_text=standards_text or "（无）",
        rule_id=finding.get("rule_id") or "",
        rule_title=finding.get("title") or "",
        issue=(f"{finding.get('detail') or ''}\n"
               f"整改建议：{finding.get('suggestion') or '（无）'}"),
        targets=json.dumps(
            [{"章节": t.get("section_title"), "行号": t.get("line"),
              "原文": t.get("context"), "原因": t.get("why")}
             for t in targets], ensure_ascii=False, indent=2),
        instruction=cap.instruction,
        must_not_contain="、".join(cap.must_not_contain) or "（无）",
        must_contain="、".join(cap.must_contain) or "（无）",
        section_content=content[:SECTION_CONTENT_CAP],
    )
    raw = await chat_with_fallback(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=AUTOFIX_TEMPERATURE, timeout=AUTOFIX_TIMEOUT,
        scene="review_autofix")
    return _clean_output(raw, section.get("title") or ""), []


def validate_fixed(before: str, after: str, cap: Capability) -> tuple[bool, list[str]]:
    """校验修复结果。**失败即不写库**（保留原文）。

    复用 ``repair_validator.validate_repair``（一致性修复的同一校验器）做
    结构 / 篇幅 / 围栏校验；再叠加能力表声明的 ``must_contain`` /
    ``must_not_contain`` —— 后者是「这条问题是否真的被修掉」的**语义**校验：
    结构校验通过并不代表问题解决了（例如 AI 把废止编号换成了另一个废止编号）。
    """
    if not (after or "").strip():
        return False, ["修复后内容为空"]
    if re.sub(r"\s+", "", after) == re.sub(r"\s+", "", before):
        return False, ["修复后内容与原文一致，问题未消除"]
    ok, problems = validate_repair(
        before=before, after=after,
        wrong_values=list(cap.must_not_contain), authoritative_value="")
    # 「权威值应出现」是面向数值冲突的判据，本模块无权威值概念 → 剔除
    problems = [p for p in problems if "权威值" not in p]
    # ⚠️ 篇幅判据按修复性质区分（实测踩坑）：``validate_repair`` 的
    #    「篇幅显著增加（>115%）」是**为「最小必要修改」设计的**，对
    #    「统一取值 / 替换编号」类改写是对的，但对**补充类**修复
    #    （CMP-09 补计算过程、SAF-03 补应急组织…）是错的 ——
    #    补内容必然大幅增长正文，会被误判成「疑似整体重写」而全盘拒绝，
    #    致使补充类问题**永远修不了**。故补充类（anchor=section）改用
    #    「允许增长、但设上限防跑偏」的口径；原「显著缩短」一律保留
    #    （无论哪类修复，篇幅骤减都意味着丢内容）。
    soft: list[str] = []
    if cap.anchor == "section":
        lb = len(before or "")
        if lb:
            ratio = len(after) / lb
            # 先摘出「篇幅」类问题：补充类允许增长，只保留「超过上限」这一条硬伤
            length_problems = [p for p in problems if "篇幅" in p]
            problems = [p for p in problems if "篇幅" not in p]
            # 上限取「比例上限」与「绝对增量下限」的较大者：极短章节（如仅
            # 一句话的骨架章）按纯比例判定会误杀（补一段就超 300%），
            # 故给一个「至少允许补 N 字」的绝对额度兜底。
            allowed = max(lb * SUPPLEMENT_MAX_LEN_RATIO, lb + SUPPLEMENT_MIN_GROWTH)
            if len(after) > allowed:
                problems.append(
                    f"补充后篇幅 {len(after)} 字超过允许上限 {int(allowed)} 字，"
                    "疑似大段重写而非定点补充")
            soft = length_problems
    ok = ok and not problems
    # ⚠️ must_contain 是**硬性**语义校验：结构没坏但没补上要点，问题依旧存在，
    #    必须一并计入结论（否则「改了但没改对」会被当成修复成功）。
    hard = list(problems)
    visible = re.sub(r"\s+", "", strip_fenced_code_blocks(after))
    for token in cap.must_contain:
        if re.sub(r"\s+", "", token) not in visible:
            msg = f"修复后正文缺少必须补齐的要点「{token}」"
            problems.append(msg)
            hard.append(msg)
    return (ok and not hard), problems + soft


async def _fix_each_section(finding: dict, sections: list[dict], cap: Capability,
                            scheme: dict, facts: str, standards_text: str,
                            targets: list[dict]):
    """逐章改写并校验，返回 (items, pending, skipped)。

    ``pending`` 只含**校验通过且确有变化**的章节 —— 失败章一律不进 pending，
    落库阶段自然被跳过（保留原文）。
    """
    by_section: dict[str, list[dict]] = {}
    for t in targets:
        sid = t.get("section_id") or ""
        if sid:
            by_section.setdefault(sid, []).append(t)
    skipped = 0
    if AUTOFIX_MAX_SECTIONS and len(by_section) > AUTOFIX_MAX_SECTIONS:
        ranked = sorted(by_section.items(), key=lambda kv: len(kv[1]), reverse=True)
        skipped = len(ranked) - AUTOFIX_MAX_SECTIONS
        by_section = dict(ranked[:AUTOFIX_MAX_SECTIONS])

    sec_by_id = {s.get("id") or "": s for s in sections}
    items: list[dict] = []
    pending: list[tuple[str, str, str]] = []
    for sid, tgts in by_section.items():
        section = sec_by_id.get(sid) or {}
        before = section.get("content") or ""
        try:
            if cap.mode == FIX_MODE_AUTO:
                after, problems = fix_programmatic(cap, before)
            else:
                after, problems = await fix_by_ai(
                    cap=cap, finding=finding, scheme=scheme, section=section,
                    targets=tgts, facts=facts, standards_text=standards_text)
                if not problems:
                    ok, problems = validate_fixed(before, after, cap)
                    if not ok:
                        after = before
        except Exception as exc:  # AI 异常 / 渲染失败 → 该章判失败，不写库
            logger.warning("审核预检自动修复失败 rule=%s section=%s: %s",
                           finding.get("rule_id") or "", sid[:8], exc, exc_info=True)
            after, problems = before, [f"修复过程异常：{exc}"]
        changed = not problems and after != before
        if changed:
            pending.append((sid, before, after))
        items.append({
            "section_id": sid, "section_title": section.get("title") or "",
            "status": "repaired" if changed else "failed", "targets": tgts,
            "before": before[:4000],
            "after": (after if changed else before)[:4000],
            "problems": problems,
        })
    return items, pending, skipped


async def apply_fix(db, *, scheme_id: str, finding: dict, sections: list[dict],
                    scheme: dict, facts: str = "",
                    standards_text: str = "") -> dict:
    """定位 → 改写 → 校验，返回修复结果与**待落库**内容。

    ⚠️ 分层约束（``tests/test_outline_name_line_20260927.py::test_services_never_import_routers``
    静态守护）：**services 不得 import routers**。故本函数**不直接写正文**，
    只返回 ``pending``（已通过校验的 (section_id, before, after)），
    由 ``routers/review_autofix.py`` 负责快照 / 写库 / 审核状态 / 缓存失效。

    ``pending`` 只含校验通过且确有变化的章节 —— 失败章不在其中，
    调用方遍历 ``pending`` 落库即天然实现「失败保留原文」。
    """
    cap = capability_of(finding.get("rule_id") or "")
    rule_id = finding.get("rule_id") or ""
    if cap.mode == FIX_MODE_MANUAL:
        return {"ok": False, "status": "unsupported", "mode": cap.mode,
                "rule_id": rule_id, "reason": cap.reason,
                "items": [], "pending": [], "snapshot_id": ""}

    targets = locate_targets(finding, sections)
    if not targets:
        # 定位不到矛盾位置 → 拒绝盲改（否则 AI 会无依据地重写整章）
        return {"ok": False, "status": "not_located", "mode": cap.mode,
                "rule_id": rule_id, "targets": [],
                "reason": "未能在正文中定位到该问题的具体位置，"
                          "为避免无依据改写正文，请按整改建议人工处理。",
                "items": [], "pending": [], "snapshot_id": ""}

    items, pending, skipped = await _fix_each_section(
        finding, sections, cap, scheme, facts, standards_text, targets)

    repaired = sum(1 for i in items if i["status"] == "repaired")
    stats = {"repaired": repaired, "failed": len(items) - repaired,
             "skipped": skipped}
    # 留痕先落（快照 id 由调用方落库后回填），此处不写 sections.content
    result = await save_repair(
        db, scheme_id=scheme_id, scan_id="", mode="review_autofix",
        items=items, snapshot_id="", total_conflicts=len(items), stats=stats)
    return {"ok": repaired > 0,
            "status": "repaired" if repaired else "failed",
            "mode": cap.mode, "rule_id": rule_id, "targets": targets,
            "snapshot_id": "", "repair_id": result.get("repair_id", ""),
            "items": items, "pending": pending, "stats": stats}


# ---------------------------------------------------------------------------------------
# 批量：同章多问题链式合并 + 暂存（不落库）
# ---------------------------------------------------------------------------------------
async def _chain_section_fixes(section: dict, findings_for_section: list[dict],
                               scheme: dict, facts: str, standards_text: str
                               ) -> tuple[str, list[dict]]:
    """把同一章节内的多条 fixable finding 按严重度顺序链式改写。

    与单条 ``apply_fix`` 的语义完全一致（复用 ``_fix_each_section`` +
    ``validate_fixed``），差异只在「一次处理同章多条问题并链式衔接」——
    前一条改写后的正文作为后一条的输入，保证同章多问题合并结果正确。

    Returns:
        ``(最终章节正文, 每条 finding 的暂存结果)``。每条结果携带 ``chain_index``
        与「截至本条改写后的章节正文」``after``，供 ``/confirm`` 在**全部接受**或
        **接受前缀**时直接取对应 ``after`` 作为合并结果（无需再调 AI）。
    """
    content = section.get("content") or ""
    items: list[dict] = []
    for idx, finding in enumerate(findings_for_section):
        # ⚠️ 每轮必须把**链式累积后的最新正文**喂给 ``locate_targets`` /
        #   ``_fix_each_section``（两者都读 ``section["content"]``）。
        #   若始终传原始 section，前一条修复的成果对后一条**不可见** →
        #   「先修控制字符、再补围栏」这类**必须顺序生效**的组合互相覆盖，
        #   末条 after 只含最后一条的修复（实测：控制字符仍残留在 after 里）。
        #   这与本函数 docstring 声称的「前一条改写后的正文作为后一条的输入」
        #   直接矛盾 —— 注释与实现分叉，本仓反复踩的同型陷阱。
        section = dict(section)
        section["content"] = content
        rule_id = finding.get("rule_id") or ""
        cap = capability_of(rule_id)
        if cap.mode == FIX_MODE_MANUAL:
            items.append({
                "rule_id": rule_id, "section_id": section.get("id"),
                "section_title": section.get("title") or "",
                "mode": cap.mode, "status": "unsupported", "reason": cap.reason,
                "targets": [], "before": content[:4000], "after": content[:4000],
                "problems": [], "chain_index": idx,
                "sentence_idx": 0, "sentence_total": 0,
            })
            continue
        targets = locate_targets(finding, [section])
        if not targets:
            items.append({
                "rule_id": rule_id, "section_id": section.get("id"),
                "section_title": section.get("title") or "",
                "mode": cap.mode, "status": "not_located",
                "reason": "未能在正文中定位到该问题的具体位置，为避免无依据改写正文，"
                          "请按整改建议人工处理。",
                "targets": [], "before": content[:4000], "after": content[:4000],
                "problems": [], "chain_index": idx,
                "sentence_idx": 0, "sentence_total": 0,
            })
            continue
        sec_items, _pending, _skipped = await _fix_each_section(
            finding, [section], cap, scheme, facts, standards_text, targets)
        rec = sec_items[0] if sec_items else {}
        after = rec.get("after") or content
        items.append({
            "rule_id": rule_id, "section_id": section.get("id"),
            "section_title": section.get("title") or "",
            "mode": cap.mode, "status": rec.get("status", "failed"),
            "targets": targets, "before": content[:4000],
            "after": after[:4000], "problems": rec.get("problems", []),
            "chain_index": idx,
            "sentence_idx": targets[0].get("sentence_idx", 0) if targets else 0,
            "sentence_total": targets[0].get("sentence_total", 0) if targets else 0,
        })
        if rec.get("status") == "repaired":
            content = after
    return content, items


async def stage_fixes(db, *, scheme_id: str, findings: list[dict],
                      sections: list[dict], scheme: dict, facts: str = "",
                      standards_text: str = "") -> dict:
    """批量「定位 → 改写 → 校验」，**只暂存不落库**（落库由 ``/confirm`` 完成）。

    同章多条 finding 走 :func:`_chain_section_fixes` 链式合并；跨章各自独立。
    返回 ``{batch_id, items, stats, status}``，``status="pending_confirm"``。

    ⚠️ 分层约束（``test_outline_name_line_20260927.py::test_services_never_import_routers``
    静态守护）：**services 不得 import routers**。故本函数**不写 sections.content**，
    也**不做快照**（``snapshot_id=""``）—— 快照与落库由
    ``routers/review_autofix.py::_persist_fixed`` 在用户确认后统一完成。
    """
    by_section: dict[str, list[dict]] = {}
    sec_by_id = {s.get("id") or "": s for s in sections}
    for f in findings:
        cap = capability_of(f.get("rule_id") or "")
        if cap.mode == FIX_MODE_MANUAL:
            continue
        sid = f.get("section_id") or ""
        if sid:
            by_section.setdefault(sid, []).append(f)
            continue
        # 无 section_id（如 CON-01 数值冲突）→ 按定位结果归章
        for t in locate_targets(f, sections):
            if t.get("section_id"):
                by_section.setdefault(t["section_id"], []).append(f)

    all_items: list[dict] = []
    for sid, fs in by_section.items():
        section = sec_by_id.get(sid)
        if not section:
            continue
        # 同章多条按严重度排序（单一事实源取 audit_rules.SEVERITY_ORDER，
        # 不用本仓另写一份映射 —— 判据分叉的典型来源）
        fs_sorted = sorted(
            fs, key=lambda x: SEVERITY_ORDER.get(x.get("severity") or "", 9))
        _content, items = await _chain_section_fixes(
            section, fs_sorted, scheme, facts, standards_text)
        all_items.extend(items)

    repaired = sum(1 for i in all_items if i["status"] == "repaired")
    stats = {"repaired": repaired, "failed": len(all_items) - repaired, "skipped": 0}
    result = await save_repair(
        db, scheme_id=scheme_id, scan_id="", mode="review_autofix_batch",
        items=all_items, snapshot_id="", total_conflicts=len(all_items), stats=stats)
    return {
        "batch_id": result["repair_id"], "items": all_items,
        "stats": stats, "status": "pending_confirm",
    }


__all__ = [
    "AUTOFIX_MAX_SECTIONS", "FIX_MODE_AI", "FIX_MODE_AUTO", "FIX_MODE_MANUAL",
    "MAX_TARGETS", "SUPPLEMENT_MAX_LEN_RATIO", "Capability", "apply_fix",
    "capability_of", "capability_summary", "fix_by_ai", "fix_programmatic",
    "locate_targets", "stage_fixes", "validate_fixed",
]
