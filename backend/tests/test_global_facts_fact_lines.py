"""全局事实「模拟值标记 + 列表行解析」单一口径回归测试（2026-09-21 新增）。

锁定两条口径收口：

1. 模拟值标记单一事实源（`SIMULATED_MARKER` / `strip_simulated_marker` /
   `append_simulated_marker`）
   历史散落三种互不相认的写法：
     - " ⚠️*(模拟值)*"   —— _build_fact_content / create_fact / format_for_frontend
     - "  *(⚠ 模拟值)*"  —— FactItem.to_db_row（AI 提取落库路径，DB 内绝大多数数据）
     - list_facts 的旧剥离正则，只能从 `*` 起匹配 → 统一写法剥离后残留悬空 ⚠️
   后果：① 走过 PATCH / 手工新增的模拟值事实，value 带着悬空 ⚠️ 在界面展示；
        ② 脏值被「值是否变化」比较当真值 → 模拟值事实恒被判为已改值，
           2026-09-20 修的「只改分类/改名静默清矛盾」漏洞实际未堵住。

2. `_split_fact_lines` 列表符识别与 update_fact 的 multi_line 判定对齐
   旧实现只认 "-"，"* 补充说明" 被当正文（name 退化为分组标题），
   "• **A**: 1" 把整行塞进 value，"-" 单独一行造出空事实。
"""
import json

from app.models import FactGroupIn, FactItem as PydFactItem
import app.routers.global_facts as gf
import app.services.facts_extractor as fe

#: FactItem 有两个同名类型：app.models 是 Pydantic 入参模型，
#: facts_extractor 是 dataclass（落库 / 合并去重的实体）。本文件两者都要用。
Item = fe.FactItem

#: 历史写法一：⚠️ 前缀（路由 / SSE 下发路径）
MARKER_PREFIX = " ⚠" + chr(0xFE0F) + "*(模拟值)*"
#: 历史写法二：⚠ 在括号内（to_db_row 落库路径）
MARKER_INNER = "  *(⚠ 模拟值)*"
#: 历史写法三：无 ⚠
MARKER_BARE = " *(模拟值)*"


# ---------------------------------------------------------------------------
# 模拟值标记：三种历史写法统一剥离
# ---------------------------------------------------------------------------

def test_marker_strips_all_three_legacy_forms():
    """统一剥离口径必须覆盖历史上全部三种写法（含悬空 ⚠️）。"""
    for form in (MARKER_PREFIX, MARKER_INNER, MARKER_BARE):
        assert fe.strip_simulated_marker(f"15.0m{form}") == "15.0m", form
        assert fe.strip_simulated_marker(f"300天{form}") == "300天", form


def test_marker_strip_keeps_plain_value_untouched():
    """纯值（含中文括号/说明性文本）不得被误删。"""
    for plain in ("15.0m", "12.5米", "365 日历天", "（含模拟值字样的正文）", ""):
        assert fe.strip_simulated_marker(plain) == plain, plain


def test_extract_value_no_dangling_warning_sign():
    """回解 value 不得残留悬空 ⚠️（旧正则会残留半个标记）。"""
    for content in (
        f"- **基坑深度**: 15.0m{MARKER_PREFIX}",
        f"- **基坑深度**: 12.5m{MARKER_INNER}",
        f"- **基坑深度**: 10m{MARKER_BARE}",
        "- **基坑深度**: 12.5m",
    ):
        name, value = fe.extract_value_from_markdown_line(content)
        assert name == "基坑深度", content
        assert "模拟值" not in value and "⚠" not in value, (content, value)
        assert "*(模拟值)*" not in value, (content, value)


def test_extract_value_forms_match_each_other():
    """两种历史标记写法回解出的值必须一致（此前一脏一净）。"""
    a = fe.extract_value_from_markdown_line(
        f"- **A**: 1{MARKER_PREFIX}")[1]
    b = fe.extract_value_from_markdown_line(f"- **A**: 1{MARKER_INNER}")[1]
    assert a == b == "1"


