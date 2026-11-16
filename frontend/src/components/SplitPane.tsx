/**
 * ✅ 可拖拽分隔面板（2026-11-16 新增；同日按「AGENTS 允许新增依赖」改用 re-resizable 重写内部实现）。
 *
 * 需求：各模块界面中「各类信息窗口之间距离可拖拉调整左右、上下大小」。
 *
 * 【为何引入 re-resizable】本组件最初为零依赖手写（Pointer Events + setPointerCapture）。
 * 按用户「AGENTS 规则可允许新增依赖」的授权，评估后改用成熟库 re-resizable（6.11.2，
 * MIT，peer react ^16~19）承载拖拽本体，理由：
 *  1. 拖拽边角它替我扛——pointer/触摸统一、拖拽期间文本选中抑制、父容器尺寸变化跟随、
 *     bounds 边界测量，这些手写版只能逐年补且易漏；
 *  2. direction 原生对齐本组件 orientation（horizontal→right / vertical→bottom）；
 *  3. 维护成本从「我维护一个拖拽引擎」降为「我维护一层薄适配」。
 * 曾评估 react-resizable-panels@4，但其已转向「可停靠工作区」模型（Workspace/Tabset/
 * PopoutWindow），对「两窗格 + 单条拖拽」严重过度设计，故不采用。
 *
 * 【API 契约（逐字保持，6 处接线点零改动）】props 与手写版完全一致：orientation /
 * storageKey / defaultSize / minSize / maxSize / first / second / style / className /
 * handleThickness / handleMargin / ariaLabel。
 *  - 持久化仍由本组件负责（re-resizable 不内置）：onResizeStop 时按 storageKey 写
 *    localStorage，只存「可拖拽那一维」的像素值（horizontal 存 width、vertical 存 height），
 *    初始化读回并钳制到 [min, max]；双击手柄区域重置为 defaultSize。
 *  - 键盘无障碍：re-resizable 手柄自带 tabIndex + 方向键调整。
 *  - 响应式：窄屏（<=768px）由 index.css 的 .swb-workbench-main 断点接管为上下堆叠，
 *    此时隐藏拖拽手柄、窗格恢复 100% 宽。
 *
 * 【布局】根容器 flex（orientation 决定 row / column）。首窗格 = <Resizable>（承载拖拽
 * 手柄），次窗格 = flex:1 占满剩余。首窗格只暴露「可拖拽那一维」的尺寸
 * （horizontal=width / vertical=height），另一维恒 100%。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Resizable } from "re-resizable";
// ✅ 2026-11-16 收口：拖拽 → 通知 resizeCoordinator（图表停手后统一重绘一次）；
//   「重置布局」按钮广播复位事件 → 各 SplitPane 复位到 defaultSize。
import { notifyResize } from "../utils/perf/resizeCoordinator";
import { LAYOUT_RESET_EVENT } from "./resizable/layoutStore";

/** 读 localStorage（SSR / 隐私模式兜底）。 */
function readStoredSize(storageKey: string): number | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

/** 写 localStorage（失败静默，不影响拖拽本身）。 */
function writeStoredSize(storageKey: string, size: number): void {
  try {
    window.localStorage.setItem(storageKey, String(size));
  } catch {
    /* ignore */
  }
}

/** 钳制到 [min, max]。 */
function clampSize(size: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, size));
}

export interface SplitPaneProps {
  /** 分隔方向：horizontal = 左右两窗格（手柄在首窗格右缘）；vertical = 上下两窗格（手柄在下缘）。 */
  orientation: "horizontal" | "vertical";
  /** localStorage 记忆键（每个分隔唯一，避免互相串扰）。 */
  storageKey: string;
  /** 首窗格默认尺寸（px，指可拖拽那一维：horizontal 宽 / vertical 高）。 */
  defaultSize: number;
  /** 首窗格最小尺寸（px）。 */
  minSize: number;
  /** 首窗格最大尺寸（px）。 */
  maxSize: number;
  /** 首窗格内容（受控尺寸的一侧，承载拖拽手柄）。 */
  first: ReactNode;
  /** 次窗格内容（flex:1 占满剩余）。 */
  second: ReactNode;
  /** 根容器附加样式（如 flex:1 / minHeight:0）。 */
  style?: CSSProperties;
  /** 根容器附加类名。 */
  className?: string;
  /** 手柄视觉厚度（px）；默认 6。 */
  handleThickness?: number;
  /** 手柄留白（px，与次窗格拉开视觉间距）；默认 6。 */
  handleMargin?: number;
  /** 拖拽手柄 aria-label。 */
  ariaLabel?: string;
}


