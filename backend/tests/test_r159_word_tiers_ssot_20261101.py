"""R159（2026-11-01）硬编码治理 · 四档字数档位 SSoT 双向锁。

背景：《软件硬编码场景深度盘点》（桌面文档）§1/§5 指控「档位数值写死、
散落多处、全局修改易遗漏」。逐项取证（R155-O 教训①：先核对现状再动手）：

  - 后端真源 ``content_utils.WORD_BUDGET_TIERS`` 单一（R66 建立）；
  - 但前端有 **3 处字面量副本且无 parity 锁**：ContentGenerationTab 的
    WORD_BUDGET_OPTIONS（label 内嵌数值）、SchemeWorkbenchPage 的
    WORD_BUDGET_TIER_VALUES 档位值表 + key 数组、workflowDerived 的缺省 1200；
  - review_autofix DLV-16 用户文案内嵌「简短800/标准1200/详细2000/超长2500」
    字面量（content_utils 注释明言「禁止再出现第二套字面量」，实况违反）。

本轮收敛：前端唯一镜像 ``utils/wordBudgetTiers.ts``（数值/labels/options/
输入区间）+ 后端唯一拼装 ``format_word_budget_tier_summary()``。
本文件锁（源码锚一律断调用/赋值形态，防注释自伤——R116 教训③）：
  A. 后端真源行为（拼装输出 + 不变量 + R66 档位解析回归）
  B. 用户可见文案单一拼装（review_autofix 接线锚 + 字面量绝迹）
  C. 前端镜像 parity（数值同源）+ 消费接线锚 + 双侧反副本扫描
"""
import re
from pathlib import Path

from app.services import review_autofix
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    WORD_BUDGET_DEFAULT_TIER,
    WORD_BUDGET_TIER_LABELS_ZH,
    WORD_BUDGET_TIERS,
    format_word_budget_tier_summary,
    resolve_word_budget_tier,
)

_BACKEND = Path(__file__).resolve().parent.parent
_FRONTEND = _BACKEND.parent / "frontend" / "src"
_FRONT_MIRROR = _FRONTEND / "utils" / "wordBudgetTiers.ts"


# ---------------------------------------------------------------------------
# A · 后端真源行为
# ---------------------------------------------------------------------------

def test_a1_tier_summary_is_sole_assembly():
    """档位摘要唯一拼装输出（字节级锁定；档位变更时随真源更新本断言）。"""
    assert format_word_budget_tier_summary() == "简短800/标准1200/详细2000/超长2500"


def test_a2_backend_source_invariants():
    """真源内部不变量：默认档取值 = DEFAULT_WORD_BUDGET；labels 与档位同键同序。"""
    assert WORD_BUDGET_TIERS == {"brief": 800, "standard": 1200,
                                 "detailed": 2000, "ultra": 2500}
    assert WORD_BUDGET_TIERS[WORD_BUDGET_DEFAULT_TIER] == DEFAULT_WORD_BUDGET
    assert list(WORD_BUDGET_TIER_LABELS_ZH) == list(WORD_BUDGET_TIERS)
    assert set(WORD_BUDGET_TIER_LABELS_ZH.values()) == {"简短", "标准", "详细", "超长"}


def test_a3_r66_tier_resolution_regression():
    """R66 档位解析回归：档位名/数值两路仍解析，非法值仍 None。"""
    assert resolve_word_budget_tier("brief") == 800
    assert resolve_word_budget_tier("  ULTRA ") == 2500
    assert resolve_word_budget_tier(1200) == 1200
    assert resolve_word_budget_tier("bogus") is None
    assert resolve_word_budget_tier(None) is None
    assert resolve_word_budget_tier(999) is None


def test_a4_backend_literal_map_is_sole():
    """反副本扫描（后端 app/）：``"brief": 800`` 字面量只允许出现在真源一处。"""
    hits = []
    for py in (_BACKEND / "app").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if re.search(r'"brief"\s*:\s*800', py.read_text(encoding="utf-8")):
            hits.append(py.name)
    assert hits == ["content_utils.py"], f"档位字面量副本回流: {hits}"


# ---------------------------------------------------------------------------
# B · 用户可见文案单一拼装
# ---------------------------------------------------------------------------

def test_b1_review_autofix_wired_to_formatter():
    """DLV-16 文案改由唯一拼装供给（import + 调用形态双锚，防注释自伤）。"""
    src = review_autofix.__file__
    text = Path(src).read_text(encoding="utf-8")
    assert "format_word_budget_tier_summary" in text
    assert re.search(r"\{format_word_budget_tier_summary\(\)\}", text)


def test_b2_review_autofix_literal_gone():
    """反向锚：review_autofix 内不再允许「简短800」字面量。"""
    text = Path(review_autofix.__file__).read_text(encoding="utf-8")
    assert "简短800" not in text


def test_b3_dlv16_message_uses_summary():
    """行为级：DLV-16 用户可见文案内嵌档位摘要（与真源同源）。"""
    cap = review_autofix._CAPABILITY["DLV-16"]
    assert format_word_budget_tier_summary() in cap.reason
    assert "对偏差章节重新生成" in cap.reason


def test_b4_preflight_dlv16_message_uses_summary():
    """行为级：预检 DLV-16 建议同样内嵌档位摘要（R159 前是第二处字面量副本，
    由 _r159_ssot_inventory.py 绝迹扫描揪出）。"""
    from app.services import preflight_engine

    src = Path(preflight_engine.__file__).read_text(encoding="utf-8")
    assert "format_word_budget_tier_summary()" in src
    assert "简短800" not in src


