/**
 * ✅ R179 N1（2026-11-05）：目录生成自动确认（开关化、默认人工）。
 *
 * 关键约束（方案 §3.4 / §4.3）：
 *   - 开关关闭（默认）时 `handleOutlineReady` **逐字委托** `openOutlineGate` ——
 *     不开 Modal、不读倒计时、不写快照，人工路径一行行为不变（有等价性用例）。
 *   - 仅在「开关开 && 后端 ready && 无已有正文 && 目录非空」时 arm 倒计时。
 *   - 任何交互（点击/键盘/滚动）、切换页面（失焦/页面隐藏）、保存报错/取消
 *     都**立即取消并回人工**（不是暂停后继续）。
 *   - 倒计时时长由后端下发（`OUTLINE_AUTO_CONFIRM_DELAY_MS`，默认 8000ms），
 *     前端只读展示，**不提供任何修改入口**。
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Modal, Space, Typography } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { OutlineGateList } from "./OutlineGateList.tsx";
import type { OutlineNodeInput } from "./workbenchTypes";
import {
  countdownSeconds,
  decideAutoConfirm,
  describeAutoConfirmBlockers,
  parseAutoConfirm,
  type AutoConfirmSignal,
} from "./outlineAutoConfirm";

const { Text } = Typography;

export interface OutlineAutoConfirmDeps {
  id: string | undefined;
  /** 治理开关（默认 false）。false 时完全走人工闸门。 */
  enabled: boolean;
  /** 后端下发的倒计时毫秒（默认 8000；运维经环境变量改，前端零配置入口）。 */
  delayMs: number;
  /** 读取「当前目录树是否已存在正文」——每次现读，避免闭包陈旧。 */
  hasExistingContent: () => boolean;
  /** 人工闸门（`useOutlineGate.openOutlineGate`）——开关关闭时逐字委托。 */
  openOutlineGate: (outline: OutlineNodeInput[]) => void;
  /** 自动保存（页面实现，与人工保存同一出口，仅多传 `snapshot_before`）；返回是否成功。 */
  saveAuto: (outline: OutlineNodeInput[], snapshotBefore: string) => Promise<boolean>;
  /** 风险条「切回人工」：PUT false 并刷新开关状态。 */
  onSwitchToManual: () => void | Promise<void>;
  msg: MessageInstance;
}

interface ArmedState {
  outline: OutlineNodeInput[];
  deadline: number;
  totalMs: number;
  signal: AutoConfirmSignal;
}

