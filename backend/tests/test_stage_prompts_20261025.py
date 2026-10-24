"""P3 阶段提示词构造器镜像测试（可直接放入 backend/tests/ 运行）。

覆盖：红线句逐字、各阶段必需文件名、初始提示三模式分支、original_only 分支、
输出修复 execution/submission 两态、关键 JSON 示例可解析、无技术评分分支与
评分分支的关键差异句。
"""
import json

from app.services.agent import stage_prompts
from app.services.agent.stage_prompts import (
    ORIGINAL_ONLY_DIRECTORY_RULE,
    create_children_prompt,
    create_initial_prompt,
    create_leaf_adjustment_prompt,
    create_leaf_allocation_prompt,
    create_no_technical_score_children_prompt,
    create_no_technical_score_review_prompt,
    create_outline_review_prompt,
    create_output_file_requirements,
    create_output_repair_prompt,
    create_score_planning_prompt,
)
from app.services.agent.validation import STAGE_OUTPUT_FILES


class _请求:
    """最小 request 替身（只用到 kind 属性，对应易标 request.kind）。"""

    def __init__(self, kind: str):
        self.kind = kind


class _元数据:
    """最小 meta 替身（只用到 workflow_stage 属性）。"""

    def __init__(self, workflow_stage: str):
        self.workflow_stage = workflow_stage


输入材料 = [
    {"path": "outline-review-context.json", "content": '{"status":"passed","leafCount":3}'},
    {"path": "outline.json", "content": '[{"id":"n1"}]'},
]


# ── 红线句与阶段文件名 ────────────────────────────────────────────────────

def test_红线句_只在回复中输出JSON不算写入_逐字出现():
    for stage in STAGE_OUTPUT_FILES:
        文本 = create_output_file_requirements(stage)
        assert "只在回复中输出 JSON 不算写入" in 文本
        assert "空文件不算完成" in 文本
        assert "全部文件写入并通过校验后才能结束本阶段" in 文本


def test_红线句_预置Schema与json_validation自查_逐字出现():
    文本 = create_initial_prompt("执行任务。")
    assert f"程序已为 outline.json 预置 Schema，可用 json-validation 自查，只传 file_path" in 文本
    分配 = create_leaf_allocation_prompt()
    assert "程序已为 leaf-allocation.json 预置 Schema，可用 json-validation 自查，只传 file_path" in 分配


def test_各构造器_产出含对应阶段的必需文件名():
    断言表 = [
        (create_initial_prompt("执行任务。"), "initial-outline"),
        (create_no_technical_score_children_prompt(
            target_leaf_count=10, standalone_technical=False), "children_generation"),
        (create_leaf_allocation_prompt(), "leaf_allocation"),
        (create_score_planning_prompt(), "score-planning"),
        (create_children_prompt(has_original_plan=False, original_only=False,
                                target_leaf_count=10, allow_root_changes=False,
                                standalone_technical=False), "children_generation"),
        (create_leaf_adjustment_prompt(10, 12), "leaf_adjustment"),
        (create_no_technical_score_review_prompt(
            target_leaf_count=10, actual_leaf_count=12, input_files=输入材料), "outline_review"),
        (create_outline_review_prompt(
            target_leaf_count=10, actual_leaf_count=12, allow_root_changes=False,
            input_files=输入材料), "outline_review"),
    ]
    for 文本, 阶段 in 断言表:
        期望清单 = "、".join(STAGE_OUTPUT_FILES[阶段])
        assert f"本阶段必需文件：{期望清单}。" in 文本


def test_导出清单_与任务约定一致():
    assert set(stage_prompts.__all__) == {
        "ORIGINAL_ONLY_DIRECTORY_RULE",
        "create_output_file_requirements",
        "create_output_repair_prompt",
        "create_initial_prompt",
        "create_no_technical_score_children_prompt",
        "create_leaf_allocation_prompt",
        "create_score_planning_prompt",
        "create_children_prompt",
        "create_leaf_adjustment_prompt",
        "create_no_technical_score_review_prompt",
        "create_outline_review_prompt",
    }


# ── create_initial_prompt 三模式 ─────────────────────────────────────────

def test_初始提示_默认模式走响应文件分支():
    文本 = create_initial_prompt("按招标要求编写。")
    assert "我们的目标是为编写响应文件/投标文件准备一级目录。" in 文本
    assert "6. 每个一级目录当前都是叶子节点" in 文本
    assert "attr=商务/资信或业绩时必须使用 template-fill" in 文本
    assert "独立分册" not in 文本
    assert "评分大项一" not in 文本


