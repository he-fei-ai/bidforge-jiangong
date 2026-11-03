/**
 * commitlint 配置 —— 提交信息遵循 Conventional Commits。
 *
 * 说明：本次仅落配置与约定文档（见 CONTRIBUTING.md），未安装 @commitlint/* 依赖、
 * 未接入 husky，避免在多会话并行开发期间改动 frontend/package.json 之外的依赖锁
 * 与共享 .git/hooks。后续启用方式：
 *   1) frontend 下安装依赖：npm i -D @commitlint/cli @commitlint/config-conventional
 *   2) 手动校验：npx commitlint --edit <msg文件>（cosmiconfig 会向上查找到本文件）
 *   3) 需要自动化时再评估接入 husky（须确认不影响并行会话的提交流程）
 */
module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    // 提交主题允许中文；Conventional Commits 对中文 subject 的默认大小写规则不适用
    "subject-case": [0],
    // 头部上限放宽到 120，兼容本仓较长的中文批次说明（如 Rxx 收口批次）
    "header-max-length": [2, "always", 120],
  },
};
