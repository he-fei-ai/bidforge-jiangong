/**
 * SplitPane · 可拖拽调整宽高的两栏布局（2026-10-30 R145，自研轻量实现）
 *
 * 为什么自研而不是 react-resizable-panels / splitpanes：
 *  - react-resizable-panels 只有百分比 min/max，无法表达「侧边栏 200-400px」这类
 *    px 级防坍塌约束；splitpanes 同为百分比模型且会接管 DOM 结构；
 *  - 本应用 5 个可拖拽区域全部是「两栏切分」（左右 / 上下），嵌套即组合，
 *    一个两栏组件 + 通用手柄即可覆盖，零新增依赖。
 *
 * 实现要点（对应需求二/三）：
 *  1. 拖拽：Pointer Events + setPointerCapture（鼠标/触摸统一），拖拽中
 *     body.bp-resize-dragging 全局禁用文本选中，光标锁定 col-resize/row-resize；
 *  2. 手柄：8px 命中区 + 4px 视觉条，悬停/拖拽高亮（样式见 index.css）；
 *  3. 持久化：localStorage 存比例（刷新后保持），「重置布局」回默认；
 *  4. 防坍塌：渲染用 CSS flex-basis:% + min/max-width/height(px) 夹紧，
 *     ResizeObserver 监听容器尺寸变化再次夹紧（窗口缩窄不坍塌、不溢出）；
 *  5. 嵌套：手柄 pointerdown stopPropagation，内外层 SplitPane 互不干扰；
 *  6. 窄屏降级：disabled=true 时渲染为上下堆叠单栏、不挂手柄。
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {
  clearStoredFraction,
  readStoredFraction,
  useResizableResetSignal,
  writeStoredFraction,
} from "./resizableLayout";

export type SplitDirection = "horizontal" | "vertical";

const BODY_DRAG_CLASS = "bp-resize-dragging";
const BODY_AXIS_X_CLASS = "bp-resize-axis-x";
const BODY_AXIS_Y_CLASS = "bp-resize-axis-y";

/** 拖拽中途组件卸载（切 Tab / 窄屏切换）时也要摘掉 body 上的全局类 */
function removeBodyDragClasses(): void {
  document.body.classList.remove(BODY_DRAG_CLASS, BODY_AXIS_X_CLASS, BODY_AXIS_Y_CLASS);
}

type ResizeHandleProps = {
  direction: SplitDirection;
  disabled?: boolean;
  /** 每次移动的增量 px（横向为 dx，纵向为 dy） */
  onDelta: (deltaPx: number) => void;
  /** 拖拽结束（持久化时机）；拖拽中卸载也会尽力补发一次 */
  onDragEnd?: () => void;
  /** 双击分隔条：恢复默认比例（同时清掉本块持久化） */
  onReset?: () => void;
  ariaLabel?: string;
  /** 无障碍：resizable separator 应暴露当前值（px 或 %，由调用方定单位） */
  ariaValueNow?: number;
  ariaValueMin?: number;
  ariaValueMax?: number;
  className?: string;
  /** 测试锚点 */
  testId?: string;
};

/**
 * 通用拖拽手柄。既被 SplitPane 内部使用，也单独暴露给
 * 「antd Sider ↔ 主区域」这类不便用两栏组件包裹的场景（Layout region 1）。
 */
