/**
 * 响应式 + 暗色模式 + 通用 UI 组件
 * ----------------------------------------------------------
 * Blueprint 工程蓝图主题配套组件。
 * 所有组件零业务依赖，纯表现层。
 */
import React, { useEffect, useState } from "react";
import { Button, Tooltip } from "antd";
import { BulbOutlined, BulbFilled } from "@ant-design/icons";
import { useBlueprintTheme, type ThemeMode } from "./theme";

// ---------- useBreakpoint ----------

/**
 * 断点定义（移动优先）
 * ----------------------------------------------------------
 * - BP_XS     380  小手机（iPhone SE/mini 375）
 * - BP_PHONE  560  手机竖屏上界（iPhone 14/15 390-430）
 * - BP_SMALL  768  手机横屏 / 小平板（历史兼容值）
 * - BP_TABLET 1200 平板 / 笔记本边界（侧边栏自动折叠阈值）
 * - BP_DESKTOP 1600 大屏
 * 断点值与 index.css 中的 @media 断点保持一致。
 */
export const BP_XS = 380;
export const BP_PHONE = 560;
export const BP_SMALL = 768;    // 手机
export const BP_TABLET = 1200;  // 平板（侧边栏自动折叠阈值）
export const BP_DESKTOP = 1600;

export function useBreakpoint() {
  const [w, setW] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth : BP_DESKTOP
  );
  useEffect(() => {
    // ✅ 性能优化：resize 在拖动窗口边缘时会以远高于 60Hz 的频率连续触发，
    // 且本页有 6 处调用方各挂一个监听器 —— 同帧内 6 个组件重复 setState、
    // 6 次整树 diff，掉帧明显。改为按 rAF 合帧：一帧只取一次最新宽度。
    // 语义不变：挂载时仍同步读取 window.innerWidth（初始值不受影响）。
    let raf = 0;
    let latest = window.innerWidth;
    const onResize = () => {
      latest = window.innerWidth;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        setW(latest);
      });
    };
    window.addEventListener("resize", onResize);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
    };
  }, []);
  return {
    width: w,
    isXs: w < BP_XS,              // 极窄屏 (<380)
    isPhone: w < BP_PHONE,        // 手机竖屏 (<560)
    isSmall: w < BP_SMALL,        // 手机 (<768)
    isTablet: w >= BP_SMALL && w < BP_TABLET,
    isCompact: w < BP_TABLET,     // 侧边栏需要折叠
    isDesktop: w >= BP_TABLET,
  };
}

// ---------- ThemeToggle ----------

export function ThemeToggle({ collapsed }: { collapsed?: boolean }) {
  const { mode, toggle } = useBlueprintTheme();
  const isDark = mode === "dark";
  return (
    <Tooltip title={isDark ? "切换到浅色" : "切换到深色"} placement="left">
      <Button
        type="text"
        size="small"
        onClick={toggle}
        icon={isDark ? <BulbFilled style={{ color: "#FFB800" }} /> : <BulbOutlined />}
        style={{ color: isDark ? "#9AB4D0" : "#5A6A80" }}
      >
        {!collapsed && <span style={{ marginLeft: 4, fontSize: 12 }}>{isDark ? "深色" : "浅色"}</span>}
      </Button>
    </Tooltip>
  );
}

// ---------- PageHero ----------

type PageHeroProps = {
  title: string;
  subtitle?: string;        // 英文/工程标识，如 "BLUEPRINT ENGINEERING · PROJECTS"
  description?: string;
  accent?: React.ReactNode; // 右对齐内容，可传按钮/JSX
  scheme?: "hero" | "flat"; // hero=深蓝渐变网格, flat=扁平浅灰
};