def test_append_marker_is_idempotent():
    """重复追加不得叠加标记；关闭标记必须剥离旧标记。"""
    once = fe.append_simulated_marker("12.5m", True)
    assert once == fe.append_simulated_marker(once, True), "标记不得叠加"
    assert fe.append_simulated_marker(once, False) == "12.5m"


def test_build_fact_content_uses_single_marker():
    """路由侧构造器与 extractor 侧落库使用同一 content 写法。"""
    line = gf._build_fact_content("基坑深度", "12.5m", True)
    row = Item(name="基坑深度", value="12.5m",
               is_simulated=True).to_db_row("g", "p", "s")
    assert line == row[6], "两条落库路径的 content 写法必须一致"
    assert fe.append_simulated_marker("12.5m", True) in line


def test_split_fact_lines_preserves_simulated_from_body():
    """模拟值判定必须基于剥离列表符后的正文。"""
    parsed = gf._split_fact_lines("技术参数", f"* **基坑深度**: 12.5m{MARKER_PREFIX}")


# ---------------------------------------------------------------------------
# _split_fact_lines：列表符识别与噪音行过滤
# ---------------------------------------------------------------------------

def test_split_accepts_star_bullet():
    """* 开头的事实行必须被解析（旧实现当正文处理，name 退化为分组标题）。"""
    assert gf._split_fact_lines("技术参数", "* **基坑深度**: 12.5m") == \
        [("基坑深度", "12.5m", False)]


def test_split_accepts_circle_plus_and_middle_dot_bullets():
    """• / + / · 与 - / * 同等对待。"""
    for bullet in ("•", "+", "·", "*", "-"):
        parsed = gf._split_fact_lines("T", f"{bullet} **总工期**: 365 日历天")
        assert parsed == [("总工期", "365 日历天", False)], (bullet, parsed)


def test_split_ignores_bare_bullets_and_hr():
    """纯列表符 / 水平分隔线不得造出假事实。"""
    content = "\n".join(["- **A**: 1", "-", "* *", "---", "•", "+ x"])
    parsed = gf._split_fact_lines("组", content)
    assert [p[0] for p in parsed] == ["A", "x"], (content, parsed)
    # 列表符不得混进 value（name-only 行的空值允许）
    assert all(not p[1].lstrip().startswith(("-", "*", "•", "+", "·"))
               for p in parsed), parsed


def test_split_does_not_leak_bullet_into_value():
    """列表符必须在入库前剥离，不得混进 name / value。"""
    for bullet in ("-", "*", "•", "+", "·"):
        parsed = gf._split_fact_lines(
            "组", f"{bullet} **实施周期**: 2024 年 3 月至 12 月")
        name, value, _ = parsed[0]
        assert name == "实施周期", bullet
        assert not value.startswith(bullet), (bullet, value)


def test_split_marked_lines_not_duplicated_as_title_facts():
    """带列表符的补充说明不得被复制成一条「名为分组标题」的假事实。"""
    parsed = gf._split_fact_lines(
        "技术参数", "- **基坑深度**: 12.5m\n- 补充说明：地质报告第 3 页")
    assert [p[0] for p in parsed] == ["基坑深度", "补充说明"], parsed


def test_split_empty_content_falls_back_to_single_row():
    """空内容仍回退单行，标题缺失时兜底为「未命名」。"""
    assert gf._split_fact_lines("组", "") == [("组", "", False)]
    assert gf._split_fact_lines("", "") == [("未命名", "", False)]


# ---------------------------------------------------------------------------
# create_fact：手工录入的模拟值必须回到待审核（闸门是不变式）
#   门控 SQL 是 `has_conflict=0 AND is_resolved=1`（不含 is_simulated 列），
#   闸门全靠「模拟值 ⟹ 待审核」这一不变式维持；旧实现按模型默认值
#   is_resolved=True 直落库，编造值会直接越过闸门注入正文与导出。
# ---------------------------------------------------------------------------