export function ResizeHandle({
  direction,
  disabled = false,
  onDelta,
  onDragEnd,
  onReset,
  ariaLabel,
  ariaValueNow,
  ariaValueMin,
  ariaValueMax,
  className,
  testId,
}: ResizeHandleProps) {
  const [dragging, setDragging] = useState(false);
  const draggingRef = useRef(false);
  const lastPosRef = useRef(0);
  // onDragEnd 经 ref 中转：拖拽中卸载时 cleanup 也能按最新回调补发持久化
  const dragEndRef = useRef(onDragEnd);
  useEffect(() => {
    dragEndRef.current = onDragEnd;
  }, [onDragEnd]);
  const wasDraggingRef = useRef(false);

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (disabled || e.button !== 0) return;
    // ✅ 嵌套防冲突：内层手柄不把事件冒泡给外层 SplitPane / 页面拖拽逻辑
    e.stopPropagation();
    e.preventDefault();
    draggingRef.current = true;
    wasDraggingRef.current = true;
    lastPosRef.current = direction === "horizontal" ? e.clientX : e.clientY;
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // jsdom / 老浏览器无 setPointerCapture：拖拽仍可用（move 事件在手柄上触发）
    }
    setDragging(true);
    document.body.classList.add(BODY_DRAG_CLASS);
    document.body.classList.add(direction === "horizontal" ? BODY_AXIS_X_CLASS : BODY_AXIS_Y_CLASS);
  };

  const handlePointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    e.stopPropagation();
    const pos = direction === "horizontal" ? e.clientX : e.clientY;
    const delta = pos - lastPosRef.current;
    lastPosRef.current = pos;
    if (delta !== 0) onDelta(delta);
  };

  const endDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    removeBodyDragClasses();
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // ignore
    }
    onDragEnd?.();
  };

  /** 双击恢复默认：清本块持久化 + 回默认比例（不冒泡给外层 SplitPane） */
  const handleDoubleClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (disabled) return;
    e.stopPropagation();
    e.preventDefault();
    onReset?.();
  };

  // ✅ R148（2026-10-30）键盘可达性：resizable separator 必须可聚焦 + 支持方向键
  //    （WAI-ARIA APG Separator 模式）。每次按键 16px（约一档），方向与切分轴对应：
  //    横向切分（竖条）→ ←/→；纵向切分（横条）→ ↑/↓。
  const handleKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    const step = 16;
    const horizontal = direction === "horizontal";
    const delta =
      (horizontal && e.key === "ArrowRight") || (!horizontal && e.key === "ArrowDown")
        ? step
        : (horizontal && e.key === "ArrowLeft") || (!horizontal && e.key === "ArrowUp")
          ? -step
          : 0;
    if (delta !== 0) {
      e.preventDefault();
      onDelta(delta);
      // ✅ R148b：键盘调整同样要落地持久化 —— 否则键盘用户调好的比例刷新即丢失，
      //    与「拖拽结果存 LocalStorage」口径不一致（无障碍路径不能是二等公民）。
      onDragEnd?.();
    }
  };

  // 卸载兜底：清 body 全局类 + 补发持久化（拖拽中途切 Tab/窄屏）
  useEffect(
    () => () => {
      removeBodyDragClasses();
      if (wasDraggingRef.current) {
        wasDraggingRef.current = false;
        dragEndRef.current?.();
      }
    },
    [],
  );

  return (
    <div
      role="separator"
      // aria 语义：横向切分的手柄自身是竖条
      aria-orientation={direction === "horizontal" ? "vertical" : "horizontal"}
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      // ✅ R148 无障碍：resizable separator 可聚焦（tabIndex=0）；disabled 时不进 Tab 序
      tabIndex={disabled ? -1 : 0}
      // resizable separator 暴露当前值范围（px 或 %，由调用方传入）
      aria-valuenow={ariaValueNow}
      aria-valuemin={ariaValueMin}
      aria-valuemax={ariaValueMax}
      data-testid={testId}
      data-resize-axis={direction}
      className={`bp-resize-handle axis-${direction === "horizontal" ? "x" : "y"}${dragging ? " dragging" : ""}${disabled ? " disabled" : ""} ${className ?? ""}`}
      style={disabled ? { pointerEvents: "none" } : undefined}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={handleDoubleClick}
      onKeyDown={handleKeyDown}
    />
  );
}

export type SplitPaneProps = {
  /** horizontal = 左右两栏（调宽）；vertical = 上下两栏（调高） */
  direction: SplitDirection;
  /** localStorage 键（自动加 bp-rp: 前缀），同键即同一块持久化布局 */
  storageKey: string;
  /** 无持久化值时的默认首面板占比（0~1），默认 0.3 */
  defaultRatio?: number;
  /** 首面板最小 px（防坍塌），默认 120 */
  min?: number;
  /** 首面板最大 px，默认不限制 */
  max?: number;
  /** 次面板最小 px（防首面板吃满），默认 120 */
  minSecond?: number;
  first: ReactNode;
  second: ReactNode;
  /** 窄屏降级：true 时上下堆叠单栏、不挂手柄、不读不写持久化 */
  disabled?: boolean;
  className?: string;
  style?: CSSProperties;
  firstStyle?: CSSProperties;
  secondStyle?: CSSProperties;
  handleAriaLabel?: string;
};

const DEFAULT_RATIO = 0.3;
const DEFAULT_MIN = 120;
const DEFAULT_MIN_SECOND = 120;

