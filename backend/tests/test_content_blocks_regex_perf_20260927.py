"""content_blocks 热路径正则预编译的护栏（2026-09-27 性能轮）。

背景（实测数据）
----------------
`_parse_content_blocks` 是**导出与正文落库共用的最热路径**（每章必跑）。
cProfile（200 章正文 ×10）显示优化前 `re.__init__._compile` 被调用 278 次/次解析
—— 全部来自调用点上的**内联** `re.match(r"...", s)` / `re.sub(r"...", s)`：
每次都要走一次模块级正则缓存的字典查找。

同机 timeit A/B（20 万次）：
    内联 re.match 0.871 us/op  vs  预编译 .match 0.285 us/op  → 3.06x
    内联 re.sub   0.911 us/op  vs  预编译 .sub   0.516 us/op  → 1.77x

本文件锁定：
1. **源码结构**：调用点不得再出现内联 `re.match/search/sub(...)` 字面量
   （性能护栏主体 —— 计数断言会被「换个函数名」绕过）；
2. **行为等价**：14 组边界输入下，预编译版与内联版输出**逐字节一致**
   （把调用点动态还原成内联再对拍，而非只断言「没抛异常」）；
3. **性能**：解析耗时的宽松上界 + 运行时「零正则缓存查找」断言。

复跑：``python -m pytest tests/test_content_blocks_regex_perf_20260927.py -q``
"""
import importlib
import inspect
import json
import re
import sys
import time

from app.services import content_blocks as cb

# 14 组覆盖各条分支的边界输入：MD 标题 / 纯文本编号 / 加粗包裹 /
# 有序列表 / 引用 / 分隔线 / 无序列表 / 表格 / 围栏 / CHART_TYPE 标记 /
# 图片行 / 句末标点排除 / 数字误判防护
_CASES = [
    "## 1.1 标题\n正文一段。\n### 1.1.1 子标题\n内容",
    "**2.3 加粗标题**\n1）第一条要求\n2）第二条要求",
    "1.2 施工现场准备\n见下图所示：流程如下",
    "普通段落，没有编号，也不该是标题。\n下一段。",
    "| 参数 | 数值 |\n| --- | --- |\n| a | 1 |",
    "```mermaid\ngraph TD\n A-->B\n```\n[CHART_TYPE: gantt]\n图 1-1 流程图",
    "> 引用行一\n> 引用行二\n\n---\n\n- 列表 A\n- 列表 B",
    "![下图](https://x/y.png)\n\n正文",
    "2023 年完成的工程，100 人团队参与。",
    "3.14 是圆周率，不是标题。",
    "表 1-1 主要参数表\n| a | b |",
    "1) 括号列表一\n2) 括号列表二",
    "a. 字母编号\nb. 另一个",
    "第一段以句号结尾。\n第二段。\n3.1 这是标题",
]


def _snapshot_for(mod) -> str:
    """把模块的全部相关行为序列化成字符串，用于新旧实现逐字节对拍。"""
    out = []
    for c in _CASES:
        out.append(mod._parse_content_blocks(c))
        out.append(mod._detect_plain_heading(c.split("\n")[0]))
        out.append(mod._clean_chart_title("  图  1-1  流程图  "))
        out.append(mod._title_candidate("见下图所示： 流程图  "))
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def _snapshot() -> str:
    return _snapshot_for(cb)


