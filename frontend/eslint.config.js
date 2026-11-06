import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  {
    ignores: ["dist", "logs", "node_modules", "**/*.bak*", "junk_*.log", "tsconfig.tsbuildinfo"],
  },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/immutability": "off",
      "react-hooks/refs": "off",
      "react-hooks/purity": "off",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // ✅ any 清理专项（2026-11-02）：no-explicit-any 升级为 error 作为 CI 门禁。
      // src（非测试）存量显式 any 已全部清零；测试目录见下方 per-directory override。
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
          ignoreRestSiblings: true,
        },
      ],
    },
  },
  {
    // ✅ any 清理专项（2026-11-02）遗留豁免（带原因）：
    // src/tests/** 约 760 处显式 any 集中在测试桩/夹具（(window as any).matchMedia
    // 补丁、mockImplementation(async (body: any) => ...)、部分对象夹具等），
    // 本轮（前端 any 清理专项）未清零。主规则已升级为 error，src 非测试代码
    // 全量生效；测试目录暂时维持 warn，后续按文件逐个清零后删除本 override。
    // 受影响文件清单见 any 清理专项报告「遗留风险」章节。
    files: ["src/tests/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
);
