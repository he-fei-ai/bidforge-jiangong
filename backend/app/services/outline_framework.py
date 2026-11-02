"""目录结构框架：代码生成骨架 + AI 补全 + 结构递归校验

引入自《标书智能体（2）——生成标书提纲代码+提示词》方案二（长方案分步生成）
的两个核心机制：

  ① generate_one_outline_json_by_level1 —— 代码预构建 JSON 骨架
     （id 已生成、层级已定、title/description 留空），AI 只负责填内容，
     结构由程序保证，解决弱模型「输出结构不稳定」问题；
  ② check_json —— 逐层递归比对 AI 返回与传入框架，确保 AI 没有破坏
     传入的 JSON 结构，返回具体错误位置与原因（供定向修复轮携带）。

适配本软件（建筑工程专项施工方案编写）的差异：

  - 层级：参考实现为三级目录（1 / 1.1 / 1.1.1），本软件现行标准同为
    **三级叶子**（outline_rules.LEAF_LEVEL=3：L1 / L2 / L3，L3 为最小
    叶子，L4 仅作兼容层、严禁 L5。历史：R151 一度翻转「四级叶子」标准、
    R159 据此把骨架补到四级，R162 按用户指令随标准回摆三级叶子 ——
    骨架深度始终随单一事实源同深）；
  - 数量：参考实现用随机分布（nodes_distribution），本软件是硬约束
    「每个一级 3~5 个二级、每个二级 3~5 个三级」（outline_rules.CHILDREN_MIN/MAX
    单一事实源；L3 下是否细分 L4 不作数量校验），故改为约束区间内
    **按章序确定性轮转分配**（各章有差异、结果稳定可测）；传入 rng
    即可恢复参考实现的随机分布（等价 nodes_distribution 机制）；
  - 内容：参考实现对齐「技术评分要求」，本软件对齐方案主要施工内容与
    九大章节标准（一级章由 outline_level1 阶段产出，本模块只负责其
    下属二三级骨架）。

fail-soft 约定：本模块全部为纯函数，任何异常由调用方捕获后回退旧行为
（自由生成 + 宽松形状校验），绝不因此阻断目录生成。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services.outline_rules import CHILDREN_MAX, CHILDREN_MIN

logger = logging.getLogger("outline_framework")

# ✅ R162：各层子节点数量区间上收 outline_rules 单一事实源（现行标准：
# L1 下 3~5 个 L2、L2 下 3~5 个 L3，L3 为最小叶子）。历史：R159 前本模块
# 私藏「L2 2~3 / L3 1~3」字面量与 R151 四级提示词打架；R159 曾补到四级
# 骨架；R162 随「三级叶子」标准回摆（骨架叶子=L3，L4 由 AI 自然产出、
# 交 normalize_outline 裁剪，本模块不再预建 L4 层）。
#: 二级目录数量约束（每个一级下的二级数）
L2_MIN, L2_MAX = CHILDREN_MIN[1], CHILDREN_MAX[1]
#: 三级目录数量约束（每个二级下的三级叶子数）
L3_MIN, L3_MAX = CHILDREN_MIN[2], CHILDREN_MAX[2]


def build_sublevel_framework(
    chapter_index: int,
    chapter_title: str = "",
    chapter_desc: str = "",
    *,
    l2_min: int = L2_MIN,
    l2_max: int = L2_MAX,
    l3_min: int = L3_MIN,
    l3_max: int = L3_MAX,
    rng=None,
) -> dict:
    """为某一级章节构建二三级目录 JSON 骨架。

    等价移植参考实现 generate_one_outline_json_by_level1：
    二级/三级节点的 title、description 一律留空（AI 补全），id 与
    children 结构由程序生成（AI 禁止修改）。三级为骨架叶子层
    （children 为空数组；AI 若自然细分四级，由 _fill_nodes 原文保留、
    normalize_outline 统一裁剪）。

    数量分配（nodes_distribution 等价物）：
      - rng 为 None（默认）：在 [l2_min, l2_max] / [l3_min, l3_max]
        约束区间内按章序轮转确定性分配 —— 相邻章数量不同（有差异，
        符合参考实现「随机分布」的初衷），同一章多次构建结果一致（可测）；
      - rng 非 None：区间内随机分配（参考实现原始随机语义）。

    返回结构与子目录 AI 响应同构：{"outline": [二级节点, ...]}，
    每个二级节点含 children（三级叶子节点，children 为空数组）。
    """
    l2_lo = max(1, int(l2_min))
    l2_hi = max(l2_lo, int(l2_max))
    l3_lo = max(0, int(l3_min))
    l3_hi = max(l3_lo, int(l3_max))
    idx = max(1, int(chapter_index))

    def _counts(lo: int, hi: int, offset: int, n: int) -> list[int]:
        span = hi - lo + 1
        return [lo + (offset + t) % span for t in range(n)]

    if rng is not None:
        n_l2 = rng.randint(l2_lo, l2_hi)
        n_l3s = [rng.randint(l3_lo, l3_hi) for _ in range(n_l2)]
    else:
        n_l2 = l2_lo + (idx - 1) % (l2_hi - l2_lo + 1)
        n_l3s = _counts(l3_lo, l3_hi, idx - 1, n_l2)

    outline: list[dict] = []
    for j, n_l3 in enumerate(n_l3s, start=1):
        l2_node = {
            "id": f"{idx}.{j}",
            "title": "",           # ← AI 填充
            "description": "",     # ← AI 填充
            "children": [
                {
                    "id": f"{idx}.{j}.{k}",
                    "title": "",   # ← AI 填充
                    "description": "",  # ← AI 填充
                    "children": [],  # 三级叶子（R162），严禁再挂子级
                }
                for k in range(1, n_l3 + 1)
            ],
        }
        outline.append(l2_node)
    return {"outline": outline}


def check_json(json_str: Any, schema: Any) -> tuple[bool, str]:
    """根据模板 JSON 校验目标字符串的结构是否与框架一致。

    等价移植参考实现 check_json（《标书智能体（2）》），递归比对规则：

      - 数字类型 int / float 可互换；
      - 其余基本类型必须一致（type 相同）；
      - 列表：模板为空列表 → 任意列表均通过；模板非空而目标为空 → 报错；
        否则用**模板首元素**作模板校验目标的每个元素（不比对长度 ——
        节点数量由业务校验与审核链路负责，此处只管结构形状）；
      - 字典：模板的每个必需键必须存在且类型递归匹配；目标的**多余键
        放行**（AI 可携带 id 等附加字段，不影响结构判定）；
      - 出错即返回具体路径与原因（供修复轮定向修复）。

    Args:
        json_str: 要校验的 JSON 字符串或已解析对象
        schema: 模板 JSON 字符串或字典对象

    Returns:
        (是否通过, 错误信息)；通过时错误信息为空串。
    """
    try:
        if isinstance(json_str, str):
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError as e:
                return False, f"JSON 解析错误: {e}"
        elif isinstance(json_str, (dict, list)):
            data = json_str
        else:
            return False, f"json_str 必须是 JSON 字符串或 dict/list，实际 {type(json_str).__name__}"

        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except json.JSONDecodeError as e:
                return False, f"schema 解析错误: {e}"
        elif not isinstance(schema, (dict, list)):
            return False, "schema 必须是 JSON 字符串或字典对象"

        def check_structure(target, template, path: str = "") -> tuple[bool, str]:
            # 数字类型（int 和 float 可以互换）
            if isinstance(template, (int, float)) and isinstance(target, (int, float)) \
                    and not isinstance(template, bool) and not isinstance(target, bool):
                return True, ""
            # 基本数据类型必须一致
            if type(template) is not type(target):
                return False, (f"路径 '{path}' 的类型不匹配: "
                               f"期望 {type(template).__name__}, "
                               f"实际 {type(target).__name__}")
            if isinstance(template, list):
                if not template:  # 模板列表为空 → 允许任何列表
                    return True, ""
                if not target:    # 模板非空而目标为空
                    return False, f"路径 '{path}' 的列表为空，但期望有内容"
                template_item = template[0]
                for i, item in enumerate(target):
                    ok, err = check_structure(item, template_item, f"{path}[{i}]")
                    if not ok:
                        return False, err
                return True, ""
            if isinstance(template, dict):
                for key in template:
                    if key not in target:
                        return False, f"路径 '{path}' 缺少必需的键 '{key}'"
                    ok, err = check_structure(target[key], template[key],
                                               f"{path}.{key}")
                    if not ok:
                        return False, err
                return True, ""
            return True, ""

        ok, err = check_structure(data, schema)
        return ok, ("" if ok else err)
    except Exception as e:  # noqa: BLE001 - 纯防御：校验器自身异常不得炸主流程
        return False, f"未预期的错误: {e}"


def fill_framework_structure(framework: Any, ai_nodes: Any) -> list[dict]:
    """结构规整兜底：把 AI 返回的节点装配为骨架形状（机制①的最终保障）。

    语义（保守，绝不凭空造内容）：
      - **结构以骨架为准**：每个节点必须有 title / description / children
        三个键，children 为列表；层级在骨架深度内对齐骨架；
      - **数量以 AI 为准**：AI 少给的节点不补齐（凭空补会产出占位标题，
        违反本软件「严禁泛化/占位标题」红线），AI 多给的节点按同构
        模板规整后保留（不丢内容）；
      - 骨架叶子之下的 AI 子孙原样保留，交由 normalize_outline 统一
        裁剪到四级（保留「深层标题并入 description」的内容线索语义；
        R162 起骨架叶子停在三级，AI 自然产出的四级及更深由此句兜底）。

    fail-soft：任何异常返回原 AI 节点列表（由调用方日志告警）。
    """
    try:
        fw = framework.get("outline") if isinstance(framework, dict) else framework
        if not isinstance(fw, list):
            fw = []
        if not isinstance(ai_nodes, list):
            return []
        return _fill_nodes(fw, ai_nodes, level=1)
    except Exception as e:  # noqa: BLE001 - fail-soft
        logger.warning("目录骨架结构规整失败（保留 AI 原始结构）: %s", e)
        return ai_nodes if isinstance(ai_nodes, list) else []


def _fill_nodes(fw_nodes: list, ai_nodes: list, *, level: int) -> list[dict]:
    """按骨架模板逐节点规整（level=1 为二级层，≥2 为三级叶子层）。"""
    out: list[dict] = []
    for i, ai in enumerate(ai_nodes):
        if not isinstance(ai, dict):
            continue  # 非法节点丢弃（与 clamp_outline_depth 口径一致）
        # 模板取同位元素；AI 超出骨架数量时用末元素同构模板兜底
        tpl = (fw_nodes[i] if i < len(fw_nodes)
               else (fw_nodes[-1] if fw_nodes else {}))
        raw_children = ai.get("children")
        if level >= 2:
            # 骨架叶子层（三级，R162）：保留 AI 子孙原文（含自然产出的
            # 四级兼容层），交 normalize_outline 裁剪
            children = list(raw_children) if isinstance(raw_children, list) else []
        else:
            tpl_children = tpl.get("children") if isinstance(tpl, dict) else None
            children = _fill_nodes(
                tpl_children if isinstance(tpl_children, list) else [],
                raw_children if isinstance(raw_children, list) else [],
                level=level + 1)
        out.append({
            "title": str(ai.get("title") or "").strip(),
            "description": str(ai.get("description") or "").strip(),
            "children": children,
        })
    return out


def render_framework_block(framework: Any, chapter_title: str = "",
                           chapter_desc: str = "") -> str:
    """生成注入子目录提示词的【结构框架】消息正文。

    内容 = 一级章上下文（禁止修改）+ 骨架 JSON（AI 只填 title/description）
    + 填充规则（对应参考实现 SystemPrompt「在原 json 上补全信息，
    禁止修改 json 结构，禁止修改一级标题」）。
    """
    try:
        fw_json = json.dumps(framework, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 - 纯防御
        fw_json = str(framework)
    head = ""
    if chapter_title:
        head = f"【本章一级标题（禁止修改）】：{chapter_title}"
        if chapter_desc:
            head += f"\n【本章说明】：{chapter_desc}"
    return f"""{head}
【结构框架（程序预生成的 JSON 骨架）】：
{fw_json}

填充规则：
1. 在上面的 JSON 结构上**原位补全**每个节点的 title 和 description：二级标题根据一级标题撰写，三级标题根据二级标题撰写
2. **禁止修改 JSON 结构**：不得增删节点、不得改变层级、不得修改 id 与 children 结构，只允许填写留空的 title 与 description
3. **禁止修改一级标题**（【本章一级标题】原文不得出现在子目录的改写中）
4. 补全内容须贴合方案主要施工内容与项目关键事实，且不得与其他章节（见提示词中的「其他章节标题」）重复或交叉
5. description 必须是具体施工内容短语（8~20 字），不得使用占位标记
6. 直接返回补全后的完整 JSON（结构与框架完全一致），不要输出任何解释文字或 markdown 代码块标记"""
