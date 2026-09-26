"""统一编号服务 —— 目录编号的唯一事实来源（目录生成 / 正文生成 / 导出文档三模块收敛点）

背景：目录编号此前存在三处同构但各自维护的实现，任一处改动都可能漂移——
  1) 目录树编号（AI 输出清洗 / 落库前重排）：services/ai/json_response.renumber_outline；
  2) DB 侧拖拽/增删后的重排：routers/sections.renumber_sections_after_reorder 内嵌 _renumber；
  3) 导出展示编号：services/ai/heading_v2.HeadingNumberingGeneratorV2（计数器 + 格式映射）。

本模块把「编号重算」「存储态→展示态映射」「正文子标题规范化」收敛为唯一实现，
其余模块只做薄包装或引用，杜绝再次漂移。

编号体系口径（与 HEADING_STYLE_CONFIG / 前端 outlineTreeLogic 一致）：
- 存储态（canonical）：sections.outline_json.id 点分路径（"3.2.4"）。
  结构事实源是 parent_id + sort_order 组成的树，编号随时可由本模块重算；
- 展示态（V2 导出口径）：L1=第X章（中文数字）、L2=X、L3=X.X、L4=X.X.X、L5=X.X.X.X
  —— 二级及以下展示编号 = 存储编号去掉首段章号后的相对路径；
- 图号 / 表号：「图{章}-{序}」「表{章}-{序}」为独立命名空间，由导出器
  （export.py 的 figure_counters / table_counters）管理，不与本模块混用。
"""
import json
import re

# ============================================================
# 编号字符表（唯一事实源；heading_templates / heading_v2 由此再导出）
# ============================================================
# 中文数字（支持 1-30，含 "" 占位以保持索引一致）
CHINESE_NUMBERS: list[str] = [
    "", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十",
]

# 英文小写字母（a-z 之后回到 aa…az，共 52 个）
ALPHABET: list[str] = [
    chr(ord("a") + i) if i < 26 else f"a{chr(ord('a') + i - 26)}" for i in range(52)
]


# ------------------------------------------------------------
# 标题内嵌编号清洗（修复双重编号）
#
# 设计约定：目录树只存裸标题，编号由 renumber_outline_nodes 的节点 id 推导、
# 在前端/导出时套用。但 AI 经常无视指令在标题里自带编号（"第一章 工程概况"、
# "2.1 相关法律法规"），若不清洗则显示层再套一层 → "第一章 第一章 工程概况"。
# ------------------------------------------------------------
_STRIP_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零0-9]+[章节]"      # 第X章 / 第X节 / 第1章
    r"|[（(][一二三四五六七八九十0-9]+[)）]"          # （三） / (3)
    r"|[0-9]+[)）]"                                    # 1） / 2）
    r"|[0-9]+(?:\.[0-9]+){0,7}[、.．，,：: \-—]+"      # 1 / 1.2 / 1.2.3 + 分隔符
    r"|[一二三四五六七八九十百千零]+[、.．，,：: \-—]+"  # 一、 / 十二、
    r")[、.．，,：: \-—]*"
)
# 纯数字路径型标题（整个标题就是一个编号，如 "1" / "1.1" / "1.2.3"）
_PURE_NUMBER_TITLE_RE = re.compile(r"[0-9]+(?:[.．][0-9]+)*")

# 合法存储编号：点分数字路径（1 / 1.1 / 1.1.1）
_DOT_PATH_RE = re.compile(r"\d+(\.\d+)*")


def strip_outline_numbering(title: str) -> str:
    """去掉标题开头已嵌入的编号前缀，返回裸标题。

    安全性设计：
    - "第X章/节"、"（三）"、"1）" 为无歧义结构，直接剥离；
    - 纯数字/中文数字编号必须后跟分隔符（顿号/点/空格等）才剥离，
      因此 "2023年规范"、"3D打印"、"十二层平面" 等正常标题不受影响；
    - 若剥离后为空（标题本身就是一个编号），返回原标题；
    - 整体即纯数字路径的标题（"1.1" / "1.2.3"）直接原样返回——
      否则 "." 同时属于分隔符字符类，会只剥半截（"1.1" → "1"），
      违反「标题本身即编号 → 返回原标题」的契约。
    """
    if not title:
        return title
    if _PURE_NUMBER_TITLE_RE.fullmatch(title.strip()):
        return title.strip()
    stripped = _STRIP_NUMBER_RE.sub("", title, count=1).strip()
    return stripped or title.strip()


