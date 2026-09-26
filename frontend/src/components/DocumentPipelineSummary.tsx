/**
 * 「上传解析」Tab · 解析质量（四层存储）概要。
 *
 * 为什么需要它：后端 `services/doc_pipeline` + `routers/doc_pipeline.py` 已实现
 * 「原文层 / 解析层 / 提取层 / 语义层」四层产物落盘与
 * `GET /documents/{id}/status` 状态查询（含解析版本、页数、质量分、各层产物清单），
 * 前端 `api/index.ts` 的 `docPipelineApi` 已由本组件在「文档预览」弹窗内实际调用
 * （SchemeWorkbenchPage 的 `handlePreviewDocument` / `handlePipelineAction` /
 * `loadPipelineStatus`），不再是死代码 —— 用户在预览文档时可查看四层产物是否就绪、
 * 质量分多少（下游目录/事实提取的可信度依据）。
 *
 * 本组件是**受控展示 + 轻交互组件**：状态数据由页面（handlePreviewDocument）通过
 * docPipelineApi 拉取后传入；传入 `onAction` 时渲染「重新解析 / 物化提取层 /
 * 交叉校验 / 刷新质量」操作按钮，具体请求与刷新由页面处理，组件只负责禁用/loading
 * 呈现。渲染与交互正确性由 tests/DocumentPipelineSummary.test.tsx 钉住。
 */
