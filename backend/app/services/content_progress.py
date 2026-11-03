"""正文生成 · 进度模型常量（从 sse_handlers.py 抽离，2026-09-21）

供正文生成 / 目录生成共用的**常量单一事实源**（``STAGE_MODEL`` 等五个常量由
``sse_handlers`` import 别名消费，见其 2026-10-12 技术债清单 P0-1 收口注释）。

✅ R168（2026-11-03 · 死代码收口）：本文件此前的**函数族**（section_partial /
calibrate_stage_expect / monotonic_progress / weighted_progress / fmt_duration /
snapshot_stats / make_content_progress_state，另含 STAGE_EXPECT_* 三常量）是
2026-09-21 抽离时的旧实现 —— 进度函数的活路径早已改走
``routers/_sse_outline_build.py`` 的私有副本（含 B19 阶段起点计时修复，休眠副本
从未同步），生产与测试**双零消费**（``tests/test_content_progress.py`` 等锚定的
是 ``sse_handlers`` 转发的活实现）。为杜绝「休眠副本与活副本口径分叉、未来恢复
共用时静默回退行为」（R95/R98 两轮登记未处置），本轮删除整个休眠函数族；
活实现的行为回归锁在 ``tests/test_content_progress.py`` / R98 护栏维持不变。

⚠️ 函数族的活体在 ``routers/_sse_outline_build.py``（services 不得 import routers，
故不回填委派）。要改进度行为请改那里；要改全局常量改本文件。
"""
from __future__ import annotations

# ---------- 正文生成进度模型（2026-09-15 增强） ----------
# 现改为三段式：
#    1) 章节内阶段事件 section_stage（context → draft → continue → persist）；
#    2) 按阶段权重折算的平滑总进度（进行中章节也贡献部分进度）；
#    3) 心跳通道周期性推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。
SECTION_PHASE_MAX = 0.95
# 章节内阶段进度模型：base 进入该阶段时已完成的累积比例（保证单调递增），
# span 该阶段自身占本章的比例（合计 1.0），expect 该阶段预期耗时（秒）。
STAGE_MODEL = {
    "context":  {"base": 0.00, "span": 0.05, "expect": 3.0},
    "draft":    {"base": 0.05, "span": 0.55, "expect": 90.0},
    "continue": {"base": 0.60, "span": 0.30, "expect": 60.0},
    "persist":  {"base": 0.90, "span": 0.10, "expect": 12.0},
}
# 阶段内渐近填充上限：只填到 span 的 85%，给「阶段完成」留出可见的跳变
STAGE_FILL_MAX = 0.85
STAGE_LABELS = {
    "context": "构建上下文",
    "draft": "AI 生成中",
    "continue": "续写扩充中",
    "persist": "清洗与落库",
}
# AI 长调用期间推送运行统计的间隔（秒）
STATS_PUSH_INTERVAL = 3.0