def test_b5_tier_literal_extinct_in_backend():
    """反向扫描：后端 app/ 内「简短800」字面量绝迹（文案一律走唯一拼装）。"""
    hits = []
    for py in (_BACKEND / "app").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if "简短800" in py.read_text(encoding="utf-8"):
            hits.append(py.name)
    assert hits == [], f"档位字面量文案回流: {hits}"


# ---------------------------------------------------------------------------
# C · 前端镜像 parity + 消费接线（fs 源码锚，与 r151 J 组同模式）
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


def test_c1_mirror_values_match_backend():
    """前端唯一镜像的数值与后端真源 parity（双向锁的前端侧）。"""
    ts = _read(_FRONT_MIRROR)
    fe = {}
    for key in WORD_BUDGET_TIERS:
        m = re.search(rf"\b{key}:\s*(\d+)", ts)
        assert m, f"镜像缺档位 {key}"
        fe[key] = int(m.group(1))
    assert fe == WORD_BUDGET_TIERS


def test_c2_mirror_default_and_labels_derived():
    """默认值从档位表派生（禁止独立 1200 字面量）；labels 与档位同键。"""
    ts = _read(_FRONT_MIRROR)
    assert "WORD_BUDGET_TIERS[WORD_BUDGET_DEFAULT_TIER]" in ts
    for key, label in WORD_BUDGET_TIER_LABELS_ZH.items():
        assert re.search(rf'{key}:\s*"{label}"', ts), f"镜像中文名漂移: {key}"
    # options 的 label 必须由数值拼装（模板锚），禁止手写「简短 800 字」
    assert "${WORD_BUDGET_TIERS[k]} 字" in ts
    assert "简短 800 字" not in ts


def test_c3_content_tab_consumes_mirror():
    """ContentGenerationTab：选项/自定义档区间取自镜像，字面量绝迹。"""
    ts = _read(_FRONTEND / "components" / "ContentGenerationTab.tsx")
    assert re.search(r'import\s*\{[^}]*WORD_BUDGET_OPTIONS[^}]*\}\s*from\s*"\.\./utils/wordBudgetTiers"', ts)
    assert "CUSTOM_TOTAL_BUDGET_MAX" in ts
    # 反副本：label 字面量与内联输入上限绝迹
    assert "简短 800 字" not in ts
    assert "max={20000}" not in ts
    assert "?? 2000" not in ts


def test_c4_page_consumes_mirror():
    """SchemeWorkbenchPage：档位值表/key 数组/预算区间取自镜像，字面量绝迹。"""
    page = _read(_FRONTEND / "pages" / "SchemeWorkbenchPage.tsx")
    assert re.search(
        r'import\s*\{[^}]*WORD_BUDGET_TIER_VALUE_OF[^}]*\}\s*from\s*"\.\./utils/wordBudgetTiers"', page)
    assert "WORD_BUDGET_TIER_VALUE_OF" in page
    assert re.search(r'export\s*\{\s*WORD_BUDGET_MIN,\s*WORD_BUDGET_MAX\s*\}', page)
    # 反副本：档位字面量值表 / key 数组绝迹
    assert not re.search(r"brief:\s*800", page)
    assert '["brief", "standard", "detailed", "ultra"]' not in page


def test_c5_workflowderived_default_from_mirror():
    """workflowDerived 缺省预算取自镜像（`|| DEFAULT_WORD_BUDGET` 形态锚）。"""
    ts = _read(_FRONTEND / "utils" / "workflowDerived.ts")
    assert 'import { DEFAULT_WORD_BUDGET } from "./wordBudgetTiers"' in ts
    assert "Number(s.word_budget) || DEFAULT_WORD_BUDGET" in ts
    assert "Number(s.word_budget) || 1200" not in ts


def test_c6_frontend_literal_copies_extinct():
    """反副本扫描（frontend/src，测试目录与镜像本身除外）。"""
    hits = []
    for f in _FRONTEND.rglob("*"):
        if f.suffix not in {".ts", ".tsx"} or not f.is_file():
            continue
        rel = f.relative_to(_FRONTEND).as_posix()
        if rel.startswith("tests/") or f.name == "wordBudgetTiers.ts":
            continue
        text = f.read_text(encoding="utf-8")
        if re.search(r"brief:\s*800", text) or "简短 800 字" in text:
            hits.append(rel)
    assert hits == [], f"前端档位字面量副本回流: {hits}"


# ---------------------------------------------------------------------------
# D · 方案级总字数缺省 30000 五处定义面同值（R66 同款「同值护栏锁死」；
#     models 不 import services 防循环导入 → 字面量保留 + 源码锚锁同值）
# ---------------------------------------------------------------------------

def test_d1_scheme_word_budget_default_single_value():
    """方案级总字数缺省 30000：models/ddl/sse/agent 全部同值，防漂移。"""
    models_src = (_BACKEND / "app" / "models.py").read_text(encoding="utf-8")
    assert "word_budget: int = 30000" in models_src, "SchemeCreate 缺省漂移"
    ddl_src = (_BACKEND / "app" / "schema_sql.py").read_text(encoding="utf-8")
    assert "word_budget INTEGER DEFAULT 30000" in ddl_src, "DDL 缺省漂移"
    sh_src = (_BACKEND / "app" / "routers" / "sse_handlers.py").read_text(
        encoding="utf-8")
    assert 'int(scheme.get("word_budget") or 30000)' in sh_src, \
        "分步判定读取面缺省漂移"
    assert "word_budget = 30000" in sh_src, "脏值回退面缺省漂移"
    agent_src = (_BACKEND / "app" / "services" / "agent" /
                 "outline_agent_flow.py").read_text(encoding="utf-8")
    assert agent_src.count("word_budget: int = 30000") == 3, \
        "Agent 链三个入口形参缺省必须同为 30000"
