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
import logging
import re

logger = logging.getLogger(__name__)

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

# 目录树递归最大深度（环引用 / 异常深嵌套防护，全模块唯一口径）
_MAX_TREE_DEPTH = 20


# ------------------------------------------------------------
# 标题内嵌编号清洗（修复双重编号）
#
# 设计约定：目录树只存裸标题，编号由 renumber_outline_nodes 的节点 id 推导、
# 在前端/导出时套用。但 AI 经常无视指令在标题里自带编号（"第一章 工程概况"、
# "2.1 相关法律法规"），若不清洗则显示层再套一层 → "第一章 第一章 工程概况"。
# ------------------------------------------------------------
# 编号分隔符字符类（顿号 / 半角点 / 全角点 / 逗号 / 冒号 / 空格 / 横线）
_SEP_CLASS = r"[、.．，,：: \-—]"
# 正文起始边界：全角标点与 CJK 汉字 —— 编号后直接跟中文或全角括号（无空格）时，
# 也应判定为「编号前缀已结束」（"2.4.1钢筋工程"）。
_CJK_BOUNDARY = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"

_STRIP_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[一二三四五六七八九十百千零0-9]+\s*[章节]"   # 第X章 / 第X节 / 第 1 章
    r"|[（(][一二三四五六七八九十0-9]+[)）]"            # （三） / (3)
    r"|[0-9]+[)）]"                                     # 1） / 2）
    # ✅ BUG 修复（2026-09-27 · 标题损坏）：点分编号（1.2 / 1.2.3 …）此前写作
    #    `[0-9]+(?:\.[0-9]+){0,7}[分隔符]+` —— 路径后**没有**分隔符时，
    #    正则引擎会回退成「更短的前缀 + 把点当分隔符」，把标题剥成残句：
    #        "1.2.3（1）细部构造" → "3（1）细部构造"
    #        "2.4.1钢筋工程"      → "1钢筋工程"（残留数字 + 双重编号）
    #    现改为「前瞻捕获最长路径 + 反向引用整条吃满」模拟原子组（Python/JS
    #    均不支持原子组，但前瞻是原子的、反向引用不可回退），路径后要求
    #    紧跟分隔符，或紧跟 CJK/全角字符（中文标题常常不留空格）。
    # ✅ BUG 修复（2026-10-03 · 数量型标题丢首字）：本分支的「紧跟 CJK 边界」
    #    备选原用 `[0-9]+(?:\.[0-9]+)*`（星号），于是**单段数字直接接中文**
    #    （无分隔符）也被当编号剥离，把数量型标题的数字剥掉：
    #        "2层作业平台" → "层作业平台"、"2台塔吊" → "台塔吊"、"10个人" → "个人"
    #    —— 与该函数 docstring 承诺的「单段数字编号必须后跟分隔符才剥离，
    #    十二层平面等正常标题不受影响」相矛盾。此 CJK 无分隔符剥离本就只为
    #    多段点分路径（"2.4.1钢筋工程"）设计。现把捕获限定为**至少一个点段**
    #    `(?:\.[0-9]+)+`，单段数字（"2、施工安排" / "2 施工准备"）的剥离仍由下方
    #    「[0-9]+分隔符+(?![0-9])」分支负责，多段路径紧邻 CJK 的剥离行为不变。
    r"|(?=([0-9]+(?:\.[0-9]+)+))\1(?:" + _SEP_CLASS + r"+|(?=" + _CJK_BOUNDARY + r"))"
    # 单段编号（"1 施工准备"）：必须带分隔符，且分隔符后不能仍是数字 ——
    # 否则 "1.5m 深" 会被剥成 "5m 深"（把数值/单位剥坏）。
    r"|[0-9]+" + _SEP_CLASS + r"+(?![0-9])"
    r"|[一二三四五六七八九十百千零]+" + _SEP_CLASS + r"+"   # 一、 / 十二、
    r")" + _SEP_CLASS + r"*"
)
# 纯数字路径型标题（整个标题就是一个编号，如 "1" / "1.1" / "1.2.3"）
_PURE_NUMBER_TITLE_RE = re.compile(r"[0-9]+(?:[.．][0-9]+)*")