import React from "react";
import { Alert, Button, Descriptions, Empty, Space, Spin, Tag, Tooltip, Typography } from "antd";
import {
  CheckCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

const { Text } = Typography;

export type DocumentPipelineStatusLike = {
  doc_id?: string;
  file_name?: string;
  parse_status?: string;
  parse_version?: string;
  parse_time?: string;
  parse_duration_ms?: number;
  page_count?: number;
  extract_status?: string;
  quality_score?: number | null;
  expires_at?: string;
  meta_on_disk?: boolean;
  /** 各层产物文件名清单：{ raw: [...], parsed: [...], extracted: [...], semantic: [...] } */
  layers?: Record<string, unknown>;
  [k: string]: unknown;
};

/** 四层管线可触发的动作类型 */
export type PipelineActionKey = "reparse" | "syncExtractions" | "crossCheck" | "refreshQuality";

export type DocumentPipelineSummaryProps = {
  /** 状态拉取中 */
  loading?: boolean;
  /** 状态数据；null 表示尚未取到（此时按空态渲染） */
  status?: DocumentPipelineStatusLike | null;
  /** 拉取失败原因（非阻断：预览正文仍可看） */
  error?: string | null;
  /**
   * 触发某项管线动作（重解析 / 物化提取层 / 交叉校验 / 刷新质量）。
   * 传入即渲染对应操作按钮；未传入的动作不渲染按钮（保持纯展示向后兼容）。
   * 返回 Promise 时按钮进入 loading，期间由 actionBusy 精确标记哪一个在跑。
   */
  onAction?: (action: PipelineActionKey) => void | Promise<void>;
  /** 当前正在执行的动作（对应按钮显示 loading，其余按钮禁用避免并发重入） */
  actionBusy?: PipelineActionKey | null;
};

/** 四层存储的展示定义（与后端 store.ALL_LAYERS 对齐） */
export const PIPELINE_LAYERS: Array<{ key: string; label: string; hint: string }> = [
  { key: "raw", label: "原文层", hint: "原件副本 + 文件指纹（MD5/SHA256）" },
  { key: "parsed", label: "解析层", hint: "Markdown 正文 + 页/表/图结构化产物" },
  { key: "extracted", label: "提取层", hint: "按类别的 AI 结构化提取结果（含溯源）" },
  { key: "semantic", label: "语义层", hint: "分块与语义索引（供目录/事实提取定位）" },
];

/**
 * 管线动作定义（顺序即按钮呈现顺序）。
 * - reparse：指纹变更/启用 OCR 后重建四层产物（POST /reparse）
 * - syncExtractions：把 AI 提取结果物化到提取层（POST /sync-extractions），仅解析成功后有意义
 * - crossCheck：多文档事实交叉校验（POST /cross-check）
 * - refreshQuality：重跑完整性校验并刷新质量分（GET /completeness?refresh=true）
 */
export const PIPELINE_ACTIONS: Array<{
  key: PipelineActionKey;
  label: string;
  hint: string;
  icon: React.ReactNode;
}> = [
  { key: "reparse", label: "重新解析", hint: "按文件指纹增量重解析，重建解析/分块/语义层产物", icon: <ReloadOutlined /> },
  { key: "syncExtractions", label: "物化提取层", hint: "把 AI 提取结果按标准格式落盘到提取层（可追溯）", icon: <SyncOutlined /> },
  { key: "crossCheck", label: "交叉校验", hint: "跑一致性规则，汇总多文档事实冲突", icon: <ThunderboltOutlined /> },
  { key: "refreshQuality", label: "刷新质量", hint: "重新计算解析/表格/图片/字段覆盖率与质量分", icon: <CheckCircleOutlined /> },
];

/**
 * 某动作是否应展示按钮。
 * 边界：syncExtractions 仅在解析成功后有意义（未解析时提取层无源可物化）。
 */
export function isActionAvailable(action: PipelineActionKey, status?: DocumentPipelineStatusLike | null): boolean {
  if (action === "syncExtractions") {
    return (status?.parse_status || "").toLowerCase() === "success";
  }
  return true;
}


/**
 * 某层的产物文件数。
 *
 * 边界：layers 缺失 / 非对象 / 该层不是数组 → 一律 0（不抛异常）。
 * 后端在文档尚未落盘该层时返回空数组，故 0 即「未产出」。
 */
export function layerFileCount(layers: unknown, key: string): number {
  if (!layers || typeof layers !== "object") return 0;
  const v = (layers as Record<string, unknown>)[key];
  return Array.isArray(v) ? v.length : 0;
}

/** 解析状态 → Tag 颜色（未知状态按中性色，不误报为成功） */
export function parseStatusColor(status?: string): string {
  const s = (status || "").trim().toLowerCase();
  if (s === "success" || s === "parsed" || s === "completed" || s === "valid") return "green";
  if (s === "failed" || s === "error") return "red";
  if (s === "pending" || s === "processing" || s === "running") return "orange";
  return "default";
}

/**
 * 质量分归一化为百分数。
 *
 * 后端 `quality_score` 是 0~1 的比值（`round(sum/len, 3)`），但历史脏数据、
 * 后续若改为百分制都可能出现 >1 的值 —— 故按「≤1 视为比值」兼容，
 * 无法识别（null/NaN/非数字）返回 null，UI 显式为「未评估」而不是「0%」。
 */
export function qualityPercent(score?: number | null): number | null {
  if (typeof score !== "number" || Number.isNaN(score)) return null;
  const pct = score <= 1 ? score * 100 : score;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

/** 质量分 → Tag 颜色（≥80 绿 / ≥60 橙 / 其余红；未评估灰） */
export function qualityColor(score?: number | null): string {
  const pct = qualityPercent(score);
  if (pct === null) return "default";
  if (pct >= 80) return "green";
  if (pct >= 60) return "orange";
  return "red";
}

/** 解析耗时毫秒 → 可读文案（0/缺失返回空串，不渲染占位） */
export function formatDurationMs(ms?: number): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return "";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export default function DocumentPipelineSummary({
  loading = false,
  status = null,
  error = null,
  onAction,
  actionBusy = null,
}: DocumentPipelineSummaryProps) {
  if (loading) {
    return (
      <Space size={8}>
        <Spin size="small" />
        <Text type="secondary" style={{ fontSize: 12 }}>正在读取解析质量...</Text>
      </Space>
    );
  }

  // 拉取失败：降级为一条非阻断提示，不影响正文预览
  if (error) {
    return (
      <Alert
        type="warning"
        showIcon
        message="解析质量信息读取失败"
        description={error}
        style={{ fontSize: 12 }}
      />
    );
  }

  if (!status) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无解析质量信息" />;
  }

  const statusColor = parseStatusColor(status.parse_status);
  const durationLabel = formatDurationMs(status.parse_duration_ms);
  const pct = qualityPercent(status.quality_score);

  return (
    <div>
      <Descriptions size="small" column={2} colon={false} style={{ marginBottom: 8 }}>
        <Descriptions.Item label="解析状态">
          <Tag color={statusColor}>{status.parse_status || "未知"}</Tag>
          {status.parse_version && <Tag>版本 {status.parse_version}</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="解析质量">
          {pct === null ? (
            <Tooltip title="尚未执行完整性校验（GET /documents/{id}/completeness）">
              <Tag color="default">未评估</Tag>
            </Tooltip>
          ) : (
            <Tag color={qualityColor(status.quality_score)}>{pct}%</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="页数">
          {status.page_count ? `${status.page_count} 页` : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="解析耗时">
          {durationLabel || "—"}
        </Descriptions.Item>
        <Descriptions.Item label="提取状态">
          <Tag color={parseStatusColor(status.extract_status)}>
            {status.extract_status || "pending"}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="有效期">
          {status.expires_at || "—"}
        </Descriptions.Item>
      </Descriptions>

      <div>
        <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>四层产物：</Text>
        <Space size={4} wrap>
          {PIPELINE_LAYERS.map((layer) => {
            const count = layerFileCount(status.layers, layer.key);
            return (
              <Tooltip key={layer.key} title={layer.hint}>
                <Tag color={count > 0 ? "blue" : "default"}>
                  {layer.label} {count}
                </Tag>
              </Tooltip>
            );
          })}
          {status.meta_on_disk === false && (
            <Tooltip title="磁盘 meta 缺失，四层产物可能尚未落盘（可重新解析重建）">
              <Tag color="volcano">⚠ meta 缺失</Tag>
            </Tooltip>
          )}
        </Space>
      </div>

      {onAction && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid #f0f0f0" }}>
          <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>管线操作：</Text>
          <Space size={4} wrap>
            {PIPELINE_ACTIONS.filter((a) => isActionAvailable(a.key, status)).map((a) => (
              <Tooltip key={a.key} title={a.hint}>
                <Button
                  size="small"
                  icon={a.icon}
                  loading={actionBusy === a.key}
                  disabled={!!actionBusy && actionBusy !== a.key}
                  onClick={() => onAction(a.key)}
                >
                  {a.label}
                </Button>
              </Tooltip>
            ))}
          </Space>
        </div>
      )}
    </div>
  );
}