export function SplitPane({
  direction,
  storageKey,
  defaultRatio = DEFAULT_RATIO,
  min = DEFAULT_MIN,
  max,
  minSecond = DEFAULT_MIN_SECOND,
  first,
  second,
  disabled = false,
  className,
  style,
  firstStyle,
  secondStyle,
  handleAriaLabel,
}: SplitPaneProps) {
  const resetSignal = useResizableResetSignal();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const firstRef = useRef<HTMLDivElement | null>(null);
  const [ratio, setRatio] = useState<number>(() =>
    disabled ? defaultRatio : readStoredFraction(storageKey) ?? defaultRatio,
  );
  // 手柄 onDragEnd / RO 回调里要拿最新 ratio，走 ref
  const ratioRef = useRef(ratio);
  useEffect(() => {
    ratioRef.current = ratio;
  }, [ratio]);

  // 「重置布局」：清本块持久化 + 回默认比例（跳过初始信号 0）
  const prevSignalRef = useRef(resetSignal);
  useEffect(() => {
    if (prevSignalRef.current === resetSignal) return;
    prevSignalRef.current = resetSignal;
    clearStoredFraction(storageKey);
    setRatio(defaultRatio);
  }, [resetSignal, storageKey, defaultRatio]);

  /** 把比例按容器实际尺寸 + px 上下限夹紧 */
  const clampRatio = useCallback(
    (r: number): number => {
      const el = containerRef.current;
      const total = el ? (direction === "horizontal" ? el.clientWidth : el.clientHeight) : 0;
      if (total <= 0) return r;
      const maxPx = Math.min(max ?? Number.POSITIVE_INFINITY, total - minSecond);
      const lo = Math.min(min, Math.max(0, maxPx));
      const px = Math.max(lo, Math.min(r * total, maxPx));
      return px / total;
    },
    [direction, min, max, minSecond],
  );

  // 容器尺寸变化（窗口缩放 / 上层面板拖拽联动）时重新夹紧 —— ResizeObserver 兜底
  useEffect(() => {
    if (disabled) return;
    setRatio((r) => clampRatio(r));
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setRatio((r) => clampRatio(r)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [clampRatio, disabled]);

  const handleDelta = useCallback(
    (delta: number) => {
      const el = containerRef.current;
      const firstEl = firstRef.current;
      if (!el || !firstEl) return;
      const total = direction === "horizontal" ? el.clientWidth : el.clientHeight;
      if (total <= 0) return;
      const rect = firstEl.getBoundingClientRect();
      const curPx = direction === "horizontal" ? rect.width : rect.height;
      const next = clampRatio((curPx + delta) / total);
      // ✅ R148b：同步写 ref，使紧随其后的 onDragEnd（键盘调整后立即持久化）
      //    读到的是**新比例**而不是上一次渲染的旧值（effect 里的同步尚未生效）。
      ratioRef.current = next;
      setRatio(next);
    },
    [direction, clampRatio],
  );

  const persist = useCallback(() => {
    writeStoredFraction(storageKey, ratioRef.current);
  }, [storageKey]);

  /** 双击 / 全局重置：清掉本块持久化并回到默认比例 */
  const resetToDefault = useCallback(() => {
    clearStoredFraction(storageKey);
    setRatio(defaultRatio);
  }, [storageKey, defaultRatio]);

  if (disabled) {
    // 窄屏降级：单栏堆叠（first 在上、second 在下），无手柄无持久化
    return (
      <div
        ref={containerRef}
        className={`bp-resize-group disabled ${className ?? ""}`}
        style={style}
        data-storage-key={storageKey}
      >
        <div className="bp-resize-pane" style={{ flex: "1 1 auto", ...firstStyle }}>
          {first}
        </div>
        <div className="bp-resize-pane" style={{ flex: "1 1 auto", ...secondStyle }}>
          {second}
        </div>
      </div>
    );
  }

  const firstPaneStyle: CSSProperties =
    direction === "horizontal"
      ? {
          flex: `0 0 ${(ratio * 100).toFixed(4)}%`,
          minWidth: min,
          maxWidth: max,
          ...firstStyle,
        }
      : {
          flex: `0 0 ${(ratio * 100).toFixed(4)}%`,
          minHeight: min,
          maxHeight: max,
          ...firstStyle,
        };

  const secondPaneStyle: CSSProperties =
    direction === "horizontal"
      ? { flex: "1 1 0", minWidth: minSecond, ...secondStyle }
      : { flex: "1 1 0", minHeight: minSecond, ...secondStyle };

  // ✅ R148 无障碍：resizable separator 暴露当前值（首面板占比 %）与可调范围。
  //    clampRatio(0)/clampRatio(1) 即 px min/max 换算后的比例边界；容器尺寸未知
  //    （jsdom / 初始渲染）时只给 now，不给 min/max（避免无意义 0/100 边界）。
  const containerPx = containerRef.current
    ? direction === "horizontal"
      ? containerRef.current.clientWidth
      : containerRef.current.clientHeight
    : 0;
  const ariaValueNow = Math.round(ratio * 100);
  const ariaValueMin = containerPx > 0 ? Math.round(clampRatio(0) * 100) : undefined;
  const ariaValueMax = containerPx > 0 ? Math.round(clampRatio(1) * 100) : undefined;

  return (
    <div
      ref={containerRef}
      className={`bp-resize-group axis-${direction === "horizontal" ? "x" : "y"} ${className ?? ""}`}
      style={style}
      data-storage-key={storageKey}
    >
      <div ref={firstRef} className="bp-resize-pane" style={firstPaneStyle}>
        {first}
      </div>
      <ResizeHandle
        direction={direction}
        onDelta={handleDelta}
        onDragEnd={persist}
        onReset={resetToDefault}
        ariaLabel={handleAriaLabel}
        ariaValueNow={ariaValueNow}
        ariaValueMin={ariaValueMin}
        ariaValueMax={ariaValueMax}
        testId={`resize-handle-${storageKey}`}
      />
      <div className="bp-resize-pane" style={secondPaneStyle}>
        {second}
      </div>
    </div>
  );
}