def test_初始提示_独立装订技术文件分支():
    文本 = create_initial_prompt("按招标要求编写。", standalone_technical=True)
    assert "我们的目标是为单独装订的技术文件准备一级目录。一级目录必须直接对应技术评分大项。" in 文本
    assert "6. 本模式只生成技术文件独立分册：只能保留适合展开技术正文的评分大项" in 文本
    assert "每个一级目录直接对应一个技术评分大项" in 文本
    assert "评分大项一" in 文本
    assert "编写响应文件/投标文件" not in 文本


def test_初始提示_独立装订且无技术评分分支():
    文本 = create_initial_prompt(
        "按招标要求编写。", standalone_technical=True, no_technical_score_mode=True)
    assert "我们的目标是为没有技术评分项的单独装订技术文件准备一级目录。" in 文本
    assert "招标文件已确认没有可用的技术评分项" in 文本
    assert "根据项目类型和专业经验补充通用、合理的技术方案主题" in 文本
    assert "不得创建“技术方案”“项目管理方案”“监理大纲”“监理大纲（暗标）”" in 文本
    assert "评分大项一" not in 文本
    assert "完整结构示例" not in 文本


def test_初始提示_仅无评分标记不影响非独立分册分支():
    # 易标语义：noTechnicalScoreMode 只在 standaloneTechnical 为真时生效
    assert (create_initial_prompt("执行。", no_technical_score_mode=True)
            == create_initial_prompt("执行。"))


# ── original_only 分支 ───────────────────────────────────────────────────

def test_目录生成_原方案only分支_含目录来源仅限原方案规则():
    文本 = create_no_technical_score_children_prompt(
        target_leaf_count=None, standalone_technical=False, original_only=True)
    assert ORIGINAL_ONLY_DIRECTORY_RULE in 文本
    assert "请阅读 outline.json、原方案.md，然后覆盖写回 outline.json。" in 文本
    assert "响应文件要求.md" not in 文本


def test_目录生成_非原方案分支_不含红线规则且含资料优先文案():
    文本 = create_no_technical_score_children_prompt(
        target_leaf_count=None, standalone_technical=False, original_only=False)
    assert ORIGINAL_ONLY_DIRECTORY_RULE not in 文本
    assert "响应文件要求.md、项目概述.md，以及存在的原方案.md 和参考知识库目录" in 文本
    assert "优先采用资料中的明确要求" in 文本


def test_叶子调整_红线规则仅在无评分且原方案模式出现():
    同时满足 = create_leaf_adjustment_prompt(
        10, 8, no_technical_score_mode=True, original_only=True)
    仅无评分 = create_leaf_adjustment_prompt(10, 8, no_technical_score_mode=True)
    仅原方案 = create_leaf_adjustment_prompt(10, 8, original_only=True)
    默认 = create_leaf_adjustment_prompt(10, 8)
    assert ORIGINAL_ONLY_DIRECTORY_RULE in 同时满足
    assert ORIGINAL_ONLY_DIRECTORY_RULE not in 仅无评分
    assert ORIGINAL_ONLY_DIRECTORY_RULE not in 仅原方案
    assert ORIGINAL_ONLY_DIRECTORY_RULE not in 默认


def test_无评分审核_原方案only分支_含规则与不得新增目录句():
    文本 = create_no_technical_score_review_prompt(
        target_leaf_count=10, actual_leaf_count=10, original_only=True,
        input_files=输入材料)
    assert ORIGINAL_ONLY_DIRECTORY_RULE in 文本
    assert "不得以缺少通用技术主题为由新增目录" in 文本
    assert "- 来源与完整性：根据下附原方案.md" in 文本
    assert "专业合理性：检查目录是否覆盖项目实施所需的通用技术主题" not in 文本


# ── create_output_repair_prompt 两态 ─────────────────────────────────────

def test_输出修复_execution类型返回None():
    assert create_output_repair_prompt(_请求("execution"), _元数据("outline_review")) is None


def test_输出修复_submission类型返回必需文件要求与修复文案():
    文本 = create_output_repair_prompt(_请求("submission"), _元数据("score-planning"))
    assert 文本.startswith(create_output_file_requirements("score-planning"))
    assert "technical-score-groups.json、score-directory-plan.json" in 文本
    assert "沿用已有材料和用户确认结果，只修复本次提交报告中的问题" in 文本
    assert "已通过的文件保留，不要重复询问已确认事项" in 文本


