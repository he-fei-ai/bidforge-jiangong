/**
 * Blueprint 主题 token + 暗色模式支持
 * ----------------------------------------------------------
 * 统一 token 来源；main.tsx 和各 ConfigProvider 都从此导入，
 * 避免「侧边栏一套、主区一套」的漂移问题。
 */
import { createContext, useContext } from "react";
import type { theme as AntTheme } from "antd";

export type ThemeMode = "light" | "dark";

export const LS_THEME_KEY = "bp-theme-mode";

/** 工程青（Blueprint Cyan）全局亮色 token */
export const BLUEPRINT_TOKENS = {
  colorPrimary: "#00D4FF",
  colorInfo: "#00D4FF",
  colorLink: "#00D4FF",
  colorSuccess: "#00C853",
  colorWarning: "#FFB800",
  colorError: "#FF3D5A",

  borderRadius: 4,
  borderRadiusLG: 6,
  borderRadiusSM: 3,

  fontFamily:
    "'Geologica', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif",
  fontFamilyCode:
    "'JetBrains Mono', 'Cascadia Code', Consolas, 'Courier New', monospace",

  // 主区域（浅色）覆盖
  colorBgLayout: "#F4F6FA",
  colorBgContainer: "#FFFFFF",
  colorBgElevated: "#FFFFFF",
  colorBorder: "#D8DFE8",
  colorBorderSecondary: "#E8EDF4",
  colorText: "#1A2332",
  colorTextSecondary: "#5A6A80",
  colorTextTertiary: "#8B99AC",

  // 间距系统（略紧凑，适配高密度工程界面）
  padding: 12,
  paddingLG: 16,
  paddingSM: 8,

  // 阴影：精细、克制
  boxShadow: "0 1px 2px rgba(15,43,79,0.06), 0 2px 8px rgba(15,43,79,0.04)",
  boxShadowSecondary: "0 2px 6px rgba(15,43,79,0.08)",

  // 控件
  controlHeight: 32,
  controlHeightLG: 40,
  controlHeightSM: 24,
};

/** 深色主区覆盖（在 defaultAlgorithm 基础上覆盖亮色字段） */
export const BLUEPRINT_TOKENS_DARK_OVERRIDE = {
  colorBgLayout: "#0A1626",
  colorBgContainer: "#12243D",
  colorBgElevated: "#1A3352",
  colorBgSpotlight: "#1A3352",
  colorBorder: "#284466",
  colorBorderSecondary: "#1F3854",
  colorText: "#D8E6F4",
  colorTextSecondary: "#9AB4D0",
  colorTextTertiary: "#6F87A5",
};

/** 侧边栏（始终深色）—— 两种模式下的差异极小，只暴露 token 便于复用 */
export const SIDEBAR_TOKENS = {
  colorBgContainer: "#0F2B4F",
  colorBgLayout: "#0F2B4F",
  colorBgElevated: "#16395F",
  colorBorder: "rgba(0, 212, 255, 0.15)",
  colorBorderSecondary: "rgba(0, 212, 255, 0.1)",
  colorText: "rgba(255, 255, 255, 0.88)",
  colorTextSecondary: "rgba(255, 255, 255, 0.65)",
  colorTextTertiary: "rgba(255, 255, 255, 0.45)",
};

// ---------- 主题上下文 ----------

type ThemeCtx = {
  mode: ThemeMode;
  toggle: () => void;
  setMode: (m: ThemeMode) => void;
  /** 返回对应 antd algorithm */
  algorithm: () => typeof AntTheme.defaultAlgorithm | typeof AntTheme.darkAlgorithm;
};

export const BlueprintThemeContext = createContext<ThemeCtx>({
  mode: "light",
  toggle: () => {},
  setMode: () => {},
  algorithm: () => null as any,
});

export function useBlueprintTheme() {
  return useContext(BlueprintThemeContext);
}

export function readInitialTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(LS_THEME_KEY) as ThemeMode | null;
    if (saved === "dark" || saved === "light") return saved;
  } catch { /* ignore */ }
  // 系统偏好
  try {
    if (matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
  } catch { /* ignore */ }
  return "light";
}