# 预编译版 ↔ 内联版 的调用点对照表（对拍时据此把模块临时改回旧写法）
_REVERSE_MAP = [
    ('_RE_BOLD_WRAP.sub(r"\\1", s, count=1)',
     're.sub(r"^\\*\\*(.*?)\\*\\*$", r"\\1", s, count=1)'),
    ("_RE_SENTENCE_TAIL_FULL.search(s_inside)",
     're.search(r"[。！？.!?；;，,、]\\s*$", s_inside)'),
    ("_RE_HEADING_NNN.match(s_inside)",
     're.match(r"^(\\d+\\.\\d+\\.\\d+)\\s+([\\u4e00-\\u9fa5A-Za-z].*)$", s_inside)'),
    ("_RE_HEADING_DEEP.match(s_inside)",
     're.match(r"^(\\d+(?:\\.\\d+){3,6})\\s+([\\u4e00-\\u9fa5A-Za-z].*)$", s_inside)'),
    ("_RE_HEADING_NN.match(s_inside)",
     're.match(r"^(\\d+\\.\\d+)\\s+([\\u4e00-\\u9fa5A-Za-z].*)$", s_inside)'),
    ("_RE_HEADING_N.match(s_inside)",
     're.match(r"^(\\d+)\\s+([\\u4e00-\\u9fa5A-Za-z].*)$", s_inside)'),
    ('_RE_BOLD_WRAP.sub(r"\\1", text).strip()',
     're.sub(r"^\\*\\*(.*?)\\*\\*$", r"\\1", text).strip()'),
    ('_RE_WS_RUN.sub("", str(raw or "").strip())[:limit]',
     're.sub(r"\\s+", "", str(raw or "").strip())[:limit]'),
    ('t = _RE_WS_RUN.sub("", str(raw or "").strip())',
     't = re.sub(r"\\s+", "", str(raw or "").strip())'),
    ("_RE_MD_HEADING.match(stripped)", 're.match(r"^(#{1,6})\\s+(.*)", stripped)'),
    ("_RE_SENTENCE_TAIL.search(_h_text)",
     're.search(r"[。！？；;，,]\\s*$", _h_text)'),
    ("_RE_TABLE_SEP.match(tbl_lines[1].strip())",
     're.match(r"^\\|?\\s*:?-+:?\\s*(\\|\\s*:?-+:?\\s*)*\\|?$", tbl_lines[1].strip())'),
    ("_RE_CHART_TYPE_TAG.match(line.strip())",
     're.match(r"^\\[CHART_TYPE:\\s*(\\w+)\\]", line.strip())'),
    ('_RE_QUOTE_PREFIX.sub("", lines[i]).rstrip()',
     're.sub(r"^\\s*>\\s?", "", lines[i]).rstrip()'),
    ("_RE_HR_LINE.match(stripped)", 're.match(r"^([-*_])(\\s*\\1){2,}$", stripped)'),
    ("_RE_UL_BULLET.match(stripped)", 're.match(r"^[-*•·]\\s+(.*)", stripped)'),
    ('s = _RE_INLINE_MD_NOISE.sub("", s)', 's = re.sub(r"[*_`#]", "", s)'),
    ('s = _RE_TITLE_TAIL_PUNCT.sub("", s)',
     's = re.sub(r"[\\s、.。：:；;，,]+$", "", s)'),
    ('return _RE_WS_RUN.sub("", s)', 'return re.sub(r"\\s+", "", s)'),
]



class TestNoInlineRegexOnHotPath:
    """性能护栏主体：调用点不得再内联正则字面量。"""

    def test_no_inline_re_calls_in_module(self):
        """整模块不得出现内联 `re.match/search/sub/...` 调用。

        注释里提到 `re.match` 是允许的（判定会先剔除注释行）。
        """
        src = inspect.getsource(cb)
        code_lines = [ln.split("  #")[0] for ln in src.splitlines()
                      if not ln.strip().startswith("#")]
        code = "\n".join(code_lines)
        offenders = re.findall(
            r"(?<![_.A-Za-z])re\.(?:match|search|sub|findall|split|fullmatch)\(",
            code)
        assert not offenders, (
            f"content_blocks 出现 {len(offenders)} 处内联正则调用，热路径性能回退")

    def test_precompiled_constants_are_patterns(self):
        """新增的 `_RE_*` 必须是已编译对象。"""
        names = [n for n in dir(cb) if n.startswith("_RE_")]
        assert names, "预编译常量丢失"
        for n in names:
            assert isinstance(getattr(cb, n), re.Pattern), f"{n} 不是已编译正则"

    def test_parse_blocks_source_has_no_inline_re(self):
        """最热函数 `_parse_content_blocks` 自身零内联正则。"""
        src = inspect.getsource(cb._parse_content_blocks)
        assert not re.search(
            r"(?<![_.A-Za-z])re\.(?:match|search|sub|findall|split)\(", src), \
            "_parse_content_blocks 出现内联正则调用"