# ── 关键 JSON 示例可被 json.loads 解析 ────────────────────────────────────

def test_初始提示_完整结构示例是合法JSON():
    for 文本 in (
        create_initial_prompt("执行。"),
        create_initial_prompt("执行。", standalone_technical=True),
    ):
        示例 = 文本.split("完整结构示例：", 1)[1].split("。", 1)[0]
        解析 = json.loads(示例)
        assert 解析["outline"][0]["id"] is None


def test_叶子分配_输出格式示例是合法JSON():
    文本 = create_leaf_allocation_prompt()
    示例 = 文本.split("7. 输出格式为 ", 1)[1].split("。", 1)[0]
    解析 = json.loads(示例)
    assert 解析["mode"] == "allocated"
    assert 解析["allocatable_ai_leaf_count"] == 19


def test_评分规划_groups示例与字段层级示例是合法JSON():
    文本 = create_score_planning_prompt()
    groups = 文本.split("完整结构为 ", 1)[1].split("。", 1)[0]
    assert json.loads(groups)["groups"][0]["requirement_id"] == "R1"
    层级 = 文本.split("完整字段层级示例：", 1)[1].split("。branches 中", 1)[0]
    解析 = json.loads(层级)
    assert 解析["allow_root_changes"] is False
    assert 解析["branches"][0]["score_item_level"] == 2
    独立 = create_score_planning_prompt(standalone_technical=True)
    层级独立 = 独立.split("完整字段层级示例：", 1)[1].split("。branches 中", 1)[0]
    assert json.loads(层级独立)["branches"][0]["score_item_level"] == 1


def test_完整目录_结构示例是合法JSON():
    for 文本 in (
        create_children_prompt(has_original_plan=False, original_only=False,
                               target_leaf_count=10, allow_root_changes=False,
                               standalone_technical=False),
        create_children_prompt(has_original_plan=True, original_only=False,
                               target_leaf_count=10, allow_root_changes=False,
                               standalone_technical=True),
    ):
        示例 = 文本.split("的完整结构示例：", 1)[1].split("。", 1)[0]
        解析 = json.loads(示例)
        assert 解析["outline"][0]["id"] is None


def test_审核_两种状态完整格式是合法JSON():
    文本 = create_outline_review_prompt(
        target_leaf_count=10, actual_leaf_count=10, allow_root_changes=False,
        input_files=输入材料)
    通过 = 文本.split("无问题时完整格式为 ", 1)[1].split("；", 1)[0]
    assert json.loads(通过)["status"] == "passed"
    待改 = 文本.split("有问题时完整格式为 ", 1)[1].split("。category", 1)[0]
    解析 = json.loads(待改)
    assert 解析["issues"][0]["confirmation_required"] is True


# ── 无技术评分分支 / 评分分支关键差异句 ───────────────────────────────────

def test_无评分目录生成与评分规划的判定句互斥():
    无评分 = create_no_technical_score_children_prompt(
        target_leaf_count=10, standalone_technical=False)
    评分 = create_score_planning_prompt()
    assert "用户已确认招标文件没有技术评分项" in 无评分
    assert "不要判断是否存在评分项，也不要创建评分清单或评分映射" in 无评分
    assert "程序已确认本任务存在技术评分项" in 评分
    assert "程序已确认本任务存在技术评分项" not in 无评分


def test_两种最终审核提示的开篇与评分覆盖维度差异():
    无评分 = create_no_technical_score_review_prompt(
        target_leaf_count=10, actual_leaf_count=10, input_files=输入材料)
    评分 = create_outline_review_prompt(
        target_leaf_count=10, actual_leaf_count=10, allow_root_changes=False,
        input_files=输入材料)
    assert "请对当前无技术评分项模式生成的完整技术方案目录执行最终审核" in 无评分
    assert "不要判断、补造或检查评分项" in 无评分
    assert "评分覆盖：直接以技术评分信息.md 为原始依据" not in 无评分
    assert "请对当前完整技术方案目录执行最终审核" in 评分
    assert "- 评分覆盖：直接以技术评分信息.md 为原始依据" in 评分
    assert "无技术评分项模式" not in 评分


