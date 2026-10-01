"""标题编号规范对齐回归（2026-09-23 · 编号命名空间统一批次）

编号权威口径 = heading_v2.HeadingNumberingGeneratorV2（导出）+ 前端 outlineTreeLogic
（slice(-(level-1))）+ HEADING_STYLE_CONFIG（2026-09 规范）：
    L1 第一章  /  L2 1  /  L3 1.1  /  L4 1.1.1  /  L5 1.1.1.1、
本文件锁定五处历史漂移的修复（任一处回退即红）：
  BUG-A create_section 不剥离标题内嵌编号（PATCH 已剥、POST 漏 → 双重编号）
  BUG-B DEFAULT_NUMBERING_TEMPLATES L2~L5 用 {full}（含章号全路径），与展示口径分叉
  BUG-C build_heading_spec_prompt L5 仍写「（X）中文括号」、示例「2.3 / 2.3.1」
        与实现（第二章第 3 节 → 3.1 / 3.1.1）相反
  BUG-D heading_v1.generate_number(L5) 返回（一），且 HEADING_REGEX_PATTERNS
        无法识别四位十进制 L5（1.1.1.1 标题 → level 0）
  BUG-E prompts/content.py 正文小标题规范（1 / 1.1 /（一））与导出重排
        （_compute_subheading 节号.序号链）不一致

测试策略：直接调用路由/服务函数 + 内存 sqlite（conftest.db_conn），
纯函数用例无需 db；asyncio_mode=auto（pyproject.toml）。
"""
import inspect
import uuid

# 触发 content_generation_system 注册（_reg 在模块 import 期执行）
import app.services.ai.prompts.content  # noqa: F401
from app.models import SectionCreate
from app.routers import sse_handlers as sh
from app.routers.export import _compute_subheading
from app.routers.sections import create_section
from app.services.ai.heading_templates import (
    build_heading_spec_prompt,
    build_subheading_rule,
    format_heading_by_id,
    get_heading_template,
)
from app.services.ai.heading_v1 import HeadingNumberingGenerator
from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
from app.services.ai.json_response import strip_outline_numbering
from app.services.ai.prompts._registry import get_default_prompt


# ============================================================
# BUG-A：create_section 标题内嵌编号剥离（双重编号防御）
# ============================================================
class TestCreateSectionStripsTitleNumber:
    async def _seed_scheme(self, db):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "项目"))
        await db.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
            (sid, pid, "方案"))
        await db.commit()
        return sid

    async def _title_of(self, db, section_id):
        cur = await db.execute("SELECT title FROM sections WHERE id=?", (section_id,))
        row = await cur.fetchone()
        return row[0]

    async def test_strip_dotted_number(self, db_conn):
        """粘贴带号标题 "1.1 编制依据" → 落库 "编制依据"（与 PATCH 路径同口径）"""
        sid = await self._seed_scheme(db_conn)
        r = await create_section(sid, SectionCreate(title="1.1 编制依据"), db=db_conn)
        assert await self._title_of(db_conn, r["id"]) == "编制依据"

    async def test_strip_chinese_bracket_number(self, db_conn):
        """"（三）设计标准" → 落库 "设计标准"（（X）为无歧义结构直接剥离）"""
        sid = await self._seed_scheme(db_conn)
        r = await create_section(sid, SectionCreate(title="（三）设计标准"), db=db_conn)
        assert await self._title_of(db_conn, r["id"]) == "设计标准"

    async def test_pure_number_title_fallback(self, db_conn):
        """标题本身即编号（剥离后为空）→ 回退原标题，不丢内容"""
        sid = await self._seed_scheme(db_conn)
        r = await create_section(sid, SectionCreate(title="1.1"), db=db_conn)
        assert await self._title_of(db_conn, r["id"]) == "1.1"

    async def test_plain_title_untouched(self, db_conn):
        """无编号标题原样入库（反例回归：不得误伤正常标题）"""
        sid = await self._seed_scheme(db_conn)
        r = await create_section(sid, SectionCreate(title="2023年规范编制说明"), db=db_conn)
        assert await self._title_of(db_conn, r["id"]) == "2023年规范编制说明"

    def test_strip_keeps_safe_titles(self):
        """strip_outline_numbering 安全性：常见非编号开头不被误剥"""
        assert strip_outline_numbering("2023年规范") == "2023年规范"
        assert strip_outline_numbering("十二层平面布置") == "十二层平面布置"
        assert strip_outline_numbering("3D打印施工方案") == "3D打印施工方案"
        # 整体即纯数字路径 → 原样返回（不得只剥半截 "1.1" → "1"）
        assert strip_outline_numbering("1.1") == "1.1"
        assert strip_outline_numbering("1.2.3") == "1.2.3"




