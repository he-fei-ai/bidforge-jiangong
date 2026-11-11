# 贡献约定

单人项目，约定从简。当前仅约定提交信息规范；commitlint 配置已就位（`commitlint.config.js`），依赖与钩子暂未安装（见文末）。

> **代码规范总纲**：分支模型（main/develop/feature/test/release/hotfix）、构建分级闸门与协作守则
> 见 `docs/代码规范原则_Git协作与构建约束_20261109.md`（2026-11-09 生效），与本文件冲突时以该文件为准。

## 提交信息规范

采用 Conventional Commits 格式：

```
<type>(<scope>): <subject>
```

- **type**（必填）：`feat` 新功能 / `fix` 缺陷修复 / `refactor` 重构 / `docs` 文档 / `test` 测试 / `chore` 构建·工具·杂项 / `style` 格式 / `perf` 性能
- **scope**（可选）：模块名，如 `export`、`outline`、`chart`、`frontend`、`lint`
- **subject**（必填）：中文简述，结尾不加句号；一行写不下时在正文补充细节

示例（取自本仓历史）：

```
feat(生成链路): 引入 G1/G2/G3 三机制编排选事实强注入
chore(lint): 整仓 I001 import 排序（独立提交，与功能改动分离）
fix(export): 表格侧叠行拆分与尾部空列裁剪
```

## 两条硬性约定（延续既有红线）

1. **整仓机械格式化（import 排序、行尾归一、全量 prettier/ruff format 重排等）必须独立提交**，不与功能改动混在同一提交。
2. 提交前确认不包含他人会话的未提交改动；不要使用 `git add -A`。

## commitlint 启用状态

- 配置：仓库根 `commitlint.config.js`（已就位，手动校验可用 `npx commitlint --edit`）
- 依赖：`@commitlint/cli`、`@commitlint/config-conventional` 暂未安装
- 钩子：未接入 husky。多会话并行开发期间，共享 `.git/hooks` 中的 pre-commit 钩子会拦截所有会话的提交，待并行窗口结束、需要自动化时再评估接入。

## Python 解释器：pin 来源与 CI 的双腿策略

2026-11-04 起的事实：

- `backend/requirements*.txt` 的 pin 与 **Python 3.14.4 验证环境**对齐（逐包 `importlib.metadata.version` 实测），部署端 `pip install -r requirements.txt` 装到的就是这一套版本。
- CI 的**阻断腿**（`backend` job）仍跑 **Python 3.12**；另有非阻断的**观察腿**（`backend-py314-observe`）跑 **Python 3.14**，只跑 12 个文件的冒烟子集（六条链路 import + 核心纯函数），不跑全量、不拦合并。
- 观察腿转正标准（与 `smoke-windows` 相同）：连续 10 次 run 全绿，且期间未暴露 3.14 特有的依赖或行为问题 ⇒ 此时把阻断腿的 `python-version` 切到 3.14，并删除观察腿。
- 切腿之前不要只改一边：只改 pin 或只改 CI 解释器，都会让「声明版本 / 实装版本 / CI 版本」再次分叉。

## ruff 升级的前置条件

当前 ruff pin 为 `0.6.9`。换成实测的 `0.16.2` 会新增 5 条 I001（`app/routers/global_facts.py`、`app/routers/sse_handlers.py`、`app/services/bid_section_extraction.py`、`app/services/consistency_scanner.py`、`app/services/preflight_engine.py`），且 0.16.2 的 `ruff format --check` 会因 Annotation range bug panic。升 ruff 的前置条件：先单独提交修完这 5 处 import 排序，并换到不 panic 的版本，再改 pin。
