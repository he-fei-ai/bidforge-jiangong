/**
 * 响应式 + 暗色模式 + 通用 UI 组件
 * ----------------------------------------------------------
 * Blueprint 工程蓝图主题配套组件。
 * 所有组件零业务依赖，纯表现层。
 */
import React, { useEffect, useState, useSyncExternalStore } from "react";
import { Button, Skeleton, Tooltip } from "antd";
import { BulbOutlined, BulbFilled } from "@ant-design/icons";
import { useBlueprintTheme, type ThemeMode } from "./theme";

// ---------- useBreakpoint ----------

/**
 * 断点定义（移动优先）
 * ----------------------------------------------------------
 * - BP_XS      380  小手机（iPhone SE/mini 375）
 * - BP_PHONE   560  手机竖屏上界（iPhone 14/15 390-430）
 * - BP_SMALL   768  手机横屏 / 小平板
 * - BP_MEDIUM  900  常规平板（内容区由 2 列转 1 列）
 * - BP_TABLET  1200 平板 / 笔记本边界（侧边栏自动折叠阈值）
 * - BP_DESKTOP 1440 大屏
 * 以上 6 个值与 index.css 的 @media 断点（380 / 560 / 768 / 900 / 1200 / 1440）
 * 一一对应；新增或调整断点时必须两处同步修改。
 */
export const BP_XS = 380;
export const BP_PHONE = 560;
export const BP_SMALL = 768;
export const BP_MEDIUM = 900;
export const BP_TABLET = 1200; // 平板（侧边栏自动折叠阈值）
export const BP_DESKTOP = 1440;

/**
 * ✅ 2026-11-11 性能：断点宽度改为**单一模块级外部 store**。
 *
 * 旧实现每个 useBreakpoint() 调用方各自挂一个 resize 监听 + 各自 setState，
 * 本仓有 5 个调用点 ⇒ 拖窗口时同帧 5 次订阅通知、5 次组件树 diff。
 * 现在全局只挂 1 个监听、按 rAF 合帧，5 个组件通过 useSyncExternalStore
 * 共享同一个宽度快照；最后一个订阅者卸载时移除监听/取消 rAF（无泄漏）。
 * 初始值、各断点计算与返回字段逐字不变。
 */
let bpWidth = typeof window !== "undefined" ? window.innerWidth : BP_DESKTOP;
const bpListeners = new Set<() => void>();
let bpRaf = 0;
let bpAttached = false;
let bpOnResize: (() => void) | null = null;

function bpGetWidth(): number {
  // 直接读实时宽度而非模块缓存：测试/SSR 在 mount 前改 window.innerWidth 时，
  // 语义与旧 useState initializer 保持一致。number 是不可变原始值，
  // 满足 useSyncExternalStore 对 getSnapshot 返回稳定值的要求。
  return typeof window !== "undefined" ? window.innerWidth : BP_DESKTOP;
}

function bpSubscribe(cb: () => void): () => void {
  if (!bpAttached && typeof window !== "undefined") {
    bpAttached = true;
    bpOnResize = () => {
      if (bpRaf) return;
      bpRaf = window.requestAnimationFrame(() => {
        bpRaf = 0;
        const next = window.innerWidth;
        if (next === bpWidth) return;
        bpWidth = next;
        for (const listener of bpListeners) listener();
      });
    };
    window.addEventListener("resize", bpOnResize, { passive: true });
  }
  bpListeners.add(cb);
  return () => {
    bpListeners.delete(cb);
    if (bpListeners.size === 0 && bpAttached) {
      if (bpOnResize) window.removeEventListener("resize", bpOnResize);
      if (bpRaf) window.cancelAnimationFrame(bpRaf);
      bpRaf = 0;
      bpOnResize = null;
      bpAttached = false;
    }
  };
}

export function useBreakpoint() {
  const w = useSyncExternalStore(bpSubscribe, bpGetWidth, () => BP_DESKTOP);
  return {
    width: w,
    isXs: w < BP_XS, // 极窄屏 (<380)
    isPhone: w < BP_PHONE, // 手机竖屏 (<560)
    isSmall: w < BP_SMALL, // 手机 (<768)
    isTablet: w >= BP_SMALL && w < BP_TABLET,
    isCompact: w < BP_TABLET, // 侧边栏需要折叠
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
        {!collapsed && (
          <span style={{ marginLeft: 4, fontSize: 12 }}>{isDark ? "深色" : "浅色"}</span>
        )}
      </Button>
    </Tooltip>
  );
}

// ---------- PageSkeleton ----------

/**
 * 页面级加载骨架
 * ----------------------------------------------------------
 * data：页面首屏的「数据未到」统一用骨架屏，而不是全屏 Spin。
 * 局部/行内等待（按钮、抽屉内小区域）仍可用 Spin，两者语义不同。
 */
export function PageSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div style={{ padding: 24 }}>
      <Skeleton active title paragraph={{ rows }} />
    </div>
  );
}

// ---------- PageHero ----------

type PageHeroProps = {
  title: string;
  subtitle?: string; // 英文/工程标识，如 "BLUEPRINT ENGINEERING · PROJECTS"
  description?: string;
  accent?: React.ReactNode; // 右对齐内容，可传按钮/JSX
  scheme?: "hero" | "flat"; // hero=深蓝渐变网格, flat=扁平浅灰
};

export function PageHero({ title, subtitle, description, accent, scheme = "hero" }: PageHeroProps) {
  const isHero = scheme === "hero";
  const { isPhone, isSmall } = useBreakpoint();
  // 手机：单列布局，padding 收敛；桌面：原有双列
  const padding = isPhone
    ? "12px 10px"
    : isSmall
      ? "14px 12px"
      : isHero
        ? "22px 28px"
        : "16px 24px";
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
              top: -50,
              right: -30,
              width: 180,
              height: 180,
              borderRadius: "50%",
              background: "radial-gradient(circle, rgba(0, 212, 255, 0.22), transparent 70%)",
              pointerEvents: "none",
            }}
          />
        </>
      )}

      <div
        style={{
          position: "relative",
          display: "flex",
          flexDirection: flexDir as "row" | "column",
          justifyContent: "space-between",
          alignItems: align as "center" | "flex-start",
          gap: isPhone ? 8 : 16,
          width: "100%",
        }}
      >
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
        {accent && (
          <div style={{ flexShrink: 0, minWidth: 0, display: "flex", flexWrap: "wrap", gap: 8 }}>
            {accent}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- StatCards ----------

export type StatItem = {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  color: string; // 主色，如 #00D4FF
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
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLElement).style.boxShadow = "none";
      }}
    >
      <div
        style={{
          width: 38,
          height: 38,
          borderRadius: 4,
          background: `${item.color}18`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: item.color,
          fontSize: 18,
          flexShrink: 0,
        }}
      >
        {item.icon}
      </div>
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontSize: 20,
            fontWeight: 700,
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
            <span
              style={{
                fontSize: 12,
                fontWeight: 400,
                marginLeft: 2,
                color: isDark ? "#9AB4D0" : "#8B99AC",
              }}
            >
              {item.suffix}
            </span>
          )}
        </div>
        <div
          style={{
            fontSize: 11,
            color: isDark ? "#6F87A5" : "#8B99AC",
            marginTop: 2,
            letterSpacing: 0.3,
          }}
        >
          {item.label}
        </div>
      </div>
    </div>
  );
}

// 确保 ThemeMode 被类型系统使用
export type _ThemeModeRef = ThemeMode;