# ============================================================
# BUG-B + BUG-D：三套生成器/模板口径统一
# （V2 计数器 == ID 模板路径 == 前端 slice(-(level-1))）
# ============================================================
class TestNumberingNamespaceUnified:
    def test_default_templates_use_relative_path(self):
        """L2~L5 默认模板取末 N 段，与展示口径一致（反例：{full} 含章号全路径）"""
        assert get_heading_template(2)["numbering_template"] == "{num}"
        assert get_heading_template(3)["numbering_template"] == "{last2}"
        assert get_heading_template(4)["numbering_template"] == "{last3}"
        assert get_heading_template(5)["numbering_template"] == "{last4}"
        assert "{full}" not in str(get_heading_template(3))
        assert "{full}" not in str(get_heading_template(4))
        assert "{full}" not in str(get_heading_template(5))

    def test_format_heading_by_id_matches_frontend_slice(self):
        """ID 模板输出 == 前端 slice(-(level-1)) 拼接（outlineTreeLogic 口径）"""
        cases = [
            ("2.3", 2, "进度计划", "3 进度计划"),
            ("2.3.1", 3, "分项工程", "3.1 分项工程"),
            ("2.3.1.1", 4, "细部做法", "3.1.1 细部做法"),
            ("2.3.1.1.1", 5, "设计标准", "3.1.1.1、设计标准"),
        ]
        for id_str, level, title, expected in cases:
            # 前端口径：slice(-(level-1)) 后 ".".join
            parts = id_str.split(".")[1:] if level >= 2 else id_str.split(".")
            assert ".".join(parts[-(level - 1):]) + ("、" if level == 5 else " ") + title == expected
            assert format_heading_by_id(id_str, level, title) == expected

    def test_v2_counter_matches_id_template(self):
        """V2 计数器输出与 ID 模板输出逐级相等（第二章第 3 节场景）"""
        gen = HeadingNumberingGeneratorV2()
        gen.format_heading(1, "章一", "root")
        gen.format_heading(1, "章二", "root")
        gen.format_heading(2, "占位节A", "ch2")
        gen.format_heading(2, "占位节B", "ch2")
        t2 = gen.format_heading(2, "进度计划", "ch2")
        t3 = gen.format_heading(3, "分项工程", "sec3")
        t4 = gen.format_heading(4, "细部做法", "s31")
        t5 = gen.format_heading(5, "设计标准", "s311")
        assert t2 == format_heading_by_id("2.3", 2, "进度计划") == "3 进度计划"
        assert t3 == format_heading_by_id("2.3.1", 3, "分项工程") == "3.1 分项工程"
        assert t4 == format_heading_by_id("2.3.1.1", 4, "细部做法") == "3.1.1 细部做法"
        assert t5 == format_heading_by_id("2.3.1.1.1", 5, "设计标准") == "3.1.1.1、设计标准"

    def test_v1_l5_is_decimal(self):
        """BUG-D 反例回归：V1 L5 输出十进制 X.X.X.X + 顿号，不是（一）"""
        counters = [1, 1, 1, 1, 1, 0, 0, 0]
        num = HeadingNumberingGenerator.generate_number(5, counters)
        assert num == "1.1.1.1"
        assert "（" not in num
        formatted = HeadingNumberingGenerator.format_heading(5, counters, "设计标准")
        assert formatted == "1.1.1.1、设计标准"

    def test_detect_level_decimal_l5(self):
        """BUG-D 反例回归：四位十进制识别为五级；中文括号旧格式仍兼容"""
        assert HeadingNumberingGenerator.detect_level("1.1.1.1、设计标准") == (5, "设计标准")
        assert HeadingNumberingGenerator.detect_level("1.1.1.1 设计标准") == (5, "设计标准")
        # 旧中文括号格式保留识别（历史数据兼容）
        assert HeadingNumberingGenerator.detect_level("（一）设计标准") == (5, "设计标准")
        # 低层级不受新增正则影响（反例）
        assert HeadingNumberingGenerator.detect_level("1.1.1 立面概况") == (4, "立面概况")
        assert HeadingNumberingGenerator.detect_level("1.1 地理位置") == (3, "地理位置")
        assert HeadingNumberingGenerator.detect_level("1 工程概况") == (2, "工程概况")

    def test_detect_level_not_confused_by_long_decimal(self):
        """五段十进制（1.1.1.1.1）不得被四位正则误判成五级标题"""
        level, _ = HeadingNumberingGenerator.detect_level("1.1.1.1.1 过深标题")
        assert level != 5

# ============================================================
# BUG-C：build_heading_spec_prompt 与生成器对齐
# ============================================================
class TestSpecPromptAligned:
    def test_l5_example_is_decimal(self):
        prompt = build_heading_spec_prompt()
        assert "（一） 设计标准" not in prompt
        assert "1.1.1.1、设计标准" in prompt

    def test_examples_use_section_relative_numbering(self):
        """示例编号为节内相对路径：第二章第 3 节 → 3.1 / 3.1.1 / 3.1.1.1"""
        prompt = build_heading_spec_prompt()
        assert "2.3、四级为 2.3.1" not in prompt  # 旧反例：含章号全路径
        assert "3.1.1、五级为 3.1.1.1" in prompt

    def test_punctuation_rule_matches_style_config(self):
        """分隔符规则与 HEADING_STYLE_CONFIG 一致：L1~L4 空格、L5~L7 顿号"""
        from app.services.ai.heading_templates import HEADING_STYLE_CONFIG
        assert HEADING_STYLE_CONFIG[2]["punctuation"] == " "
        assert HEADING_STYLE_CONFIG[3]["punctuation"] == " "
        assert HEADING_STYLE_CONFIG[4]["punctuation"] == " "
        assert HEADING_STYLE_CONFIG[5]["punctuation"] == "、"
        prompt = build_heading_spec_prompt()
        assert "顿号" in prompt