class TestPrecompiledBehaviourParity:
    """行为等价：预编译版输出必须与「内联版」逐字节一致。"""

    def test_reverse_map_covers_every_precompiled_usage(self):
        """对照表本身不能腐化：每个被使用的 `_RE_*` 都必须在表里。

        否则「对拍通过」可能只是因为没真正还原到旧写法。
        """
        src = inspect.getsource(cb)
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("_RE_"))
        # 用正则取名，避免 `s = _RE_X.sub(...)` 这类带赋值前缀的行取错名字
        used = {m for m in re.findall(r"\b(_RE_[A-Z0-9_]+)\.", body)}
        mapped = {re.match(r"(?:s = |t = |return )?(_RE_[A-Z0-9_]+)\.", a).group(1)
                  for a, _ in _REVERSE_MAP}
        assert not (used - mapped), f"新增预编译常量未加入对拍表: {used - mapped}"

    def test_output_identical_to_inline_version(self):
        """把调用点动态还原成内联写法，两版输出必须逐字节相同。

        ⚠️ 关键实现约束：**不得**把还原后的模块写回磁盘再 import。
        那样会重建 `sys.modules` 里的模块对象，使 `export._parse_content_blocks
        is content_blocks._parse_content_blocks` 这类**身份断言**在
        同一次全量运行中被别的用例判红（模块级 import 只绑定一次）。
        正确做法：在**独立命名空间**里 exec 还原后的源码，不触碰 sys.modules。
        """
        import types

        new_out = _snapshot()
        with open(inspect.getsourcefile(cb), encoding="utf-8") as f:
            original_src = f.read()
        reverted = original_src
        for new, old in _REVERSE_MAP:
            reverted = reverted.replace(new, old)
        assert reverted != original_src, "对拍失败：未能还原任何内联调用"

        # 独立命名空间执行：绝不写盘、绝不进 sys.modules
        old_mod = types.ModuleType("_cb_inline_variant")
        old_mod.__file__ = inspect.getsourcefile(cb)
        old_mod.__package__ = cb.__package__
        exec(compile(reverted, old_mod.__file__, "exec"), old_mod.__dict__)
        old_out = _snapshot_for(old_mod)

        assert old_out, "旧实现快照为空，对拍无效"
        assert new_out == old_out, "预编译改造改变了输出结果"
        # 顺带确认没有污染全局模块表
        assert sys.modules["app.services.content_blocks"] is cb, \
            "对拍污染了 sys.modules（会导致身份类断言在同批次失败）"


class TestParseBlocksPerfBudget:
    """性能：宽松上界 + 运行时「零正则缓存查找」断言。"""

    def test_800_sections_within_budget(self):
        parts = []
        for i in range(800):
            parts += [f"## {i}.1 工艺流程", f"第{i}章正文内容示例。" * 8,
                      "```mermaid\ngraph TD\n A-->B\n```", "- 项目A", "- 项目B",
                      "| 参数 | 数值 |\n| --- | --- |",
                      f"### {i}.2 验收要求", "1）第一条要求", "2）第二条要求",
                      "![下图](https://x/y.png)", "**2.3 加粗标题**", "见下图所示："]
        content = "\n".join(parts)
        cb._parse_content_blocks(content)  # 预热
        t0 = time.perf_counter()
        blocks = cb._parse_content_blocks(content)
        dt = time.perf_counter() - t0
        assert len(blocks) > 5000, "解析结果明显漏解析"
        assert dt < 5.0, f"800 章解析耗时 {dt:.3f}s，疑似数量级退化"

    def test_regex_cache_lookups_eliminated(self):
        """运行时护栏：预热后 `re._compile` 不应再被本模块调用。

        命中缓存时 `re._compile` 同样会被调用，所以先预热把模式灌进
        `re._cache`，再统计单次解析期间的调用次数。
        """
        content = "\n".join(["## 1.1 标题", "正文示例。" * 20,
                             "1.2 准备", "1）要求", "- 列表", "---",
                             "> 引用", "```mermaid\ngraph TD\n A-->B\n```"])
        cb._parse_content_blocks(content)          # 预热：灌满 re._cache
        calls = {"n": 0}
        original = re._compile

        def counted(pattern, flags):
            calls["n"] += 1
            return original(pattern, flags)

        re._compile = counted
        try:
            cb._parse_content_blocks(content)
        finally:
            re._compile = original
        assert calls["n"] == 0, (
            f"单次解析仍触发 {calls['n']} 次正则缓存查找（优化前为 278）")
