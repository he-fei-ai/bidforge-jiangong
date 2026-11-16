# R248 · facts_extractor 遗留收口（2026-11-14）

## 一、任务与让号

承接 R247 §八 让渡登记：`facts_extractor.py` 有 R243/R245 未提交在飞改动，上轮让渡归属；本会话即归属会话，HEAD 停在 R238。用户指令「逐项探针定罪 → 加法式小步修复 → 每项跑内容簇对比基线 → 新护栏测试 → ruff/tsc/字节体检 → 报告」。

开工前删除 `backend/_probe_r243.py`（确认不存在）。落盘报告前复查轮次号：R243–R247 均被占用（`docs/解析提取模块深度探索增强_R243_20261114.md` 等），**R248 未被占用**，本轮用号 R248。

## 二、三项在飞遗留 · 逐项探针定罪

探针脚本 `backend/_probe_r248/probe.py`（报告后已删）对三项在飞遗留逐项行为实证：

### B7 · `_split_table_rows` 连续超长行空 chunk —— 不复现，维持

超长单行强制独立成段的在飞修复已覆盖连续超长行场景。探针输入连续 3 条超长行，输出 5 chunks、0 空 chunk、内容零丢失。**维持现状**（已在飞修复即最优方案，无需额外改动）。

### C12 · 时序配对 break 过早 —— 不复现，维持

`facts_cross_validators.py:306` 的 C3 自身时序 `break` 位于对称外层循环内：每对 entry×exit 双向各评一次，`break` 只跳过当前方向的内层剩余候选，外层另一方向仍完整评估。「双进场对单退场」判 1 次符合预期。**维持现状**（检测无盲区，加注释/改结构属无收益重构）。

### C11 · 正文-事实数值矛盾无单位换算 —— 定罪成立，修

`merge_and_deduplicate` 簇内去重键只走 `_norm_value`（去空白 + 全角冒号），`"150 天"` vs `"150 日历天"` 被判异值 → 凭空造出一条假矛盾让用户裁决。而本仓另两处判据早已把 天/日历天 列为确定等价：

* `facts_cross_validators._UNIT_SPEC`：两者同为 duration 基准 1.0；
* `content_standard._UNIT_ALIASES`：两者同映射 `"day"`；
* `_norm_dicts.UNIT_ALIASES`：`日→天`、`日历日→日历天`、`昼夜→天`；`content_data_contract.py:119`：`自然天` 与 `日历天/天` 并列；
* `content_standard.py:228`：`工作日→workday`（≠ day，不折叠）。

只有提取合并这一处漏了单位换算。**修**（见 §三）。

## 三、加法式小步修复

**生产源**：`backend/app/services/facts_extractor.py`（唯一改动文件）

1. `_norm_value` 后新增 `_NUM_UNIT_RE` + `_norm_unit_aware_key(v)`：对「纯数字 + 单位」形态按量纲折叠，判据单一事实源 = `facts_cross_validators._UNIT_DIM`/`_UNIT_TO_BASE`。同数值 + 同量纲 + 同基准系数 → 同一去重键；否则回落 `_norm_value` 原样。
2. 新增 `_DAY_VARIANT_ALIASES`（`日`/`日历日`/`自然天`/`昼夜` → `天`）别名链，查量纲表前先归一单位（首跑 A3 护栏发现 `日历日`/`昼夜` 不在量纲表时间段，据 `_norm_dicts.UNIT_ALIASES` 补齐）。
3. `merge_and_deduplicate` 簇内去重键 `nv = _norm_unit_aware_key(it.value)`。

**折叠边界（窄判据）**：温度仿射量纲不折叠（30℃ vs 30℉ 必异值）；跨基准系数不折叠（天=1.0 vs 月=30.0）；跨量纲不折叠（m vs cm）；`工作日` 不入别名表（与 `天` 保持独立）；纯数字裸值、非「数字+单位」形态、序列值、清单型并存键（`basis_scoped_key` 另有 `_norm_value` md5 口径）一律原样。