def renumber_outline_nodes(nodes: list, *, strip_titles: bool = True,
                           prefix: str = "", _depth: int = 0) -> list:
    """程序统一重排目录树编号（不信任模型编号）——目录树编号的唯一实现。

    同时修复：
    - 标题内嵌编号 → 剥离（strip_titles=True 时，防止显示层双重编号）
    - 缺少 children 字段的节点 → 添加 children=[]
    - children=None → 转为 []
    - children 不是列表 → 转为 []
    - 循环引用防护：最大深度 20

    Args:
        nodes: 目录树节点列表（就地修改并返回）。
        strip_titles: 是否剥离标题内嵌编号。目录树（标题为 AI 原文）用 True；
            DB 行标题已是裸标题的路径用 False。
        prefix: 父节点编号前缀（递归内部使用）。
        _depth: 递归深度（循环引用防护）。
    """
    if _depth > 20 or not isinstance(nodes, list):
        return nodes
    # 编号按"有效节点"连续递增：非法节点（非 dict）被跳过时不应占用编号，
    # 避免出现 "1 / 2 / 4" 这样的断号。
    valid_idx = 0
    for node in nodes:
        if not isinstance(node, dict):
            # ✅ 健壮性修复：旧实现直接 node["id"]=... → 传入手工编辑/第三方
            #    上传的畸形目录（含字符串项）时抛 AttributeError/TypeError，
            #    save-outline 接口直接 500。
            continue
        valid_idx += 1
        node_id = f"{prefix}.{valid_idx}" if prefix else str(valid_idx)
        node["id"] = node_id
        node["level"] = node_id.count(".") + 1
        # ✅ 清洗标题内嵌编号（双重编号根因修复）
        if strip_titles and node.get("title"):
            node["title"] = strip_outline_numbering(str(node["title"]))
        # 确保 children 是合法列表
        children = node.get("children")
        if not isinstance(children, list):
            node["children"] = []
            children = node["children"]
        if children:
            renumber_outline_nodes(children, strip_titles=strip_titles,
                                   prefix=node_id, _depth=_depth + 1)
    return nodes