# ✅ 增强（2026-09-27 · 年份不是编号）：4 位数字后紧跟「年」是年份
#    （"2023 年度安全生产计划" / "2024年施工计划"），旧实现对带空格的写法
#    会剥成「年度安全生产计划」（标题丢年份）。命中即原样保留。
_YEAR_PREFIX_RE = re.compile(r"^\s*[0-9]{4}\s*年")

# 合法存储编号：点分数字路径（1 / 1.1 / 1.1.1）
_DOT_PATH_RE = re.compile(r"\d+(\.\d+)*")


def strip_outline_numbering(title: str) -> str:
    """去掉标题开头已嵌入的编号前缀，返回裸标题。

    安全性设计：
    - "第X章/节"、"（三）"、"1）" 为无歧义结构，直接剥离；
    - 点分编号一次性吃满整条路径（"1.2.3（1）细部构造" → "（1）细部构造"，
      绝不回退成 "3（1）细部构造"）；
    - 单段数字/中文数字编号必须后跟分隔符才剥离，
      因此 "2023年规范"、"3D打印"、"十二层平面" 等正常标题不受影响；
    - 4 位数字 + "年" 视为年份（"2023 年度安全生产计划"）原样保留；
    - 若剥离后为空（标题本身就是一个编号），返回原标题；
    - 整体即纯数字路径的标题（"1.1" / "1.2.3"）直接原样返回——
      否则 "." 同时属于分隔符字符类，会只剥半截（"1.1" → "1"），
      违反「标题本身即编号 → 返回原标题」的契约。
    """
    if title is None:
        return title
    if not isinstance(title, str):
        title = str(title)
    if not title.strip():
        return title
    trimmed = title.strip()
    if _PURE_NUMBER_TITLE_RE.fullmatch(trimmed):
        return trimmed
    if _YEAR_PREFIX_RE.match(title):
        # 年份不是编号：原样保留（仅去首尾空白）
        return trimmed
    stripped = _STRIP_NUMBER_RE.sub("", title, count=1).strip()
    return stripped or trimmed


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
    if _depth > _MAX_TREE_DEPTH or not isinstance(nodes, list):
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
    - 同级全部节点连续计数（非法节点被跳过且不占号）。

    本函数为纯计算（无 IO），调用方拿到元组后自行 executemany 写库。

    ✅ BUG 修复（2026-10-04 · 边界与环防护）：旧实现 _walk 无任何防御——
      tree 为 None / 非 list 时迭代抛 TypeError；节点非 dict 时 n.get 抛
      AttributeError；节点缺 id 时 n["id"] 抛 KeyError；环引用 / 超深嵌套导致
      RecursionError 使整次重算失败（调用方事务回滚）。现：
      - 入参非法返回空列表；非 dict / 缺 id 节点跳过并告警，不占编号；
      - visited 集合检测环引用（DB 主键本应唯一，重复出现即异常结构）；
      - 深度上限 _MAX_TREE_DEPTH 截断，超限子树跳过并告警；
      - outline_json 不可序列化时跳过该节点，不拖垮整树。
    """
    updates: list[tuple[str, int, str]] = []
    if not isinstance(tree, list):
        logger.warning("章节编号重算：入参 tree 非 list（实际 %s），跳过重算",
                       type(tree).__name__)
        return updates

    def _walk(ns: list, prefix: str = "", depth: int = 0,
              visited: set[str] | None = None):
        if not isinstance(ns, list):
            return
        if depth > _MAX_TREE_DEPTH:
            logger.warning("章节编号重算：嵌套深度超过 %s，截断子树（prefix=%s）",
                           _MAX_TREE_DEPTH, prefix)
            return
        if visited is None:
            visited = set()
        idx = 0
        for n in ns:
            if not isinstance(n, dict):
                logger.warning("章节编号重算：跳过非 dict 节点（实际 %s）",
                               type(n).__name__)
                continue
            sid = n.get("id")
            if not isinstance(sid, str) or not sid:
                logger.warning("章节编号重算：跳过缺少合法 id 的节点")
                continue
            if sid in visited:
                logger.warning("章节编号重算：检测到重复/环引用节点 %s，不重复展开",
                               sid[:8])
                continue
            idx += 1
            visited.add(sid)
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
            try:
                payload = json.dumps(oj, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                logger.warning("章节编号重算：节点 %s 的 outline_json 不可序列化，"
                               "跳过该节点: %s", sid[:8], e)
                continue
            updates.append((payload, level, sid))
            children = n.get("children")
            if isinstance(children, list) and children:
                _walk(children, new_id, depth + 1, visited)

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

    # ✅ 2026-09-27（T-2 已修）：原先在此运行时 `from app.routers.export import`
    # （services → routers 反向依赖）。四个纯函数已整体下沉到
    # services/content_blocks.py，routers/export.py 反过来从那里导入，
    # 依赖方向恢复为 routers → services。此处仍用函数级 import 以避免
    # 任何残余的导入期环（content_blocks 模块级已引用本模块的 ALPHABET）。
    from app.services.content_blocks import (
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


# ============================================================
# 正文落库前的编号规范化接线（唯一实现）
# ------------------------------------------------------------
# 历史：sections.update_section 与 sse_handlers._persist_section 各自内联了
# 一份「读 outline_json/level/title → 查子章节数 → 调 renumber」的样板代码。
# 两份样板漂移过一次真实事故：`content_subheading_renumber` 开关在两处都
# 没有被读取，配置项形同虚设（文档承诺「设为 False 回退旧行为」实际无效）。
# 现收口到本函数，两条落库路径只调用它，开关与口径由单一实现保证。
# ============================================================
async def load_scheme_section_index(db, scheme_id: str) -> dict:
    """一次性预取方案内全部章节的编号元数据，供批量校验/修复复用。

    ✅ 性能修复（2026-09-27 · 消除导出路径的 N+1）：
    旧实现里 `validate_scheme_numbering_consistency` 虽然把 content 批量读出来了，
    但每章仍要调用 `normalize_section_content_subheadings`，而后者**逐章**再查两次
    （读 outline_json/level/title + COUNT 子章节）。实测 200 章方案 =
    **401 次 db.execute / 480ms**，而该函数在 `export.py::_guard_numbering_consistency`
    的 strict 与非 strict **两个分支都会跑**（即每次导出都付一次）。

    本函数把这两类信息压成 2 条查询：
      1) 一次 SELECT 取回全方案 id/outline_json/level/title；
      2) 一次 GROUP BY parent_id 统计子章节数。
    返回结构（供 `_normalize_from_meta` 消费，调用方不必理解）：
        {"meta": {section_id: {"number": str, "level": int, "title": str}},
         "has_children": {section_id: bool}}

    契约：查询失败一律返回空索引（`{"meta": {}, "has_children": {}}`），
    调用方据此回退到逐章查询的原路径，**行为与旧版完全一致**（fail-soft）。
    """
    empty: dict = {"meta": {}, "has_children": {}}
    meta: dict = {}
    has_children: dict = {}
    try:
        cur = await db.execute(
            "SELECT id, outline_json, level, title FROM sections WHERE scheme_id=?",
            (scheme_id,))
        for r in await cur.fetchall():
            sid = r["id"]
            raw = r["outline_json"] or "{}"
            try:
                obj = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                obj = {}
            if not isinstance(obj, dict):
                obj = {}
            number = str(obj.get("id") or "").strip()
            if not number:
                # 存储编号非法（历史脏数据 / UUID 主键）→ 与旧实现一致：跳过规范化
                continue
            try:
                level = int(r["level"] or 1)
            except (TypeError, ValueError):
                level = 1
            meta[sid] = {"number": number, "level": level,
                         "title": str(r["title"] or "")}
        cur = await db.execute(
            "SELECT parent_id, COUNT(*) AS n FROM sections "
            "WHERE scheme_id=? AND COALESCE(parent_id,'')!='' GROUP BY parent_id",
            (scheme_id,))
        for r in await cur.fetchall():
            has_children[r["parent_id"]] = bool((r["n"] or 0) > 0)
    except Exception as e:  # noqa: BLE001 — 预取失败必须回退逐章查询，不得阻断
        logger.warning("预取方案编号索引失败（回退逐章查询）: %s", e)
        return empty
    return {"meta": meta, "has_children": has_children}


def _normalize_from_meta(content: str, info: dict, has_db_children: bool
                         ) -> tuple[str, bool]:
    """用已预取的元数据执行规范化（纯计算，无 IO）。

    与 `normalize_section_content_subheadings` 的查库之后那半段**逐行等价**，
    是「唯一编号算法」的第二入口：算法本体只此一份，批量/单章共用，
    避免出现第二套实现再次漂移。
    """
    new_content, changes = renumber_section_body_subheadings(
        content, info["number"], info["level"], info["title"],
        has_db_children=has_db_children)
    return new_content, bool(changes)


def _renumber_disabled() -> bool:
    """`content_subheading_renumber` 开关（读取失败时按默认开启，与旧版一致）。"""
    try:
        from app.config import settings
        return not settings.content_subheading_renumber
    except Exception:  # pragma: no cover - 配置不可用时按默认开启
        return False


async def normalize_section_content_subheadings(
        db, scheme_id: str, section_id: str, content: str,
        *, index: dict | None = None) -> tuple[str, bool]:
    """正文落库前规范化 Markdown 子标题编号，返回 (规范化后正文, 是否发生改写)。

    与导出 write_section 同算法（renumber_section_body_subheadings），
    使「前端预览 = 落库正文 = 导出成稿」三处同源。

    契约（务必保持）：
    - 开关 `content_subheading_renumber=False` 时**原样返回**（回退旧行为：
      正文保留 AI 原始编号，仅导出端重算）；
    - 章节不存在 / 存储编号非法（历史脏数据、UUID 主键）时原样返回；
    - 任何异常一律降级为「原样返回 + WARNING」，绝不阻断正文落库
      （正文丢失的代价远高于编号不完美）。

    调用点必须在**结构重排之后**调用（update_section 的 parent_id 变更、
    create/delete/reorder 的重排都会改写 outline_json.id）：否则会按旧编号
    规范化，随后编号又被重排，正文子标题与新编号永久错位。

    参数 `index`：批量调用方（`load_scheme_section_index`）预取的元数据。
    传入时**跳过全部查库**（消除 N+1）；缺省/查不到该章时回退到原逐章查询路径，
    因此本函数对既有单章调用方**完全向后兼容**（签名仅新增 keyword-only 可选参数）。
    """
    if not content:
        return content, False
    if _renumber_disabled():
        return content, False

    try:
        if index is not None:
            info = (index.get("meta") or {}).get(section_id)
            if info is not None:
                return _normalize_from_meta(
                    content, info,
                    bool((index.get("has_children") or {}).get(section_id, False)))

        cur = await db.execute(
            "SELECT outline_json, level, title FROM sections WHERE id=? AND scheme_id=?",
            (section_id, scheme_id))
        rec = await cur.fetchone()
        if rec is None:
            return content, False
        raw = rec["outline_json"] or "{}"
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        section_number = str(obj.get("id") or "").strip()
        if not section_number:
            return content, False
        try:
            section_level = int(rec["level"] or 1)
        except (TypeError, ValueError):
            section_level = 1
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM sections WHERE parent_id=? AND scheme_id=?",
            (section_id, scheme_id))
        child_row = await cur.fetchone()
        has_db_children = bool(child_row and (child_row["n"] or 0) > 0)

        return _normalize_from_meta(
            content,
            {"number": section_number, "level": section_level,
             "title": str(rec["title"] or "")},
            has_db_children)
    except Exception as e:  # noqa: BLE001 — 编号规范化绝不能阻断正文落库
        import logging
        logging.getLogger("numbering").warning(
            "章节 %s 正文子标题编号规范化失败（保留原文）: %s", section_id[:8], e)
        return content, False


# ============================================================
# 编号一致性显式跨校验器（2026-09-26）
# ============================================================
async def validate_section_content_numbering(
        db, scheme_id: str, section_id: str, content: str,
        *, index: dict | None = None) -> dict:
    """单章落库正文子标题编号 vs 当前 outline 编号 一致性校验（显式跨模块校验器）。

    复用 normalize_section_content_subheadings 同一规范化逻辑：若按当前 outline 重算后
    子标题文本与落库内容不同，即落库正文编号与目录/导出不一致（典型 D4 类漂移）。
    content_subheading_renumber=False 时规范化被跳过 → 返回 skipped=True（无法判定，
    视为通过，与落库行为一致）。

    参数 `index`：批量调用方传入的预取索引（见 `load_scheme_section_index`），
    用于消除逐章查库的 N+1；缺省时行为与旧版完全一致。

    Returns:
        {section_id, consistent, skipped, changed, diffs, error?}
        diffs 为差异行列表（tag/old/new），便于前端/日志定位。
    """
    # ✅ BUG 修复（2026-09-27 · 契约不成立）：docstring 一直承诺
    #    「content_subheading_renumber=False 时返回 skipped=True」，但旧实现
    #    只靠 normalize_section_content_subheadings 返回 changed=False 间接等价，
    #    `skipped` 字段恒为 False —— 消费方（validate_scheme_numbering_consistency /
    #    路由返回体 / 前端）永远看不到「本次校验被开关跳过」，会把
    #    「无法判定」误读成「校验通过」。现按承诺显式回传。
    disabled = _renumber_disabled()
    if not content or not content.strip():
        return {"section_id": section_id, "consistent": True,
                "skipped": disabled, "changed": False, "diffs": []}
    try:
        new_content, changed = await normalize_section_content_subheadings(
            db, scheme_id, section_id, content, index=index)
    except Exception as e:  # noqa: BLE001 — 校验本身不应阻断
        return {"section_id": section_id, "consistent": False, "skipped": disabled,
                "changed": True, "diffs": [], "error": str(e)}
    if not changed:
        return {"section_id": section_id, "consistent": True,
                "skipped": disabled, "changed": False, "diffs": []}
    return {"section_id": section_id, "consistent": False, "changed": True,
            "skipped": disabled, "diffs": _diff_content_lines(content, new_content)}


async def validate_scheme_numbering_consistency(db, scheme_id: str) -> dict:
    """批量校验方案内所有含正文章节的编号一致性，返回汇总报告。

    ✅ 性能（2026-09-27）：预取一次编号索引并透传给逐章校验，
    把「1 + 2N 次查询」压到「3 次固定查询」。旧实现 200 章 = 401 次 execute / 480ms，
    而本函数在**每次导出**都会跑（`export.py::_guard_numbering_consistency`）。
    预取失败时索引为空，调用方自动回退逐章查询，行为与旧版一致。
    """
    try:
        cur = await db.execute(
            "SELECT id, content FROM sections WHERE scheme_id=? "
            "AND COALESCE(content,'')!=''",
            (scheme_id,))
        rows = await cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"scheme_id": scheme_id, "checked": 0, "mismatched": 0,
                "consistent": True, "sections": [], "error": str(e)}
    index = await load_scheme_section_index(db, scheme_id)
    sections = []
    mismatched = 0
    for r in rows:
        rep = await validate_section_content_numbering(
            db, scheme_id, r["id"], r["content"] or "", index=index)
        sections.append(rep)
        if not rep.get("consistent"):
            mismatched += 1
    return {"scheme_id": scheme_id, "checked": len(sections),
            "mismatched": mismatched, "consistent": mismatched == 0,
            "sections": sections}


async def repair_scheme_numbering_consistency(db, scheme_id: str) -> dict:
    """将落库正文子标题编号按当前 outline 重新规范化（修复 D4 类漂移）并落库。

    仅对「不一致且未跳过」的章节写回规范化结果；单章失败仅告警、不阻断其余章节。
    ✅ 编号版本管理（2026-09-26）：修复前对全部漂移章节建 numbering_repair 快照
    （scheme_snapshots，复用 repair_record.create_snapshot），返回值带 snapshot_id，
    支持经 /numbering-consistency/rollback/{snapshot_id} 一键回滚；无漂移不建快照。
    返回 {fixed, total, report, snapshot_id}。
    """
    from app.services.content_utils import text_word_count
    from app.services.repair_record import create_snapshot
    report = await validate_scheme_numbering_consistency(db, scheme_id)
    drifted = [s for s in report.get("sections", [])
               if not s.get("consistent") and not s.get("skipped")]
    if not drifted:
        return {"fixed": 0, "total": report["checked"], "report": report,
                "snapshot_id": None}
    # ✅ 性能（2026-09-27）：快照与修复两段各自逐章 SELECT content（各 N 次），
    #    修复段还逐章调 normalize（再 2N 次）。实测 200 章漂移方案合计 >1000 次
    #    execute。这里各用**一次批量查询**取回 content 字典，两段共用。
    content_by_id: dict = {}
    if drifted:
        try:
            ids = [s["section_id"] for s in drifted if s.get("section_id")]
            if ids:
                qs = ",".join("?" * len(ids))
                cur = await db.execute(
                    f"SELECT id, content FROM sections WHERE scheme_id=? "
                    f"AND id IN ({qs})", (scheme_id, *ids))
                content_by_id = {r["id"]: (r["content"] or "") for r in await cur.fetchall()}
        except Exception as e:  # noqa: BLE001 — 取不到就回退逐章查询，不阻断修复
            logger.warning("批量读取漂移章节正文失败（回退逐章读取）: %s", e)
            content_by_id = {}
    # ✅ 版本管理：写库前快照（漂移章节写库前即可由报告确定，无需边修边记；
    #    create_snapshot 自带 commit，位于所有正文写操作之前）
    snapshot_id = None
    try:
        snap_rows = []
        for sec in drifted:
            sid = sec["section_id"]
            if sid in content_by_id:
                content_before = content_by_id[sid]
            else:
                cur = await db.execute(
                    "SELECT content FROM sections WHERE id=? AND scheme_id=?",
                    (sid, scheme_id))
                row = await cur.fetchone()
                if not row:
                    continue
                content_before = row["content"] or ""
            snap_rows.append({"section_id": sid, "content_before": content_before})
        if snap_rows:
            snapshot_id = await create_snapshot(
                db, scheme_id, snap_rows, snapshot_type="numbering_repair")
    except Exception as e:  # noqa: BLE001 — 快照失败不阻断修复（回退为无版本修复）
        logger.warning("编号修复前快照失败（继续修复，本次无版本记录）: %s", e)
        snapshot_id = None
    # 复用 validate 已建好的索引，避免修复段再逐章查元数据
    index = await load_scheme_section_index(db, scheme_id)
    fixed = 0
    for sec in report.get("sections", []):
        if sec.get("consistent") or sec.get("skipped"):
            continue
        sec_id = sec["section_id"]
        try:
            if sec_id in content_by_id:
                content = content_by_id[sec_id]
            else:
                cur = await db.execute(
                    "SELECT content FROM sections WHERE id=? AND scheme_id=?",
                    (sec_id, scheme_id))
                row = await cur.fetchone()
                if not row:
                    continue
                content = row["content"] or ""
            new_content, changed = await normalize_section_content_subheadings(
                db, scheme_id, sec_id, content, index=index)
            if changed:
                await db.execute(
                    "UPDATE sections SET content=?, word_count=? WHERE id=? AND scheme_id=?",
                    (new_content, text_word_count(new_content), sec_id, scheme_id))
                fixed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("章节 %s 编号修复失败（保留原正文）: %s", sec_id[:8], e)
    await db.commit()
    return {"fixed": fixed, "total": report["checked"], "report": report,
            "snapshot_id": snapshot_id}


# ============================================================
# 编号版本管理 / 回滚（2026-09-26 ·「八」遗留建议收口）
# ============================================================
_NUMBERING_SNAPSHOT_TYPES = ("numbering_repair", "numbering_rollback")


async def rollback_numbering_version(db, scheme_id: str, snapshot_id: str) -> dict:
    """编号版本一键回滚：把指定 numbering_* 快照涉及章节的正文恢复为快照前内容。

    复用 repair_record.rollback_snapshot 引擎（回滚前自动建 undo 快照保证可撤销、
    word_count/word_status 按全项目唯一口径重算）。安全约束：
      - 快照必须存在；
      - 必须属于当前方案；
      - type 必须为 numbering_*（一致性修复快照走 /consistency/rollback，互不串用）。
    失败抛 ValueError（路由层转 404/400）。
    """
    from app.services.repair_record import get_snapshot, rollback_snapshot
    snap = await get_snapshot(db, snapshot_id)
    if not snap:
        raise ValueError("快照不存在")
    if snap.get("scheme_id") != scheme_id:
        raise ValueError("快照不属于当前方案")
    if snap.get("type") not in _NUMBERING_SNAPSHOT_TYPES:
        raise ValueError(
            f"快照类型 {snap.get('type')!r} 不属于编号版本管理，请使用对应模块的回滚端点")
    res = await rollback_snapshot(db, snapshot_id, undo_type="numbering_rollback")
    return {**res, "scheme_id": scheme_id, "snapshot_type": snap.get("type")}


async def list_numbering_versions(db, scheme_id: str, limit: int = 20) -> dict:
    """编号版本历史：列出 numbering_repair / numbering_rollback 快照（新→旧）。

    返回 {scheme_id, versions:[{snapshot_id, type, created_at, section_count,
    section_ids}]}；limit 上限 50（与 repair_record.list_repairs 同口径）。
    """
    limit = max(1, min(int(limit or 20), 50))
    try:
        cur = await db.execute(
            "SELECT id, type, sections, created_at FROM scheme_snapshots "
            "WHERE scheme_id=? AND type IN (?,?) "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (scheme_id, *_NUMBERING_SNAPSHOT_TYPES, limit))
        rows = await cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"scheme_id": scheme_id, "versions": [], "error": str(e)}
    versions = []
    for r in rows:
        try:
            secs = json.loads(r["sections"] or "[]")
        except Exception:  # noqa: BLE001 — 坏行跳过，不影响其余版本展示
            secs = []
        versions.append({
            "snapshot_id": r["id"],
            "type": r["type"],
            "created_at": r["created_at"],
            "section_count": len(secs),
            "section_ids": [s.get("section_id") for s in secs
                            if isinstance(s, dict) and s.get("section_id")],
        })
    return {"scheme_id": scheme_id, "versions": versions}


def _diff_content_lines(old: str, new: str) -> list[dict]:
    """精简 diff：只返回有差异的行（按 difflib  opcode 汇总）。"""
    import difflib
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines).get_opcodes():
        if tag == "equal":
            continue
        out.append({"tag": tag,
                    "old": old_lines[i1:i2],
                    "new": new_lines[j1:j2]})
    return out