/**
 * 通用可拖拽双窗格分隔（内部基于 re-resizable，props API 与手写版逐字一致）。
 *
 * 布局：根容器 flex（orientation 决定 row / column）。首窗格 = <Resizable>（受控尺寸 +
 * 拖拽手柄），次窗格 = flex:1 占满剩余空间。首窗格只暴露「可拖拽那一维」的尺寸
 * （horizontal=width / vertical=height），另一维恒 100%。
 */
export function SplitPane({
  orientation,
  storageKey,
  defaultSize,
  minSize,
  maxSize,
  first,
  second,
  style,
  className,
  handleThickness = 6,
  handleMargin = 6,
  ariaLabel,
}: SplitPaneProps) {
  const isHorizontal = orientation === "horizontal";

  // 首窗格「可拖拽那一维」的尺寸（horizontal=width / vertical=height）。
  // 初始化：读 localStorage → 钳制 → 缺失回落 defaultSize。
  const [size, setSize] = useState<number>(() => {
    const stored = readStoredSize(storageKey);
    if (stored === null) return defaultSize;
    return clampSize(stored, minSize, maxSize);
  });
  // ✅ 拖拽中态：用于 body.swb-resizing（全局禁用文本选中）与手柄高亮。
  const [dragging, setDragging] = useState(false);
  // 根容器 ref：用于把「双击复位」事件精准绑到 re-resizable 渲染的手柄元素上
  //（re-resizable 手柄本身不暴露 onDoubleClick；旧实现把 onDoubleClick 绑在次窗格上，
  //  导致用户在内容区双击选词也会误触发复位）。
  const rootRef = useRef<HTMLDivElement | null>(null);

  // 跨渲染稳定的钳制边界（避免 effect 依赖抖动）。必须声明在下方 effect 之前。
  const boundsRef = useMemo(() => ({ minSize, maxSize }), [minSize, maxSize]);

  // 拖拽开始/结束：切换 body.swb-resizing（全局 user-select:none），
  // 避免拖拽过程中误选中面板内的文字/图表。
  useEffect(() => {
    if (!dragging) return;
    document.body.classList.add("swb-resizing");
    return () => {
      document.body.classList.remove("swb-resizing");
    };
  }, [dragging]);

  // 「重置布局」按钮广播 LAYOUT_RESET_EVENT → 各 SplitPane 复位到 defaultSize 并清掉本地记忆。
  useEffect(() => {
    const onReset = () => {
      try {
        window.localStorage.removeItem(storageKey);
      } catch {
        /* ignore */
      }
      const next = clampSize(defaultSize, boundsRef.minSize, boundsRef.maxSize);
      setSize(next);
    };
    window.addEventListener(LAYOUT_RESET_EVENT, onReset);
    return () => window.removeEventListener(LAYOUT_RESET_EVENT, onReset);
  }, [storageKey, defaultSize, boundsRef]);

  // 拖拽开始：进入拖拽中态（body.swb-resizing + 手柄高亮）。
  const handleResizeStart = useCallback(() => {
    setDragging(true);
  }, []);

  // 拖拽结束：钳制 + 持久化「可拖拽那一维」的像素值；并通知 resizeCoordinator
  // 让图表停手后统一重绘一次（与 window resize 走同一条通道，不产生第二套重绘口径）。
  const handleResizeStop = useCallback(
    (
      _e: MouseEvent | TouchEvent,
      _dir: unknown,
      _elementRef: HTMLElement,
      delta: { width: number; height: number },
    ) => {
      setDragging(false);
      const dimDelta = isHorizontal ? delta.width : delta.height;
      setSize((prev) => {
        const next = clampSize(prev + dimDelta, boundsRef.minSize, boundsRef.maxSize);
        writeStoredSize(storageKey, next);
        return next;
      });
      notifyResize();
    },
    [isHorizontal, boundsRef, storageKey],
  );

  // 双击手柄：复位到 defaultSize（沿用手写版交互）。re-resizable 手柄不暴露
  // onDoubleClick，故绑在「手柄 DOM 元素」上（见下方挂载逻辑），双击内容区不会误触发。
  const resetToDefault = useCallback(() => {
    const next = clampSize(defaultSize, boundsRef.minSize, boundsRef.maxSize);
    setSize(next);
    writeStoredSize(storageKey, next);
  }, [defaultSize, boundsRef, storageKey]);

  // 把手柄的双击复位绑定到 re-resizable 渲染的手柄元素上（horizontal=right / vertical=bottom）。
  useEffect(() => {
    if (!rootRef.current) return;
    const el = rootRef.current.querySelector<HTMLElement>(".swb-split-handle");
    if (!el) return;
    const onDbl = () => resetToDefault();
    el.addEventListener("dblclick", onDbl);
    return () => {
      el.removeEventListener("dblclick", onDbl);
    };
  }, [resetToDefault, isHorizontal]);

  const rootStyle: CSSProperties = {
    display: "flex",
    flexDirection: isHorizontal ? "row" : "column",
    gap: 0, // 视觉间距由次窗格的 padding（= handleMargin）补回
    minWidth: 0,
    minHeight: 0,
    ...style,
  };

  // 首窗格（Resizable）：只设「可拖拽那一维」的受控尺寸，另一维恒 100%。
  // ✅ R252：窗格成为 flex 容器（display:flex），直接子元素由 index.css 的
  //   `.swb-split-first > *` / `.swb-split-second > *` flex:1 填满窗格高度，修复
  //   height:100% 在 flex 子项（computed height 为 auto）下塌陷导致 overflow 不触发。
  //   vertical 补 height:100% 使 swb-split-first 填满 Resizable 的受控高度。
  const resizableStyle: CSSProperties = isHorizontal
    ? { height: "100%", overflow: "hidden", display: "flex", flexDirection: "row" }
    : { width: "100%", height: "100%", overflow: "hidden", display: "flex", flexDirection: "column" };

  const controlledSize = isHorizontal
    ? { width: size, height: "100%" }
    : { width: "100%", height: size };

  // ✅ R252：次窗格同为 flex 容器，直接子元素 flex:1 填满剩余高度（同上）。
  const secondStyle: CSSProperties = {
    flex: 1,
    minWidth: 0,
    minHeight: 0,
    display: "flex",
    flexDirection: isHorizontal ? "row" : "column",
    ...(isHorizontal ? { paddingLeft: handleMargin } : { paddingTop: handleMargin }),
  };

  // 拖拽手柄视觉：覆盖 re-resizable 默认的 10px 半透明区，收成 handleThickness 细条。
  const handleStyle: CSSProperties = isHorizontal
    ? {
        width: handleThickness,
        right: 0,
        top: 0,
        bottom: 0,
        cursor: "col-resize",
        background: "transparent",
        border: 0,
        borderRadius: 4,
      }
    : {
        height: handleThickness,
        bottom: 0,
        left: 0,
        right: 0,
        cursor: "row-resize",
        background: "transparent",
        border: 0,
        borderRadius: 4,
      };

  return (
    <div
      ref={rootRef}
      className={
        className
          ? `swb-split swb-split-${orientation} ${className}`
          : `swb-split swb-split-${orientation}`
      }
      style={rootStyle}
    >
      <Resizable
        size={controlledSize}
        minWidth={isHorizontal ? minSize : undefined}
        maxWidth={isHorizontal ? maxSize : undefined}
        minHeight={isHorizontal ? undefined : minSize}
        maxHeight={isHorizontal ? undefined : maxSize}
        enable={{
          top: false,
          right: isHorizontal,
          bottom: !isHorizontal,
          left: false,
          topRight: false,
          bottomRight: false,
          bottomLeft: false,
          topLeft: false,
        }}
        onResizeStart={handleResizeStart}
        onResizeStop={handleResizeStop}
        handleStyles={isHorizontal ? { right: handleStyle } : { bottom: handleStyle }}
        handleClasses={isHorizontal ? { right: "swb-split-handle" } : { bottom: "swb-split-handle" }}
      >
        <div className="swb-split-first" style={resizableStyle}>
          {first}
        </div>
      </Resizable>
      {/* 双击复位已绑到手柄元素（见上方 useEffect），内容区双击不再误触发复位。 */}
      <div className="swb-split-second" style={secondStyle} title={ariaLabel}>
        {second}
      </div>
    </div>
  );
}

export default SplitPane;