def renumber_section_outline_ids(tree: list) -> list[tuple[str, int, str]]:
    """按章节树重算 DB 侧编号，返回 UPDATE 参数元组列表。

    服务对象：sections 表的 outline_json.id / level 列（拖拽排序、增删章节、
    整表重建后的统一重排）。每个元组为 (outline_json_str, level, section_id)。

    与 renumber_outline_nodes（目录树路径）的区别：
    - DB 标题已是裸标题，不剥编号（strip_titles=False 语义）；
    - outline_json 为 JSON 字符串，需解析后仅刷新编号相关字段（id / level），
      保留 confidence 等既有内容；
    - 同级全部节点连续计数（无非法节点跳过逻辑——DB 行都是合法 dict）。

    本函数为纯计算（无 IO），调用方拿到元组后自行 executemany 写库。
    """
    updates: list[tuple[str, int, str]] = []

    def _walk(ns: list, prefix: str = ""):
        idx = 0
        for n in ns:
            idx += 1
            new_id = f"{prefix}.{idx}" if prefix else str(idx)
            try:
                oj = json.loads(n.get("outline_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                oj = {}
            if not isinstance(oj, dict):
                oj = {}
            # 保留 confidence 等既有字段，仅刷新编号相关项
            level = new_id.count(".") + 1
            oj["id"] = new_id
            oj["level"] = level
            # ✅ 修复（2026-09-18）：level 列与 outline_json.level 同步回写。
            #    旧实现只改 outline_json、不改 sections.level —— 一旦层级发生变化
            #    （编号深度变化），level 列与编号漂移，凡是按 level 过滤/统计的
            #    消费方（如锁一级目录、按层级导出）都会拿到旧值。
            updates.append((json.dumps(oj, ensure_ascii=False), level, n["id"]))
            if n.get("children"):
                _walk(n["children"], new_id)

    _walk(tree)
    return updates


def stored_outline_id(sec: dict) -> str:
    """从章节行（sections 记录）提取合法的存储态编号（outline_json.id）。

    只接受点分路径（1 / 1.1 / 1.1.1）；UUID 主键、缺失、非法 JSON 一律返回 ""。
    （原 routers/sse_handlers._section_outline_number 的唯一实现收敛于此。）
    """
    raw = (sec or {}).get("outline_json") or ""
    if not raw:
        return ""
    if isinstance(raw, dict):
        obj = raw
    else:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(obj, dict) and obj.get("id"):
        s = str(obj["id"]).strip()
        # 合法编号只能是点分路径；UUID 主键等一律视为无效
        if _DOT_PATH_RE.fullmatch(s):
            return s
    return ""


def stored_id_to_display(stored_id: str) -> str:
    """存储态编号 → 展示态编号（导出 V2 口径）。

    "3" → "第三章"；"3.2" → "2"；"3.2.4" → "2.4"；"3.2.4.5" → "2.4.5"。
    二级及以下展示编号 = 存储编号去掉首段章号后的相对路径（与
    heading_v2 计数器、DEFAULT_NUMBERING_TEMPLATES 的 {lastN} 口径一致）；
    一级为「第X章」（中文数字，超出字表范围回退阿拉伯数字）。
    非法编号返回 ""。
    """
    parts = [p for p in str(stored_id or "").split(".") if p.isdigit()]
    if not parts:
        return ""
    if len(parts) == 1:
        n = int(parts[0])
        if 0 < n < len(CHINESE_NUMBERS):
            return f"第{CHINESE_NUMBERS[n]}章"
        return f"第{n}章"
    return ".".join(parts[1:])


def stored_id_to_prefix(stored_id: str) -> str:
    """存储态编号 → 正文子标题编号前缀（与导出 _section_number_prefix(展示标题) 同口径）。

    "3" → "3"（第三章 → 3）；"3.2" → "2"；"3.2.4" → "2.4"。非法返回 ""。

    导出端正文子标题的前缀取自展示标题（第X章/X/X.X）中的数字路径，
    本函数直接由存储编号折算，两侧对 canonical 目录树天然一致。
    """
    parts = [p for p in str(stored_id or "").split(".") if p.isdigit()]
    if not parts:
        return ""
    return ".".join(parts[1:]) if len(parts) > 1 else parts[0]


def renumber_section_body_subheadings(
        content: str, section_number: str, section_level: int = 1,
        section_title: str = "",
        has_db_children: bool = False) -> tuple[str, list[dict]]:
    """把章节正文内的子标题编号规范化为导出口径（幂等、无损）。

    背景（编号统一 · 2026-09-25）：AI 撰写正文时以提示词注入的存储态编号
    （如 "3.2"）自行推算子标题编号（"3.2.1"），而导出端按展示态编号
    （"3.2" → "2"）重算为 "2.1" —— 格式映射本身就使前端预览与导出成稿
    不一致；AI 写错号 / 跳号 / 重复时导出端虽会重算纠偏，落库正文却保留
    错误编号，预检（duplicate_section_number）与前端展示全部失真。

    本函数在正文落库前用与导出 write_section 完全同一套解析与编号算法
    （_parse_content_blocks / _compute_subheading）重写子标题行编号，
    使「前端预览 = 落库正文 = 导出成稿」三处同源。

    特性：
    - 幂等：规范化后的内容再次执行零改动（剥旧号 + 同一算法）；
    - 无损：只重写子标题行自身，代码围栏 / 列表 / 段落一概不动；
    - 兜底：存储编号非法 / 无子标题时原样返回，绝不抛异常阻断生成。

    Args:
        content: 章节正文（Markdown）。
        section_number: 本节存储态编号（outline_json.id，如 "3.2"）；非法时原样返回。
        section_level: 本节层级（仅参与 Heading 样式深度计算，不影响编号文本）。
        section_title: 本节裸标题（用于剥离正文开头与标题重复的块，与导出同口径）。
        has_db_children: 本节是否有 DB 子章节（用于 E3 降级判定：有子女时正文子标题
            改走节内 body 命名空间 1）/ a、…，与 DB 子章节彻底隔离）。默认 False。

    Returns:
        (规范化后正文, changes)；changes 元素为
        {"line": 行号(1基), "old": 原子标题文本, "new": 新子标题文本}。
    """
    if not content or not content.strip():
        return content, []
    section_prefix = stored_id_to_prefix(section_number)
    if not section_prefix:
        # 存储编号非法（历史脏数据 / UUID）：与提示词「编号缺失」兜底口径一致，
        # 不做改写（导出端会按展示编号重算，此处不猜测）。
        return content, []

    # 函数级 import：export 路由体量大且被众多模块引用，模块级引入会形成
    # services → routers 的反向依赖与循环 import 风险；此处运行时引入安全
    # （export 模块级只 import services 层，不 import 本模块）。
    from app.routers.export import (
        _compute_subheading,
        _parse_content_blocks,
        _strip_duplicate_leading_title,
        _strip_title_number,
    )

    lines = content.split("\n")
    blocks = _parse_content_blocks(content)
    if not blocks:
        return content, []

    # 与 write_section 同口径：先剥离正文开头与章节标题重复的块
    # （AI 常在正文首行自引用本节标题；导出渲染会丢弃该块，规范化若给它
    #   占号将使后续子标题整体错位一格）。
    display = stored_id_to_display(section_number)
    pure_title = _strip_title_number(str(section_title or "").strip())
    full_title = f"{display} {pure_title}".strip() if display else pure_title
    bare_title = strip_outline_numbering(str(section_title or "").strip())
    titles = [t for t in (bare_title, full_title) if t]
    if titles:
        blocks = _strip_duplicate_leading_title(blocks, *titles)

    # 第一遍：按导出同款算法为每个标题块计算规范化文本
    sub_counters: dict = {}
    try:
        sec_level = max(1, min(int(section_level or 1), 8))
    except (TypeError, ValueError):
        sec_level = 1
    # ✅ E3：配置开关关闭时不降级（与导出端 parity 硬约束）
    _demote = has_db_children
    try:
        from app.config import settings as _s
        _demote = has_db_children and _s.body_subheading_demote_with_children
    except Exception:
        pass
    for block in blocks:
        if block.get("type") != "heading":
            continue
        pure = _strip_title_number(block.get("text", ""))
        text, _style = _compute_subheading(
            section_prefix, sec_level, block.get("level", 1), sub_counters,
            pure, "", has_children=_demote)
        block["_fixed_text"] = text

    # 第二遍：把规范化文本精确重写回源行（#-标题保留井号与缩进；纯文本标题整行替换）
    changes: list[dict] = []
    rewrites: dict[int, str] = {}
    for block in blocks:
        if block.get("type") != "heading":
            continue
        fixed = str(block.get("_fixed_text") or "")
        src = block.get("src_line")
        orig_text = str(block.get("text") or "")
        # 空编号文本（标题本身即编号被剥空）或无源行信息 → 跳过，保持原样
        if not fixed or fixed == orig_text or src is None:
            continue
        if not isinstance(src, int) or not (0 <= src < len(lines)):
            continue
        line = lines[src]
        indent = line[:len(line) - len(line.lstrip())]
        m_hash = re.match(r"^(#{1,6})\s+", line.lstrip())
        if m_hash:
            # Markdown 井号标题：保留原井号数量，规范化编号与标题文本
            new_line = f"{indent}{m_hash.group(1)} {fixed}"
        else:
            # 纯文本编号标题（含 **加粗** 包裹形态）：改写为与检测层级等价的
            # # 形态。⚠️ 不能保留纯文本形态——"2 资源配置"被解析为层级 2，
            # 重写为"2.2 资源配置"后按「段数+1」会被解析为层级 3，重解析时
            # 相对深度漂移、编号跳变（幂等被破坏）。# 形态的层级由井号数
            # 唯一确定，重写前后不变；渲染侧（前端/导出）对两者完全等价，
            # 加粗包裹标记按标题样式语义丢弃。
            new_line = f"{indent}{'#' * min(int(block.get('level') or 2), 6)} {fixed}"
        if new_line == line:
            continue
        rewrites[src] = new_line
        changes.append({"line": src + 1, "old": orig_text, "new": fixed})

    if not rewrites:
        return content, []
    for idx, new_line in rewrites.items():
        lines[idx] = new_line
    return "\n".join(lines), changes