export function PageHero({ title, subtitle, description, accent, scheme = "hero" }: PageHeroProps) {
  const isHero = scheme === "hero";
  const { isPhone, isSmall } = useBreakpoint();
  // 手机：单列布局，padding 收敛；桌面：原有双列
  const padding = isPhone ? "12px 10px" : isSmall ? "14px 12px" : isHero ? "22px 28px" : "16px 24px";
  const flexDir = isPhone || isSmall ? "column" : "row";
  const align = isPhone || isSmall ? "flex-start" : "center";
  const titleSize = isPhone ? 16 : isSmall ? 17 : isHero ? 20 : 16;
  return (
    <div
      className={`bp-hero bp-hero-${scheme}`}
      style={{
        position: "relative",
        padding,
        borderRadius: 4,
        overflow: "hidden",
        flexShrink: 0,
        background: isHero
          ? "linear-gradient(135deg, #0F2B4F 0%, #16395F 60%, #1A4D7A 100%)"
          : undefined,
        border: isHero ? undefined : "1px solid var(--bp-border)",
        backgroundImage: isHero
          ? "linear-gradient(135deg, #0F2B4F 0%, #16395F 60%, #1A4D7A 100%)"
          : undefined,
      }}
    >
      {isHero && (
        <>
          {/* 蓝图网格 */}
          <div
            className="bp-hero-grid"
            style={{
              position: "absolute",
              inset: 0,
              backgroundImage:
                "linear-gradient(rgba(0, 212, 255, 0.12) 1px, transparent 1px)," +
                "linear-gradient(90deg, rgba(0, 212, 255, 0.12) 1px, transparent 1px)",
              backgroundSize: "24px 24px",
              pointerEvents: "none",
            }}
          />
          {/* 装饰光效 */}
          <div
            style={{
              position: "absolute",
              top: -50, right: -30, width: 180, height: 180,
              borderRadius: "50%",
              background: "radial-gradient(circle, rgba(0, 212, 255, 0.22), transparent 70%)",
              pointerEvents: "none",
            }}
          />
        </>
      )}

      <div style={{ position: "relative", display: "flex", flexDirection: flexDir as "row" | "column", justifyContent: "space-between", alignItems: align as "center" | "flex-start", gap: isPhone ? 8 : 16, width: "100%" }}>
        <div style={{ minWidth: 0, flex: "1 1 auto" }}>
          <div
            style={{
              color: isHero ? "#FFFFFF" : "var(--bp-text)",
              fontSize: titleSize,
              fontWeight: 700,
              letterSpacing: 0.3,
              lineHeight: 1.3,
              wordBreak: "break-word",
            }}
          >
            {title}
          </div>
          {subtitle && (
            <div
              style={{
                color: isHero ? "rgba(0, 212, 255, 0.75)" : "var(--bp-text-tertiary)",
                fontSize: 11,
                letterSpacing: "2px",
                fontFamily: "'JetBrains Mono', monospace",
                marginTop: isHero ? 4 : 2,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                maxWidth: "100%",
              }}
            >
              {subtitle}
            </div>
          )}
          {description && (
            <div
              style={{
                color: isHero ? "rgba(255, 255, 255, 0.65)" : "var(--bp-text-secondary)",
                fontSize: 12,
                marginTop: 6,
                maxWidth: "100%",
                wordBreak: "break-word",
              }}
            >
              {description}
            </div>
          )}
        </div>
        {accent && <div style={{ flexShrink: 0, minWidth: 0, display: "flex", flexWrap: "wrap", gap: 8 }}>{accent}</div>}
      </div>
    </div>
  );
}

// ---------- StatCards ----------

export type StatItem = {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  color: string;    // 主色，如 #00D4FF
  suffix?: string;
};

export function StatCards({ items }: { items: StatItem[] }) {
  const { isPhone, isSmall, isCompact } = useBreakpoint();
  // 手机：单列；小屏平板：2 列；桌面：最多 4 列
  const cols = isPhone ? 1 : isSmall ? 2 : Math.min(items.length, 4);
  return (
    <div
      className="bp-stat-cards"
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gap: 12,
        flexShrink: 0,
        width: "100%",
        minWidth: 0,
      }}
    >
      {items.map((s) => (
        <StatCard key={s.label} item={s} />
      ))}
    </div>
  );
}

function StatCard({ item }: { item: StatItem }) {
  const { mode } = useBlueprintTheme();
  const isDark = mode === "dark";
  return (
    <div
      className="bp-stat-card"
      style={{
        background: isDark ? "#12243D" : "#FFFFFF",
        borderRadius: 4,
        padding: "12px 16px",
        display: "flex",
        alignItems: "center",
        gap: 14,
        border: `1px solid ${isDark ? "#284466" : "#E8EDF4"}`,
        transition: "box-shadow 0.2s",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLElement).style.boxShadow =
          "0 2px 6px rgba(15,43,79,0.06), 0 4px 12px rgba(15,43,79,0.05)";
      }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.boxShadow = "none"; }}
    >
      <div
        style={{
          width: 38, height: 38, borderRadius: 4,
          background: `${item.color}18`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: item.color, fontSize: 18, flexShrink: 0,
        }}
      >
        {item.icon}
      </div>
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontSize: 20, fontWeight: 700,
            fontFamily: "'JetBrains Mono', monospace",
            color: isDark ? "#D8E6F4" : "#1A2332",
            lineHeight: 1.1,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {item.value}
          {item.suffix && (
            <span style={{ fontSize: 12, fontWeight: 400, marginLeft: 2, color: isDark ? "#9AB4D0" : "#8B99AC" }}>
              {item.suffix}
            </span>
          )}
        </div>
        <div style={{ fontSize: 11, color: isDark ? "#6F87A5" : "#8B99AC", marginTop: 2, letterSpacing: 0.3 }}>
          {item.label}
        </div>
      </div>
    </div>
  );
}

// 确保 ThemeMode 被类型系统使用
export type _ThemeModeRef = ThemeMode;