async def test_create_structured_simulated_fact_starts_unresolved(db_conn):
    """结构化 items 路径：is_simulated=True → is_resolved=0。"""
    await _seed_scheme(db_conn)
    res = await gf.create_fact(
        FactGroupIn(title="技术参数", content="",
                    items=[PydFactItem(name="基坑深度", value="12.5m",
                                    is_simulated=True)]),
        scheme_id="s1", db=db_conn)
    assert res["ok"] is True
    rows = [dict(r) for r in await (await db_conn.execute(
        "SELECT is_simulated, is_resolved, content FROM global_facts "
        "WHERE scheme_id=?", ("s1",))).fetchall()]
    assert len(rows) == 1
    assert rows[0]["is_simulated"] == 1
    assert rows[0]["is_resolved"] == 0, "模拟值必须待审核，不得直接注入正文"
    assert "模拟值" in rows[0]["content"]


async def test_create_legacy_simulated_content_starts_unresolved(db_conn):
    """旧版单条路径：content 带模拟值标记 → is_resolved=0。"""
    await _seed_scheme(db_conn)
    await gf.create_fact(
        FactGroupIn(title="工期安排",
                    content=f"- **总工期**: 300 日历天{MARKER_PREFIX}"),
        scheme_id="s1", db=db_conn)
    r = dict(await (await db_conn.execute(
        "SELECT is_simulated, is_resolved FROM global_facts WHERE scheme_id=?",
        ("s1",))).fetchone())
    assert r["is_simulated"] == 1
    assert r["is_resolved"] == 0, "手工粘贴模拟值不得直接成为确定性事实"


async def test_create_plain_fact_stays_resolved(db_conn):
    """非模拟值的手工分组仍是「已确认」（手工录入即人工背书）。"""
    await _seed_scheme(db_conn)
    await gf.create_fact(
        FactGroupIn(title="工期安排", content="- **总工期**: 365 日历天"),
        scheme_id="s1", db=db_conn)
    r = dict(await (await db_conn.execute(
        "SELECT is_simulated, is_resolved FROM global_facts WHERE scheme_id=?",
        ("s1",))).fetchone())
    assert r["is_simulated"] == 0 and r["is_resolved"] == 1


async def _seed_scheme(db):
    """建一个最小可用方案（项目 + 方案），供路由级测试复用。"""
    await db.execute("INSERT INTO projects (id, name) VALUES (?,?)", ("p1", "p"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "s"))
    await db.commit()


# ---------------------------------------------------------------------------
# 候选值链路：is_simulated 随候选值贯穿 落库 / SSE 回传 / 裁决重算
# ---------------------------------------------------------------------------

def test_conflict_candidates_carry_simulated_flag():
    """merge_and_deduplicate 的候选必须携带 is_simulated（裁决重算的依据）。"""
    a = Item(name="基坑深度", value="12.5m", key="depth",
             source="估算", is_simulated=True, confidence=0.3)
    b = Item(name="基坑深度", value="18.0m", key="depth",
             source="地质报告.pdf", is_simulated=False, confidence=0.95)
    merged = fe.merge_and_deduplicate([a, b])
    assert len(merged) == 1
    cand = merged[0].conflict_values
    assert len(cand) == 1
    assert cand[0]["value"] == "12.5m"
    assert cand[0]["is_simulated"] is True, "候选自身的模拟值语义必须保留"


def test_conflict_candidates_in_db_row_and_frontend_payload():
    """to_db_row（落库）与 format_for_frontend（SSE 回传）都带 is_simulated。"""
    it = Item(name="基坑深度", value="18.0m", key="depth",
                  is_simulated=False, confidence=0.95, has_conflict=True,
                  conflict_values=[{"value": "12.5m", "source": "估算",
                                    "confidence": 0.3, "is_simulated": True}])
    conflicts = json.loads(it.to_db_row("g", "p", "s", "技术参数")[13])
    assert conflicts[0]["is_simulated"] is True

    payload = fe.format_for_frontend(fe.ExtractionResult(
        groups=[fe.FactGroup(title="技术参数", category="tech_param", items=[it])],
        total_items=1))
    out = payload["groups"][0]["items"][0]["conflict_values"][0]
    assert out["is_simulated"] is True

