# 贡献约定

单人项目，约定从简。当前仅约定提交信息规范；commitlint 配置已就位（`commitlint.config.js`），依赖与钩子暂未安装（见文末）。

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
