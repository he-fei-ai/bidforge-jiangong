import { Alert } from "antd";

export function FactsSegmentFailuresAlert({
  stats,
}: {
  stats: {
    ok?: number;
    failed?: number;
    total?: number;
    failed_details?: Array<{ index?: number; heading?: string; reason?: string }>;
    /** ✅ R179：模型声明「本段无事实」——不是失败，但同样会少提取内容，必须可见 */
    declared_failed?: number;
    declared_failed_details?: Array<{ index?: number; heading?: string; reason?: string }>;
  } | null;
}) {
  const s = stats || {};
  const failed = Number(s.failed) || 0;
  const total = Number(s.total) || 0;
  const declared = Number(s.declared_failed) || 0;
  const allFailed = failed > 0 && failed === total;
  const details = Array.isArray(s.failed_details) ? s.failed_details : [];
  const declaredDetails = Array.isArray(s.declared_failed_details)
    ? s.declared_failed_details
    : [];
  // ✅ R179：仅 declared_failed>0（failed=0）时不再静默 —— 此前该提示只挂在
  //    failed>0 条件上，模型声明「无事实」的段既不显示失败明细也不显示原因，
  //    用户只看到 ok 数变少，无法判断是失败还是空段。
  const onlyDeclared = failed <= 0 && declared > 0;
  const message = allFailed
    ? `本次提取全部 ${total} 段失败，未提取到事实`
    : failed > 0
      ? `本次提取有 ${failed}/${total} 段失败，结果可能不完整`
      : `本次提取有 ${declared}/${total} 段模型未提取到事实（不是失败）`;
  return (
    <Alert
      type={allFailed ? "error" : failed > 0 ? "warning" : "info"}
      showIcon
      style={{ marginBottom: 12 }}
      message={message}
      description={
        <div style={{ fontSize: 12 }}>
          {details.slice(0, 6).map((d, i) => (
            <div key={i}>
              · 第 {d.index ?? i + 1} 段（{d.heading || "未命名"}）：{d.reason || "未知原因"}
            </div>
          ))}
          {details.length > 6 && <div>· ……其余 {details.length - 6} 条明细略</div>}
          {declaredDetails.slice(0, 6).map((d, i) => (
            <div key={`declared-${i}`}>
              · 第 {d.index ?? i + 1} 段（{d.heading || "未命名"}）：模型声明该段无事实
            </div>
          ))}
          {onlyDeclared && (
            <div>这些段落不是提取失败；下次增量提取会自动重试，如确认确无事实可忽略。</div>
          )}
          {failed > 0 && details.length === 0 && (
            <div>未获取到失败明细，建议稍后点击「③ AI 提取事实」重新提取。</div>
          )}
          {failed > 0 && <div>常见原因：模型超时/限流、网络抖动、单段内容过长。</div>}
        </div>
      }
    />
  );
}

// ============================================================
// 全局事实 · 模块级可测纯函数（2026-09-21 拆出，行为由 factsTab.test 钉住）
// ============================================================
/**
 * 把后端 list 接口返回的 stats 收敛为前端事实 Tab 使用的 summary 对象。
 *
 * has_warnings = 模拟值 / 未确认 / 矛盾 / **来源过期** 任一 > 0，驱动「全部就绪」入口与徽标颜色。
 * ✅ 抽纯函数（2026-09-21）：loadFacts 此前内联算 has_warnings，与切方案重置
 *    setFactsSummary(null) 分散两处，口径漂移难以测试。
 *
 * ✅ BUG 修复（2026-09-27 · 过期事实被误判为「全部就绪」）：
 *   后端 `list_facts` 的 stats 早已回传 `stale`（来源资料已变化 / 重新提取后
 *   取代的旧值），且这些行已被注入门控 `... AND is_stale=0` 排除在正文与导出之外；
 *   但本函数只读 simulated / unresolved / conflicts，**stale 从不参与判定**。
 *   后果：项目资料变更后事实被整体标记过期，界面却仍显示绿色「全部就绪」并
 *   放行「下一步：正文生成」—— 用户以为事实可用，实际正文里一条事实都注入不到
 *   （静默失效，且方向是「看起来正常、实际数据全丢」，比报错更难发现）。
 *   口径与后端 `_FACTS_INJECT_WHERE` 四个门控条件对齐：任何一条不满足注入条件
 *   的事实都必须计入告警，不能让用户误以为可以进入下一步。
 */
