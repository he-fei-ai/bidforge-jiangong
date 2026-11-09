/**
 * ✅ R179 N1（2026-11-05）：目录生成自动确认 —— 纯判定逻辑（可 vitest 直接覆盖）。
 *
 * 分工原则（方案 §3.3/§3.4）：
 *   - 后端 `outline_auto_confirm.evaluate()` 只回答 `{ready, blockers}`（唯一事实源）；
 *   - 本模块只做**客户端可判定的部分**：治理开关是否开启 + 后端信号是否 ready +
 *     目录非空 + 是否已存在正文；
 *   - 默认关闭时 `decideAutoConfirm` **恒返回 manual** —— 人工路径
 *     （`useOutlineGate.openOutlineGate`）逐字不变。
 */
export interface AutoConfirmBlocker {
  code: string;
  message: string;
  count: number;
}

export interface AutoConfirmSignal {
  ready: boolean;
  blockers: AutoConfirmBlocker[];
}

/** 与后端 `outline_auto_confirm.evaluate()` 的 blocker code 一一对应（仅用于展示）。 */
export const AUTO_CONFIRM_BLOCKER_LABELS: Record<string, string> = {
  no_outline: "目录为空",
  placeholder_titles: "占位/泛化标题",
  numbering_invalid: "编号/层级不连续",
  empty_chapters: "空章",
  failed_sections: "失败章节",
  existing_content: "已存在正文",
  /** ✅ R208：后端自动确认前置判定异常时回传的兜底 blocker（不再静默降级） */
  evaluate_failed: "自动确认判定异常",
};

/** 宽松解析 SSE completed / checkpoint 回传的 `auto_confirm`。
 *  旧后端、断线重挂缺键、脏数据一律按「未就绪」处理（绝不因解析失败而自动确认）。 */
export function parseAutoConfirm(raw: unknown): AutoConfirmSignal {
  if (!raw || typeof raw !== "object") return { ready: false, blockers: [] };
  const obj = raw as { ready?: unknown; blockers?: unknown };
  const blockers: AutoConfirmBlocker[] = [];
  if (Array.isArray(obj.blockers)) {
    for (const item of obj.blockers) {
      if (!item || typeof item !== "object") continue;
      const b = item as { code?: unknown; message?: unknown; count?: unknown };
      const code = typeof b.code === "string" ? b.code : "";
      if (!code) continue;
      blockers.push({
        code,
        message:
          typeof b.message === "string" && b.message.trim()
            ? b.message
            : AUTO_CONFIRM_BLOCKER_LABELS[code] || code,
        count: typeof b.count === "number" && Number.isFinite(b.count) ? b.count : 1,
      });
    }
  }
  return { ready: obj.ready === true && blockers.length === 0, blockers };
}

export interface AutoConfirmDecision {
  mode: "manual" | "auto";
  /** manual 的原因码（仅用于日志/提示，不参与判定） */
  reason: "switch_off" | "not_ready" | "existing_content" | "empty_outline" | "ready";
}

/** 是否启动自动确认倒计时。开关关闭时恒 manual（人工路径零行为变化）。 */
export function decideAutoConfirm(
  enabled: boolean,
  signal: AutoConfirmSignal,
  options: { hasContent: boolean; outlineLength: number },
): AutoConfirmDecision {
  if (!enabled) return { mode: "manual", reason: "switch_off" };
  if (options.hasContent) return { mode: "manual", reason: "existing_content" };
  if (!signal.ready) return { mode: "manual", reason: "not_ready" };
  if (options.outlineLength <= 0) return { mode: "manual", reason: "empty_outline" };
  return { mode: "auto", reason: "ready" };
}

/** 阻断原因的人类可读摘要（回人工时的一句话说明，最多 4 类）。 */
export function describeAutoConfirmBlockers(signal: AutoConfirmSignal): string {
  if (!signal.blockers.length) return "目录未就绪";
  return signal.blockers
    .slice(0, 4)
    .map((b) => {
      const label = AUTO_CONFIRM_BLOCKER_LABELS[b.code] || b.code;
      return b.count > 1 ? `${label}×${b.count}` : label;
    })
    .join("、");
}

/** 倒计时毫秒 → 界面只读展示的秒数（非配置入口；最小 1 秒）。 */
export function countdownSeconds(ms: number): number {
  const v = Number.isFinite(ms) && ms > 0 ? ms : 8000;
  return Math.max(1, Math.round(v / 1000));
}
