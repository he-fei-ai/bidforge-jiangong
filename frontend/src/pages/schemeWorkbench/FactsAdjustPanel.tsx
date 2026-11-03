import { FACTS_ADJUST_PREVIEW_LIMIT, factsAdjustIgnoreText, formatFactsAdjustOperation, normalizeFactsAdjustPlan, type FactsAdjustIgnoredItem, type FactsAdjustOperation, type FactsAdjustPlanShape } from "./factsShared.ts";
import { ThunderboltOutlined } from "@ant-design/icons";
import { Alert, App, Button, Input, Modal } from "antd";
import { useEffect, useRef, useState } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export function FactsAdjustPanel({
  schemeId,
  disabled,
  busy,
  preview,
  apply,
  onError,
  confirmDialog = Modal.confirm,
}: {
  /** 当前方案 id；为空时面板完全禁用（避免向空方案发起请求） */
  schemeId?: string;
  /** 外部置灰（如正在提取事实 / 上传资料 / 方案尚无事实） */
  disabled?: boolean;
  /** 外部忙碌（与自身 adjusting 合并，防双击重复提交） */
  busy?: boolean;
  /** 预览：只生成待确认计划，不落库（后端 apply=false） */
  preview: (instruction: string) => Promise<Partial<FactsAdjustPlanShape>>;
  /** 应用：把用户刚确认的计划一次性写入（后端 apply=true + operations）
   *
   * ✅ 2026-10-08：返回类型放宽为「可回传后端响应」。页面原先返回 void，
   * 组件拿不到 `ignored` —— 而「要求改 3 条只落 2 条」恰恰只在应用后才知道。
   * 返回 void 仍然合法（旧接线零改动），此时按「无丢弃」处理。
   */
  apply: (operations: FactsAdjustOperation[], instruction: string) =>
    Promise<Partial<FactsAdjustPlanShape> | void>;
  /** 业务失败回调（透传后端 detail），缺省时静默降级 */
  onError?: (fallback: string, e: unknown) => void;
  /**
   * 确认弹窗工厂。默认用 antd 静态 `Modal.confirm`；
   * 页面可注入 `App.useApp().modal` 以继承 ConfigProvider 主题。
   * ⚠️ 注入是为了组件级可测 —— 静态方法在测试里也可用，但注入后
   * 用例可以用 mock 直接断言「确认了什么、调用了什么」，无需触碰 DOM。
   */
  confirmDialog?: (
    options: Parameters<typeof Modal.confirm>[0],
  ) => void;
}) {
  const [ask, setAsk] = useState(false);
  const [text, setText] = useState("");
  const [adjusting, setAdjusting] = useState(false);
  const [plan, setPlan] = useState<FactsAdjustPlanShape | null>(null);
  // ✅ 2026-10-08（F3 前端接线）：应用后才知道的丢弃明细（计划已清空，故独立持有）
  const [applyNotice, setApplyNotice] = useState<FactsAdjustIgnoredItem[] | null>(null);
  // 请求期间若已切换方案，丢弃结果（防止旧方案的计划落到新方案上）
  const schemeRef = useRef(schemeId);
  schemeRef.current = schemeId;

  // ⚠️ 切换方案 → 丢弃旧方案的全部中间态。与页面此前的
  // setFactsAdjustPlan(null) 同口径，只是归属从页面挪到了组件内。
  useEffect(() => {
    setPlan(null);
    setApplyNotice(null);
    setAsk(false);
    setAdjusting(false);
    setText("");
  }, [schemeId]);

  const busyNow = Boolean(busy) || adjusting;
  const ready = Boolean(schemeId) && !disabled && !busyNow;
  const operations = Array.isArray(plan?.operations) ? plan.operations : [];
  // ⚠️ 先绑定再判数组：`Array.isArray(plan?.ignored)` 不会把 `plan` 收窄成非空，
  //    直接写 `plan.ignored` 在 strict 下报「possibly undefined」。
  const _ignoredRaw = plan?.ignored;
  const _applyRaw = applyNotice;
  const ignoredPlan: FactsAdjustIgnoredItem[] = Array.isArray(_ignoredRaw) ? _ignoredRaw : [];
  const ignoredApply: FactsAdjustIgnoredItem[] = Array.isArray(_applyRaw) ? _applyRaw : [];
  const ignoredShown = ignoredApply.length > 0 ? ignoredApply : ignoredPlan;

  const submitError = (fallback: string, e: unknown) => {
    onError?.(fallback, e);
  };

  const confirmAsk = async () => {
    const instruction = text.trim();
    setAsk(false);
    if (!schemeId || !instruction) return;   // 空指令不下发
    setAdjusting(true);
    try {
      const data = await preview(instruction);
      if (schemeRef.current !== schemeId) return;   // 已切方案 → 丢弃
      setPlan(normalizeFactsAdjustPlan(data));
      setApplyNotice(null);
    } catch (e) {
      submitError("生成调整计划失败", e);
    } finally {
      setAdjusting(false);
    }
  };

  const requestApply = () => {
    if (!ready || operations.length === 0) return;
    confirmDialog({
      title: "确认应用事实调整",
      content: (
        <div>
          <div>{plan?.summary || `将执行 ${operations.length} 项调整`}</div>
          <ul>
            {operations.slice(0, FACTS_ADJUST_PREVIEW_LIMIT).map((op, i) => (
              <li key={`${op.op}-${op.fact_id || i}`}>
                {formatFactsAdjustOperation(op, i)}
              </li>
            ))}
          </ul>
          {operations.length > FACTS_ADJUST_PREVIEW_LIMIT && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              其余 {operations.length - FACTS_ADJUST_PREVIEW_LIMIT} 项未列出
            </Text>
          )}
          {ignoredPlan.length > 0 && (
            <Text type="danger" style={{ fontSize: 12 }}>
              另有 {ignoredPlan.length} 项因安全校验未纳入计划（见下方提示）
            </Text>
          )}
        </div>
      ),
      okText: "确认应用",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const res = await apply(
            operations,
            plan?.summary || `按已确认计划调整（${operations.length} 项）`,
          );
          // 应用后后端才回传「哪些操作被安全校验丢弃」；返回 void 的旧接线
          // 归一为空数组 → 与收口前逐字一致（不弹提示）。
          const norm = normalizeFactsAdjustPlan(res);
          setApplyNotice(norm.ignored && norm.ignored.length ? norm.ignored : null);
          setPlan(null);
        } catch (e) {
          submitError("应用调整失败", e);
          throw e;   // 抛出 → 弹窗保持打开，用户可重试或取消
        }
      },
    });
  };

  return (
    <>
      <Button
        icon={<ThunderboltOutlined />}
        onClick={() => { setText(""); setAsk(true); }}
        loading={adjusting}
        disabled={!ready}
      >
        AI 调整事实
      </Button>
      {operations.length > 0 && (
        <Button danger onClick={requestApply} disabled={!ready}>
          应用调整计划{operations.length > FACTS_ADJUST_PREVIEW_LIMIT
            ? `（${operations.length} 项）` : ""}
        </Button>
      )}
      {/*
        ✅ 2026-10-08（全局事实模块收口 · F3 前端接线）：被安全校验丢弃的操作必须可见。
        后端旧实现「AI 给了 3 条、实际只落 2 条」完全静默（响应只回 applied 计数），
        用户无法区分「AI 没产出」与「产出了但被拦下」—— 与 R48 图表删除可观测性同族。
        预览阶段的丢弃来自 `plan.ignored`，应用阶段的丢弃来自 `applyNotice`；
        旧版后端两个都没有 → 本块整体不渲染，界面与收口前逐字一致。
      */}
      {ignoredShown.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message={`${ignoredShown.length} 项调整未纳入（${ignoredApply.length > 0 ? "应用时" : "预览时"}被安全校验丢弃）`}
          description={(
            <div style={{ fontSize: 12 }}>
              {ignoredShown.slice(0, 6).map((it, i) => (
                <div key={i}>· {factsAdjustIgnoreText(it)}</div>
              ))}
              {ignoredShown.length > 6 && (
                <div>· ……其余 {ignoredShown.length - 6} 条明细略</div>
              )}
            </div>
          )}
        />
      )}
      <Modal
        title="AI 调整事实"
        open={ask}
        okText="生成调整计划"
        cancelText="取消"
        onOk={confirmAsk}
        onCancel={() => setAsk(false)}
        okButtonProps={{ disabled: !text.trim() || busyNow, loading: adjusting }}
        maskClosable={false}
        destroyOnClose
      >
        <div style={{ marginBottom: 10, color: "#666", lineHeight: 1.7 }}>
          用一句自然语言描述调整要求。仅生成<b>待确认</b>的调整计划，
          不会直接修改事实；确认后再一次性应用。
        </div>
        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="例如：新增一条「项目经理=张伟」；把开挖深度统一改为 8.5m"
          rows={4}
          maxLength={500}
          showCount
          autoFocus
          onPressEnter={(e) => {
            // Shift+Enter 换行；单独 Enter 才提交（与输入习惯一致）
            if (!e.shiftKey && text.trim()) {
              e.preventDefault();
              confirmAsk();
            }
          }}
        />
      </Modal>
    </>
  );
}

/** 分段失败告警：部分失败 warning / 全部失败 error，明细最多 6 条 */