# ============================================================
# BUG-E：正文提示词小标题规范与导出重排一致
# ============================================================
class TestContentPromptAlignedWithExport:
    def test_content_prompt_subheading_levels(self):
        """content_generation_system 小标题链 = 节号.序号链，五级十进制。

        ✅ 2026-09-25（陈旧断言修复 · E3 重构对齐）：小标题规范在 E3 中改为
        **运行时条件注入** —— 模板里是占位符 `{subheading_rule}`，实际文案由
        `generate_content` 按「本章是否有 DB 子章节 + body_subheading_demote_with_children」
        二选一（节内 body 命名空间 `N）/ a、` vs 点分命名空间 `1.1`）。
        旧断言直接在校验**出厂模板文本**里找 "1.1 标题"，模板早已不含该文案
        （只剩占位符）→ 必然失败。改为对**真正下发给模型的文案**断言：
        点分规则仍在（且必须仍是五级十进制、不得回归中文括号）。
        """
        tpl = get_default_prompt("content_generation_system")
        assert tpl, "content_generation_system 出厂模板不应为空"
        # 模板保留占位符（文案运行时注入，两种命名空间按需选择）
        assert "{subheading_rule}" in tpl, (
            "模板应保留 {subheading_rule} 占位符供运行时条件注入"
        )
        assert "（一） 标题（中文数字加全角括号）" not in tpl

        # ✅ 2026-09-26：文案已从 generate_content 内部迁到
        #    heading_templates.build_subheading_rule（唯一实现）。旧断言在
        #    `inspect.getsource(generate_content)` 里找 "_sub_rule = ("，
        #    该局部变量早已随模板化重构消失 → ValueError。
        #    改为直接对**真正下发给模型的文案**断言（点分 + 降级两种命名空间）。
        normal = build_subheading_rule(has_db_children=False)
        assert "- 二级小标题：1.1 标题" in normal
        assert "- 三级小标题：1.1.1 标题" in normal
        assert "- 四级小标题：1.1.1.1 标题" in normal
        assert "- 五级小标题：1.1.1.1.1 标题" in normal
        # 旧断号示例（第 2 节从 2 起）不得回归
        assert "小标题从 2、2.1、2.1.1 起" not in normal
        # E3：body 命名空间分支必须与降级开关同在（否则正文/子章节撞号无提示）
        demoted = build_subheading_rule(has_db_children=True)
        # ✅ 编号统一（2026-09-26）：降级命名空间 L6 文案须与导出实际产出一致（'1）、标题'，顿号）
        assert "N）、标题" in demoted and "字母、标题" in demoted
        assert "1.1 标题" not in demoted

    def test_subheading_rule_actually_reaches_prompt(self):
        """✅ BUG-8 回归：{subheading_rule} 必须真的被注入，否则整段规范为空。

        该占位符自登记进 PROMPT_VARIABLE_CONTRACTS 起就**无任何生成方**，
        render 时被丢弃 —— 发给模型的提示词是
        「章节内部小标题编号规范（如需分层组织内容时使用）：」+ 空行，
        AI 收不到编号规范只能自由发挥。本用例锁定「占位符不再残留」。
        """
        from app.services.ai.prompts._registry import render as _render
        for has_children in (False, True):
            out = _render(
                "content_generation_system",
                scheme_name="X", scheme_type="Y",
                section_number="1.1", standards_text="",
                subheading_rule=build_subheading_rule(
                    has_db_children=has_children))
            assert "{subheading_rule}" not in out, "占位符未被替换"
            i = out.find("章节内部小标题编号规范")
            assert i != -1, "提示词应保留小标题规范小节标题"
            seg = out[i:i + 200]
            # 规范标题之后必须紧跟实质内容，不能是空行
            body = seg.split("：", 1)[1].lstrip() if "：" in seg else ""
            assert body.strip(), f"小标题规范内容为空（has_children={has_children}）"
            assert "标题" in body

    def test_export_subheading_matches_prompt_examples(self):
        """提示词示例与导出 _compute_subheading 实际产出一致"""
        # 提示词：本节编号 1 时二级 = 1.1
        text, level = _compute_subheading("1", 1, 2, {}, "编制依据", "sec-1")
        assert text == "1.1 编制依据"
        assert level == 2
        # 提示词：本节编号为 2 时二级 = 2.1、三级 = 2.1.1
        counters: dict = {}
        text2, _ = _compute_subheading("2", 1, 2, counters, "管理体系", "sec-2")
        assert text2 == "2.1 管理体系"
        text3, _ = _compute_subheading("2", 1, 3, counters, "组织机构", "sec-2")
        assert text3 == "2.1.1 组织机构"

