import { type GenStats, type SectionLogItem } from "../../utils/contentEvents";
import { SectionLogList } from "./SectionLogList.tsx";
import { type OutlineLogItem } from "./logTypes.ts";
import { STAGE_COLOR } from "./outlineTreeModel.ts";
import { fmtDuration } from "./statsFormat.ts";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  FileTextOutlined,
  HourglassOutlined,
  LoadingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Card, List, Progress, Tag, Alert } from "antd";
import { memo, useMemo } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export type GenerationProgressProps = {
  genType: string;
  progress: number;
  progressMsg: string;
  sectionLogs: SectionLogItem[];
  outlinePhase: string;
  outlineDoneTotal: OutlineLogItem | null;
  outlineLogs: OutlineLogItem[];
  /** ✅ 进度增强：后端下发的运行统计（已耗时 / ETA / 累计字数 / 并发 / 进行中章节） */
  stats?: GenStats;
  /** ✅ 可感知重连态：SSE 断线后正在重新挂接后台任务 */
  reconnecting?: boolean;
  /** ✅ R227（修复10）：任务已达终态（completed/degraded/stopped/failed）。
   *  正文生成终态事件把 progress 置 1（completed/degraded），但 stopped 保留真实
   *  进度（<1）。原 `hasFailed = failedShown>0 && progress>=1` 在 stopped+有失败时
   *  恒假 —— 进度条不显示 exception（不对称：completed 显示、stopped 不显示）。
   *  缺省 false（旧行为），由调用方在终态事件处置时置 true。 */
  terminal?: boolean;
};

/** ✅ 实时日志面板的最大渲染行数：仅限制渲染，数据源保持全量。
 *  长方案分步生成（数十章 × 多阶段）日志可达上千条，而这个滚动区仅 280px 高，
 *  全量渲染会造成严重掉帧；截断到最近若干条对用户可见内容无实质影响。 */
export const OUTLINE_LOG_RENDER_MAX = 200;