export function useOutlineAutoConfirm(deps: OutlineAutoConfirmDeps) {
  const depsRef = useRef(deps);
  depsRef.current = deps;

  const [armed, setArmed] = useState<ArmedState | null>(null);
  const armedRef = useRef<ArmedState | null>(null);
  const [remainingMs, setRemainingMs] = useState(0);
  const [saving, setSaving] = useState(false);
  const selectedRef = useRef<number[]>([]);

  const clearArmed = () => {
    armedRef.current = null;
    setArmed(null);
    setRemainingMs(0);
  };

  /** 自动保存：成功 = 结束；失败 = 报错并立刻回人工确认（绝不静默）。 */
  const commitAuto = async () => {
    const a = armedRef.current;
    if (!a) return;
    clearArmed();
    setSaving(true);
    try {
      const ok = await depsRef.current.saveAuto(a.outline, "outline_auto_confirm");
      if (!ok) {
        depsRef.current.msg.warning("自动确认保存失败，已切回人工确认");
        depsRef.current.openOutlineGate(a.outline);
      }
    } finally {
      setSaving(false);
    }
  };

  /** 所有打断路径的唯一出口：清计时器 + 回人工闸门。 */
  const cancelAuto = (reason: string) => {
    const a = armedRef.current;
    if (!a) return;
    clearArmed();
    depsRef.current.msg.info(`已取消自动确认（${reason}），转为人工确认`);
    depsRef.current.openOutlineGate(a.outline);
  };

  /** 三处完成路径（在线 completed / 断线重挂 / 刷新接管）的统一入口。 */
  const handleOutlineReady = (outline: OutlineNodeInput[], rawSignal?: unknown) => {
    const d = depsRef.current;
    if (!d.id) return;
    const signal = parseAutoConfirm(rawSignal);
    const decision = decideAutoConfirm(d.enabled, signal, {
      hasContent: d.hasExistingContent(),
      outlineLength: Array.isArray(outline) ? outline.length : 0,
    });
    if (decision.mode === "manual") {
      // ✅ 开关关闭（默认）时**不进入这段**：直接委托，人工路径一字不变。
      if (d.enabled && decision.reason !== "switch_off") {
        d.msg.info(`自动确认未启动（${describeAutoConfirmBlockers(signal)}），已转为人工确认`);
      }
      d.openOutlineGate(outline);
      return;
    }
    const totalMs = Number.isFinite(d.delayMs) && d.delayMs > 0 ? d.delayMs : 8000;
    const next: ArmedState = {
      outline,
      deadline: Date.now() + totalMs,
      totalMs,
      signal,
    };
    armedRef.current = next;
    setArmed(next);
    setRemainingMs(totalMs);
    d.msg.info(`自动确认已启动：${countdownSeconds(totalMs)} 秒内无操作将自动保存并锁定一级章节`);
  };

  // 倒计时：100ms 一档（剩余秒数只读展示；到期自动保存）
  const handlersRef = useRef({ commitAuto, cancelAuto });
  handlersRef.current = { commitAuto, cancelAuto };

  useEffect(() => {
    if (!armed) return;
    const tick = () => {
      const cur = armedRef.current;
      if (!cur) return;
      const left = Math.max(0, cur.deadline - Date.now());
      setRemainingMs(left);
      if (left <= 0) void handlersRef.current.commitAuto();
    };
    const timer = window.setInterval(tick, 100);
    return () => window.clearInterval(timer);
  }, [armed]);

  // 任何交互 / 切换页面 → 立即取消并回人工
  useEffect(() => {
    if (!armed) return;
    const onInteract = (e: Event) => {
      if (!armedRef.current) return;
      const t = e.target as Element | null;
      // 计时 Modal 自身的操作按钮由点击处理器显式执行，避免再次触发取消
      if (t && typeof t.closest === "function" && t.closest("[data-auto-confirm-action]")) {
        return;
      }
      handlersRef.current.cancelAuto("用户交互");
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") handlersRef.current.cancelAuto("页面隐藏");
    };
    const onLeave = () => handlersRef.current.cancelAuto("切换页面");
    window.addEventListener("pointerdown", onInteract, true);
    window.addEventListener("keydown", onInteract, true);
    window.addEventListener("wheel", onInteract, { capture: true, passive: true });
    window.addEventListener("blur", onLeave);
    window.addEventListener("pagehide", onLeave);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pointerdown", onInteract, true);
      window.removeEventListener("keydown", onInteract, true);
      window.removeEventListener("wheel", onInteract, true);
      window.removeEventListener("blur", onLeave);
      window.removeEventListener("pagehide", onLeave);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [armed]);

  // 卸载（切页/关页）时清掉 ref，防止残留计时器触发保存
  useEffect(
    () => () => {
      armedRef.current = null;
    },
    [],
  );

  // arm 时勾选集默认全选（自动确认使用全选集合，与人工默认一致）
  useEffect(() => {
    selectedRef.current = (armed?.outline || []).map((_, i) => i);
  }, [armed]);

  const seconds = countdownSeconds(armed?.totalMs || deps.delayMs);

  const modal: ReactNode = (
    <Modal
      open={!!armed}
      title="自动确认目录（倒计时中）"
      width={660}
      centered
      closable={false}
      maskClosable={false}
      keyboard={false}
      onCancel={() => cancelAuto("用户取消")}
      footer={
        <Space>
          <span data-auto-confirm-action="cancel">
            <Button onClick={() => cancelAuto("用户取消")} disabled={saving}>
              取消自动确认
            </Button>
          </span>
          <span data-auto-confirm-action="confirm">
            <Button type="primary" loading={saving} onClick={() => void commitAuto()}>
              立即确认保存
            </Button>
          </span>
        </Space>
      }
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message={`${Math.ceil(remainingMs / 1000)} 秒后自动保存并锁定一级章节`}
        description="倒计时期间任何点击、键盘、滚动、切换页面或报错都会立即取消并转为人工确认。"
      />
      <Text type="secondary">
        目录校验通过（无阻断项）；本次将保存全部一级章节。如需调整，请取消后在目录树中编辑。
      </Text>
      {armed ? (
        <div style={{ marginTop: 12 }}>
          <OutlineGateList outline={armed.outline} selectedRef={selectedRef} />
        </div>
      ) : null}
    </Modal>
  );

  const banner: ReactNode = deps.enabled ? (
    <div data-testid="outline-auto-confirm-banner" style={{ marginBottom: 12 }}>
      <Alert
        type="warning"
        showIcon
        message={`自动确认已开启：目录生成完成后 ${countdownSeconds(deps.delayMs)} 秒无操作将自动保存并锁定一级章节`}
        description="风险：自动确认会按 AI 新目录整表重建章节结构（已有正文的项目永不自动确认）；任何交互或报错都会立即切回人工确认。"
        action={
          <Button size="small" onClick={() => void deps.onSwitchToManual()}>
            切回人工
          </Button>
        }
      />
    </div>
  ) : null;

  return { handleOutlineReady, cancelAuto, modal, banner, armed: !!armed, remainingMs, seconds };
}