**展示不变**：折叠只作用于 merge 簇内比较键；对外展示的 `value`/`value_unit` 原样保留（best 取排序最优条，不把「150 日历天」改写成「150 天」）。

零迁移、零 DDL、零新增配置项、契约零破坏（加法式）。

## 四、新护栏

**新文件** `backend/tests/test_facts_unit_aware_merge_r248_20261114.py` · **14 passed / 0 failed**

* A 组正向 4：A1 值内嵌 天 vs 日历天（修复前实测 conflict=1）不再判矛盾；A2 unit 列形态 150(天) vs 150(日历天)；A3 天/日历天/日历日/昼夜 四等价写法全折叠；A4 展示 value 逐字保留（不把 日历天 改写成 天）。
* B 组反向 6：B1 同单位不同数值真矛盾照报；B2 工作日 vs 天 不折叠；B3 天 vs 月 跨基准系数不折叠；B4 12.5m vs 12.5cm 不折叠；B5 值内嵌单位 vs unit 列 跨形态保守不折叠；B6 纯数字/序列值/自由文本回落 `_norm_value` 原样。
* C 组判据锁 4：C1 折叠判据与量纲表同源；C2 温度仿射量纲不折叠；C3 清单型并存键路径不受影响；C4 A/B 反向（旧判据本应判异值 + 新判据已消除假矛盾）。

## 五、验证

| 项 | 结果 |
| --- | --- |
| R248 新护栏 | 14 passed / 0 failed |
| 事实簇 `-k "fact or global_fact"` | 1499 passed / 7 skipped / 1 failed（基线 1485p/7s/1f + 14 新护栏 = 精确闭合，零回归） |
| ruff（改动文件） | 全过 |
| 字节体检 | `facts_extractor.py` 纯 CRLF / BOM=0 / FFFD=0 / 孤立 CR=0；新测试纯 LF / BOM=0 / FFFD=0 / 孤立 CR=0 |
| tsc | 本轮零前端改动，无需复核 |

**簇内 1 failed** = `test_global_facts_route_contract_20261011.py::test_A3_route_inventory_size_is_as_documented`，引用 `docs/全局事实模块审计_R175_20261103.md` 不在仓内 = **既有环境型红**，与本轮触碰集无关（本轮只改 `facts_extractor.py`）。

**簇内 4 errors** = `.pytest_tmp` 写权限被并行会话占用（WinError 5 / disk I/O，R247 §八 已记载的同族环境型问题）。4 个 setup-error 测试用独立 basetemp 隔离复跑 **4 passed**，确认为环境型非本轮触碰集。

> ⚠️ 运维：需重启后端（`facts_extractor.py` 常驻）；前端零改动无需重建。

## 六、行为变化披露（默认配置下）

长库中「同数值同量纲但单位写法不同」（如 天/日历天/日历日/昼夜）的正文-事实对不再被凭空判为矛盾，事实簇内假矛盾候选减少；对外展示的取值逐字保留不变。真矛盾（不同数值）、跨量纲（m vs cm）、跨基准系数（天 vs 月）、工作日 vs 日历天 照报不变。

## 七、教训归档

1. **折叠判据要查「第三处口径」**——初版只对齐 `facts_cross_validators` 量纲表，A3 护栏即红（`日历日`/`昼夜` 不在该表时间段）。本仓对「天」的等价写法分散在三处（量纲表 / `_norm_dicts.UNIT_ALIASES` / `content_data_contract`），收口前须全量普查，护栏才会暴露漏项。
2. **`.pytest_tmp` 环境型红先做隔离复跑再定性**——簇内 4 errors 全是 basetemp mkdir WinError 5，换个 basetemp 即全绿；若直接当回归会误判（同 R247 教训「`--basetemp` 环境型 WinError 5」同族）。
3. **护栏要与实现同口径**（同 R237 教训③）——A 组断言「四等价写法全折叠」本身即判据普查的验收面，先红后修证明护栏有判别力。

## 八、登记

B7 / C12 维持（§二 注理由）；R247 §八 其余维持项（D-1 / C10 / D7 / D8 / 测试基建 `--basetemp`）不在本轮触碰集，维持原登记。