export const GenerationProgressCard = memo(function GenerationProgressCard(
  p: GenerationProgressProps,
) {
  const {
    genType,
    progress,
    progressMsg,
    sectionLogs,
    outlinePhase,
    outlineDoneTotal,
    outlineLogs,
    stats,
    reconnecting,
  } = p;
  // ✅ 性能优化：原实现把 `[...outlineLogs].reverse()` 写在 JSX 里 —— SSE 期间
  //    progress 与日志每条更新都触发重渲，也就意味着每次都全量复制一遍整个数组，
  //    同时把不断增加的日志全部渲染成 List.Item。改为按 outlineLogs 记忆化，
  //    并对渲染行数设上限（数据源 outlineLogs 本身不动，其它派生产物如
  //    outlineDoneTotal 仍需读取完整历史，行为保持不变）。
  const outlineLogItems = useMemo(
    () =>
      outlineLogs.length > OUTLINE_LOG_RENDER_MAX
        ? outlineLogs.slice(-OUTLINE_LOG_RENDER_MAX).reverse()
        : [...outlineLogs].reverse(),
    [outlineLogs],
  );
  const isContent = genType === "content";
  const isOutline = genType === "outline";
  // ✅ 性能优化：正文生成期间 sectionLogs 随每条 SSE 更新，原实现的 3 次独立
  //    filter 等同把整份日志遍历 3 遍（长方案可达数百章）。合并为单遍扫描 +
  //    按 sectionLogs 记忆化，非本字段变化（如 progress）不再重算。
  const { runningLogs, successCount, failedCount } = useMemo(() => {
    const running: SectionLogItem[] = [];
    let success = 0;
    let failed = 0;
    for (const l of sectionLogs) {
      if (l.status === "running") running.push(l);
      else if (l.status === "success") success += 1;
      else if (l.status === "failed") failed += 1;
    }
    return { runningLogs: running, successCount: success, failedCount: failed };
  }, [sectionLogs]);
  // ✅ 进度增强：优先采用后端权威统计（含未进入日志的失败章），缺失时回退本地计数。
  //    ✅ R175 BUG-4：stats.done 现为**成功**章节数（失败另计 failed），
  //    fallback 同步改为 successCount（旧实现 success+failed 会与后端口径打架）。
  const doneCount = typeof stats?.done === "number" ? stats.done : successCount;
  const totalCount = stats?.total ?? sectionLogs[0]?.total ?? 0;
  const elapsedMs = stats?.elapsed_ms ?? null;
  const etaMs = stats?.eta_ms ?? null;
  const words = stats?.words ?? 0;
  const concurrency = stats?.concurrency ?? 0;
  const nodes = stats?.nodes ?? 0;
  const avgSectionMs = stats?.avg_section_ms ?? null;
  // ✅ 二次增强：失败数取「后端统计 / 本地日志」的较大值 —— 强制重写失败等场景
  //    章节可能不在日志里（保留旧正文、状态未改），只看日志会把失败数显示为 0，
  //    与完成提示的「N 章失败」自相矛盾。目录与正文模式同口径。
  const failedShown = Math.max(failedCount, typeof stats?.failed === "number" ? stats.failed : 0);
  // 目录生成分步链路才有章节维度（done/total = 已生成子目录的章数 / 总章数）
  const outlineStepwise = isOutline && !!stats?.stepwise;
  // ✅ R227（修复10）：stopped 终态 progress 保留真实值（<1），`progress >= 1`
  //    恒假 ⇒ 有失败也不显示 exception。终态信号与 completed/degraded 的
  //    `progress >= 1` 并集，消除不对称（default 旧行为，未接终态时不变）。
  const hasFailed = failedShown > 0 && (progress >= 1 || p.terminal === true);
  return (
    <Card
      size="small"
      style={{ marginBottom: 12, flexShrink: 0 }}
      styles={{ body: { padding: 12 } }}
    >
      <div style={{ display: "flex", gap: 16 }}>
        {/* ---- 左侧：任务标题 + 横向进度条 + 运行统计 + 当前工作内容 ---- */}
        <div style={{ flex: "0 0 420px", display: "flex", flexDirection: "column", gap: 8 }}>
          {reconnecting && (
            <Alert
              type="warning"
              showIcon
              message="连接已中断，正在重新挂接后台任务"
              description="后台任务可能仍在运行（AI 计费继续），请勿重复发起生成；恢复后本页会自动同步进度。"
              style={{ padding: "6px 10px", fontSize: 12 }}
            />
          )}
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 8,
            }}
          >
            <Text strong style={{ fontSize: 13 }}>
              {genType === "outline"
                ? "📂 正在生成目录"
                : isContent
                  ? "📝 正在生成正文"
                  : "⏳ 正在处理"}
            </Text>
            {isContent && totalCount > 0 && (
              <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                已完成 {doneCount}/{totalCount}
              </Text>
            )}
            {outlineStepwise && totalCount > 0 && (
              <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                已完成 {doneCount}/{totalCount} 章
              </Text>
            )}
          </div>
          <Progress
            percent={Math.round(progress * 100)}
            status={hasFailed ? "exception" : "active"}
          />
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 8,
            }}
          >
            <Text
              style={{ fontSize: 12, color: "var(--bp-text-secondary)", flex: 1, minWidth: 0 }}
              ellipsis
              title={progressMsg}
            >
              {progressMsg}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
              {Math.round(progress * 100)}%
            </Text>
          </div>

          {/* ✅ 进度增强：运行态指标（已耗时 / 预计剩余 / 累计字数 / 并发档位 / 目录节点数）
              目录生成同样展示已耗时与预计剩余 —— 审核+修复最长 180s，没有时间反馈时
              用户无法判断是「在跑」还是「卡死」。 */}
          {(isContent || isOutline) && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {elapsedMs ? (
                <Tag icon={<ClockCircleOutlined />} style={{ margin: 0 }}>
                  已耗时 {fmtDuration(elapsedMs)}
                </Tag>
              ) : null}
              {etaMs ? (
                <Tag icon={<HourglassOutlined />} color="blue" style={{ margin: 0 }}>
                  预计剩余 {fmtDuration(etaMs)}
                </Tag>
              ) : null}
              {isContent && words > 0 ? (
                <Tag icon={<FileTextOutlined />} style={{ margin: 0 }}>
                  已生成 {words.toLocaleString()} 字
                </Tag>
              ) : null}
              {/* ✅ 二次增强：平均单章耗时 —— 用户据此判断「还要等多久」「是不是变慢了」 */}
              {isContent && avgSectionMs ? (
                <Tag style={{ margin: 0 }} title="已完成章节的平均实测耗时">
                  平均单章 {fmtDuration(avgSectionMs)}
                </Tag>
              ) : null}
              {(isContent || isOutline) && failedShown > 0 ? (
                <Tag color="error" style={{ margin: 0 }}>
                  失败 {failedShown}
                </Tag>
              ) : null}
              {isOutline && nodes > 0 ? (
                <Tag icon={<FileTextOutlined />} style={{ margin: 0 }}>
                  目录 {nodes} 个节点
                </Tag>
              ) : null}
              {isContent && concurrency > 0 ? (
                <Tag icon={<ThunderboltOutlined />} style={{ margin: 0 }}>
                  并发 {concurrency}
                </Tag>
              ) : null}
            </div>
          )}

          {/* ✅ 进度增强：当前正在生成的章节（章节名 + 阶段），让用户知道"卡在哪一步" */}
          {isContent && runningLogs.length > 0 && (
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
              {runningLogs.slice(0, 4).map((l) => (
                <Tag
                  key={l.section_id}
                  color={STAGE_COLOR[l.stage || "draft"] || "processing"}
                  style={{ margin: 0, maxWidth: 200, overflow: "hidden" }}
                  title={`${l.title}${l.stage_label ? " · " + l.stage_label : ""}`}
                >
                  <span style={{ fontSize: 11 }}>
                    {typeof l.index === "number" ? `${l.index}. ` : ""}
                    {l.title}
                    {l.stage_label ? ` · ${l.stage_label}` : ""}
                  </span>
                </Tag>
              ))}
              {runningLogs.length > 4 && (
                <Tag style={{ margin: 0, fontSize: 11 }}>+{runningLogs.length - 4}</Tag>
              )}
            </div>
          )}

          {/* 统计色块：正文=章节统计；目录=阶段 + 已完成章节 */}
          {genType === "outline" ? (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Tag color="processing" style={{ margin: 0 }}>
                <LoadingOutlined /> {outlinePhase}
              </Tag>
              {/* 后端 stats 未到时（连接初期/旧任务）回退到日志推断的章节计数 */}
              {!outlineStepwise && outlineDoneTotal && (
                <Tag color="blue" style={{ margin: 0 }}>
                  章节 {outlineDoneTotal.done}/{outlineDoneTotal.total}
                </Tag>
              )}
            </div>
          ) : (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Tag color="processing" style={{ margin: 0 }}>
                <LoadingOutlined /> {runningLogs.length} 进行中
              </Tag>
              <Tag color="success" style={{ margin: 0 }}>
                <CheckCircleOutlined /> {successCount} 成功
              </Tag>
              <Tag color="error" style={{ margin: 0 }}>
                <CloseCircleOutlined /> {failedShown} 失败
              </Tag>
            </div>
          )}
        </div>

        {/* ---- 右侧：正文生成章节滚动日志 ---- */}
        {genType === "content" && (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              maxHeight: 280,
              overflow: "auto",
              border: "1px solid var(--bp-border)",
              borderRadius: 6,
              padding: "6px 10px",
              background: "var(--bp-panel-bg)",
            }}
          >
            {sectionLogs.length === 0 ? (
              <div style={{ textAlign: "center", padding: 12, color: "var(--bp-text-tertiary)" }}>
                正在连接 AI，等待章节生成...
              </div>
            ) : (
              <SectionLogList items={sectionLogs} />
            )}
          </div>
        )}

        {/* ---- 右侧：目录生成实时日志 ---- */}
        {genType === "outline" && (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              maxHeight: 280,
              overflow: "auto",
              border: "1px solid var(--bp-border)",
              borderRadius: 6,
              padding: "6px 10px",
              background: "var(--bp-panel-bg)",
            }}
          >
            {outlineLogs.length === 0 ? (
              <div style={{ textAlign: "center", padding: 12, color: "var(--bp-text-tertiary)" }}>
                正在连接 AI，准备生成目录...
              </div>
            ) : (
              <List
                size="small"
                dataSource={outlineLogItems}
                // ✅ 性能优化：反转后若沿用默认的 index key，每新增一条日志都会让
                //    所有行的 key 顺移一位 → 全量 reconcile。改为按内容派生稳定 key。
                rowKey={(item: OutlineLogItem) => `${item.time}-${item.message}`}
                renderItem={(item) => (
                  <List.Item
                    style={{
                      padding: "4px 8px",
                      borderRadius: 4,
                      marginBottom: 4,
                      background: "rgba(22,119,255,0.05)",
                    }}
                  >
                    <div style={{ width: "100%", display: "flex", alignItems: "center", gap: 8 }}>
                      <Tag color="blue" style={{ margin: 0, minWidth: 46, textAlign: "center" }}>
                        {Math.round(item.progress * 100)}%
                      </Tag>
                      <Text style={{ flex: 1, fontSize: 13 }} ellipsis title={item.message}>
                        {item.message}
                      </Text>
                      {/* 仅分步链路（total>0）有章节维度，防历史日志渲染「0/0 章」 */}
                      {typeof item.done === "number" &&
                        typeof item.total === "number" &&
                        item.total > 0 && (
                          <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                            {item.done}/{item.total} 章
                          </Text>
                        )}
                    </div>
                  </List.Item>
                )}
              />
            )}
          </div>
        )}
      </div>
    </Card>
  );
});

// ✅ P0-5 组件拆分：左侧目录树面板独立 memo 组件——生成期间进度/日志更新
//    不再带动树与右侧 Tab 重渲；回调均以稳定引用传入