def test_叶子数量行_有目标时输出范围_无目标时整行省略():
    有目标 = create_outline_review_prompt(
        target_leaf_count=20, actual_leaf_count=22, allow_root_changes=False,
        input_files=输入材料)
    assert "程序计算目标为 20 个，当前为 22 个，可接受范围为 18 至 22 个" in 有目标
    下限 = create_no_technical_score_review_prompt(
        target_leaf_count=1, actual_leaf_count=1, input_files=输入材料)
    assert "可接受范围为 1 至 3 个" in 下限
    无目标 = create_outline_review_prompt(
        target_leaf_count=None, actual_leaf_count=9, allow_root_changes=True,
        input_files=输入材料)
    assert "“AI生成”叶子数量" not in 无目标


def test_叶子分配_独立分册与默认的分配指令差异():
    默认 = create_leaf_allocation_prompt()
    独立 = create_leaf_allocation_prompt(standalone_technical=True)
    assert "每个目录至少分配 2 个。" in 默认
    assert "优先为每个目录分配至少 2 个" not in 默认
    assert "优先为每个目录分配至少 2 个" in 独立
    assert "禁止形成只有一个子节点的冗余层级" in 独立


def test_目录生成_叶子目标为空与独立分册叶句子差异():
    无目标无评分 = create_no_technical_score_children_prompt(
        target_leaf_count=None, standalone_technical=False)
    有目标无评分 = create_no_technical_score_children_prompt(
        target_leaf_count=30, standalone_technical=False)
    无目标常规 = create_children_prompt(
        has_original_plan=False, original_only=False, target_leaf_count=None,
        allow_root_changes=False, standalone_technical=False)
    assert "请根据项目复杂度自主确定合理的“AI生成”叶子节点数量。" in 无目标无评分
    assert "最终完整目录合计应有约 30 个 content_mode=ai-generate 的叶子节点。" in 有目标无评分
    assert "请根据材料复杂度自主确定合理的“AI生成”叶子节点数量。" in 无目标常规
    assert "目录质量优先于机械凑数" in 无目标无评分


def test_目录生成_一级目录锁定与批准调整分支差异():
    锁定 = create_children_prompt(
        has_original_plan=False, original_only=False, target_leaf_count=10,
        allow_root_changes=False, standalone_technical=False)
    可调整 = create_children_prompt(
        has_original_plan=False, original_only=False, target_leaf_count=10,
        allow_root_changes=True, standalone_technical=False)
    assert "一级目录的数量、顺序、id、title、description、attr 均已由用户确认" in 锁定
    assert "一级目录的数量、顺序、id、title、description、attr 均已由用户确认" not in 可调整
    assert "用户已批准 score-directory-plan.json 中记录的一级目录调整" in 可调整


def test_目录生成_独立分册叶子保留句与非独立分册差异():
    独立 = create_children_prompt(
        has_original_plan=False, original_only=False, target_leaf_count=10,
        allow_root_changes=False, standalone_technical=True)
    默认 = create_children_prompt(
        has_original_plan=False, original_only=False, target_leaf_count=10,
        allow_root_changes=False, standalone_technical=False)
    assert "leaf_count=1 表示保留对应一级目录本身作为叶子" in 独立
    assert "leaf_count=1 表示保留对应一级目录本身作为叶子" not in 默认
    assert "每个 branch 的 score_item_level=1" in 独立
    assert "每个 branch 的 mappings 必须在该分支的 score_item_level 层级生成对应节点" in 默认


def test_叶子调整_无评分与评分模式的调整边界差异():
    无评分 = create_leaf_adjustment_prompt(10, 8, no_technical_score_mode=True)
    评分 = create_leaf_adjustment_prompt(10, 14)
    assert "用户已确认采用无技术评分项模式" in 无评分
    assert "必须继续遵循 score-directory-plan.json" not in 无评分
    assert "必须继续遵循 score-directory-plan.json：不得删除、移动或改变评分项对应节点" in 评分
    assert "技术一级目录的 id 必须与 score-directory-plan.json 中对应的 root_id 一致" in 评分
    assert "技术一级目录的 id 必须与 score-directory-plan.json 中对应的 root_id 一致" not in 无评分


def test_审核输入材料_按路径与内容包裹并以双换行连接():
    文本 = create_outline_review_prompt(
        target_leaf_count=10, actual_leaf_count=10, allow_root_changes=False,
        input_files=输入材料)
    第一块 = "【文件开始：outline-review-context.json】\n" \
             '{"status":"passed","leafCount":3}\n' \
             "【文件结束：outline-review-context.json】"
    第二块 = "【文件开始：outline.json】\n[{\"id\":\"n1\"}]\n【文件结束：outline.json】"
    assert 第一块 in 文本
    assert 第二块 in 文本
    assert 第一块 + "\n\n" + 第二块 in 文本
