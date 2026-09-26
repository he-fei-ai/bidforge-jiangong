/**
 * 「上传解析」Tab · 资料与解析列表（组件级抽取）。
 *
 * 为什么抽出来：这段列表是「上传解析」交互最密集的部分
 * （单份解析 / 重新解析 / 预览 / 删除 / 改分类 + 「解析中」态），原先内联在
 * 8000 行的 SchemeWorkbenchPage 里，既无法单测、也容易被误改。
 * 抽成**受控组件**后，交互正确性由 tests/documentParseList.test.tsx 钉住，
 * 页面只负责提供数据与回调，不再关心列表如何渲染。
 */
import React, { useState } from "react";
import { Button, Empty, List, Select, Tag, Tooltip } from "antd";
import {
  CloseCircleOutlined,
  EyeOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { isDocParsed, isDocFailed } from "../utils/workflowDerived";

/** 列表项（后端 /documents 返回的子集，保留索引签名以兼容后端新增字段） */
export type DocumentParseItem = {
  id: string;
  file_name: string;
  text_len?: number;
  truncated?: boolean;
  /** 解析状态：success / pending / failed（后端 parse_status 透传，2026-09-23 起） */
  parse_status?: string;
  doc_category?: string;
  file_type?: string;
  file_size?: number;
  parse_time?: number;
  parse_warnings?: string[];
  created_at?: string;
  [k: string]: unknown;
};

export type DocumentParseListProps = {
  docs: DocumentParseItem[];
  /** 批量解析进行中（此时全列表的解析类操作都应禁用） */
  parsingDocs: boolean;
  /** 正在解析的单份文档 id（null 表示无单份解析在跑） */
  parsingDocId: string | null;
  /** 分类下拉选项；为空数组时分类降级为只读 Tag */
  categoryOptions: string[];
  /** 解析 / 重新解析（force=true） */
  onParse: (docId: string, fileName: string, force?: boolean) => void;
  onPreview: (doc: DocumentParseItem) => void;
  onDelete: (docId: string, fileName: string) => void;
  onCategoryChange: (docId: string, category: string) => void;
  /**
   * 2026-09-23 紧凑模式：默认只显示前 N 行，超过则折叠并显示"展开全部"按钮。
   * 文档解析 Tab 用来减少右侧总高度（用户要求"总高度减少 1/3"）。
   * 默认 false（向后兼容），开启时 defaultVisibleCount 默认 3。
   */
  compact?: boolean;
  /** compact=true 时默认显示的行数阈值，超过则折叠；默认 3 */
  defaultVisibleCount?: number;
};

/**
 * 文件分类 → Ant Design Tag color 映射。
 *
 * 后端唯一事实源是 `app/services/doc_categories.py::DOC_CATEGORIES`（9 类），
 * 本表与其逐条对齐；`getCategoryColor` 对未登记分类返回 "default"，
 * 故后端新增分类不会导致前端崩溃或空标签（只是配色降级）。
 */
const CATEGORY_COLOR_MAP: Record<string, string> = {
  "招标文件": "red",
  "合同文件": "orange",
  "设计文件": "geekblue",
  "地勘报告": "purple",
  "报价清单": "gold",
  "资质材料": "cyan",
  "人员资料": "lime",
  "财务资料": "magenta",
  "业绩证明": "green",
};

export function getCategoryColor(category?: string): string {
  if (!category) return "default";
  return CATEGORY_COLOR_MAP[category] || "default";
}

/** 文件大小（字节）→ 人类可读（KB/MB/GB） */
export function formatFileSize(bytes: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * 上传时间 → 可读短格式（`MM-DD HH:mm`）。
 *
 * 后端 `project_documents.created_at` 落库为 SQLite 本地时间字符串
 * `YYYY-MM-DD HH:MM:SS`（见 schema_sql.py `datetime('now','localtime')`）。
 * 旧实现把原始字符串直接渲染在列表行，既不友好、也让「同一批上传」难以辨认。
 * 本函数兼容：
 *   - `YYYY-MM-DD HH:MM:SS` / `YYYY-MM-DDTHH:MM:SS`（本地或含时区 ISO）
 *   - 空值 / 非法值 → 返回空串（列表不渲染占位）
 *   - 跨年数据 → 补上年份前缀 `YYYY-MM-DD HH:mm`，避免歧义
 */
export function formatUploadTime(raw: string | null | undefined, now: Date = new Date()): string {
  if (!raw) return "";
  const s = String(raw).trim();
  if (!s) return "";
  // 统一为 `YYYY-MM-DD HH:MM:SS`：ISO 的 T 分隔符仅在含时间时存在
  const norm = s.length >= 10 && (s[10] === "T" || s[10] === " ") ? `${s.slice(0, 10)} ${s.slice(11, 19)}` : s;
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?/.exec(norm);
  if (!m) return "";
  const [, y, mo, d, hh, mm] = m;
  const year = Number(y), month = Number(mo), day = Number(d), hour = Number(hh), min = Number(mm);
  // ✅ 越界防护：JS 的 Date 构造对越界分量会「进位」而不是返回 NaN
  //    （如 2026-13-40 99:99 → 2027-02 之后某天），脏数据会被当作合法时间渲染。
  //    先校验分量范围，再回读构造后的分量做往返比对，双保险。
  if (year < 1970 || year > 2100 || month < 1 || month > 12 ||
      day < 1 || day > 31 || hour < 0 || hour > 23 || min < 0 || min > 59) {
    return "";
  }
  const date = new Date(year, month - 1, day, hour, min);
  if (Number.isNaN(date.getTime())) return "";
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 ||
      date.getDate() !== day || date.getHours() !== hour || date.getMinutes() !== min) {
    return "";
  }
  const sameYear = date.getFullYear() === now.getFullYear();
  return sameYear
    ? `${mo}-${d} ${hh}:${mm}`
    : `${y}-${mo}-${d} ${hh}:${mm}`;
}

export default function DocumentParseList({
  docs,
  parsingDocs,
  parsingDocId,
  categoryOptions,
  onParse,
  onPreview,
  onDelete,
  onCategoryChange,
  compact = false,
  defaultVisibleCount = 3,
}: DocumentParseListProps) {
  // 紧凑模式下的展开/折叠状态；默认折叠（仅显示前 N 行）
  const [expanded, setExpanded] = useState(false);
  const shouldTruncate = compact && docs.length > defaultVisibleCount && !expanded;
  const visibleDocs = shouldTruncate ? docs.slice(0, defaultVisibleCount) : docs;
  if (docs.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="还没有导入资料文件，点击上方「上传文件保存」开始"
      />
    );
  }

  /** 解析类按钮是否禁用：批量解析中，或已有任意单份解析在跑 */
  const parseDisabled = parsingDocs || !!parsingDocId;

  // ✅ BUG 修复：后端 category-options 是唯一合法分类源；若历史数据/脏数据
  //    带入了未登记分类，Select 的 value 与 options 不匹配会显示空。
  //    这里补上后端固定包含的「其他」，保证兜底值一定能被下拉识别。
  const normalizedCategoryOptions = categoryOptions.length > 0 && !categoryOptions.includes("其他")
    ? [...categoryOptions, "其他"]
    : categoryOptions;

  return (
    <div className="dpl-compact-list">
    <List
      size="small"
      dataSource={visibleDocs}
      // 2026-09-23：compact 模式下 dataSource 已截断为 visibleDocs（默认 3 行），
      // 非 compact 时 visibleDocs === docs，行为不变。行距压缩由外层 .dpl-compact-list CSS 负责。
      renderItem={(doc) => {
        // ✅ 增强：分类颜色 + 大小 + 解析耗时
        const categoryColor = getCategoryColor(doc.doc_category);
        const sizeLabel = doc.file_size ? formatFileSize(doc.file_size) : "";
        const parseTimeLabel = doc.parse_time ? `${doc.parse_time}s` : "";
        // ✅ 增强（2026-09-23）：解析状态以后端 parse_status 为准，向后兼容旧数据
        //    （无该字段时回退 text_len 判定）。failed 单独标记，避免"解析失败"
        //    被误显为"待解析"。
        // ✅ 2026-09-25：判定逻辑统一到 workflowDerived.isDocParsed / isDocFailed
        //    （此前本组件与 computeDocStats 各写一份，一处改动即产生「统计条」与
        //    「列表标签」分裂，且两侧单测都无法发现）。
        const isParsed = isDocParsed(doc);
        const isFailed = isDocFailed(doc);
        return (
          <List.Item
            actions={[
              // ✅ 新增：已解析文档可快速预览
              isParsed ? (
                <Button
                  key="preview"
                  size="small"
                  type="link"
                  icon={<EyeOutlined />}
                  onClick={() => onPreview(doc)}
                >
                  预览
                </Button>
              ) : null,
              ...(!isParsed
                ? [
                    <Button
                      key="parse"
                      size="small"
                      type="link"
                      icon={<FileSearchOutlined />}
                      disabled={parseDisabled}
                      loading={parsingDocId === doc.id}
                      onClick={() => onParse(doc.id, doc.file_name)}
                    >
                      解析
                    </Button>,
                  ]
                : []),
              ...(isParsed
                ? [
                    <Tooltip
                      key="reparse"
                      title={
                        doc.truncated
                          ? "该文档内容可能被旧版解析上限截断，重新解析可获取完整内容"
                          : "重新解析该文档"
                      }
                    >
                      <Button
                        size="small"
                        type="link"
                        icon={<ReloadOutlined />}
                        disabled={parseDisabled}
                        loading={parsingDocId === doc.id}
                        onClick={() => onParse(doc.id, doc.file_name, true)}
                      >
                        重新解析
                      </Button>
                    </Tooltip>,
                  ]
                : []),
              <Button
                key="del"
                size="small"
                danger
                type="link"
                icon={<CloseCircleOutlined />}
                disabled={parsingDocs || parsingDocId === doc.id}
                onClick={() => onDelete(doc.id, doc.file_name)}
              >
                删除
              </Button>,
            ]}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
              <FileTextOutlined style={{ fontSize: 16, color: "#8c8c8c" }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{ fontWeight: 500, fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  title={doc.file_name}
                >
                  {doc.file_name}
                </div>
                <div style={{ fontSize: 12, color: "#999" }}>
                  {/* ✅ C-2 接线：分类可下拉改判（自动关键词归类不准时的兜底），选项未加载到时降级只读 Tag */}
                  {normalizedCategoryOptions.length > 0 ? (
                    <Select
                      size="small"
                      variant="borderless"
                      style={{ width: 116, margin: "0 4px 0 -8px" }}
                      value={normalizedCategoryOptions.includes(doc.doc_category || "")
                        ? (doc.doc_category || "其他")
                        : "其他"}
                      options={normalizedCategoryOptions.map((c) => ({ label: c, value: c }))}
                      onChange={(v: string) => onCategoryChange(doc.id, v)}
                    />
                  ) : (
                    doc.doc_category && doc.doc_category !== "其他" && (
                      <Tag color={categoryColor} style={{ margin: "0 4px 0 0" }}>{doc.doc_category}</Tag>
                    )
                  )}
                  <Tag color="default" style={{ margin: "0 4px 0 0" }}>
                    {doc.file_type?.toUpperCase() || "未知格式"}
                  </Tag>
                  {sizeLabel && (
                    <Tag color="blue" style={{ margin: "0 4px 0 0" }}>{sizeLabel}</Tag>
                  )}
                  {isParsed ? (
                    <Tag color="green" style={{ margin: "0 4px 0 0" }}>✓ 已解析 {doc.text_len} 字</Tag>
                  ) : isFailed ? (
                    <Tooltip
                      title={
                        Array.isArray(doc.parse_warnings) && doc.parse_warnings.length > 0
                          ? <div>{doc.parse_warnings.map((w: string, i: number) => <div key={i}>· {w}</div>)}</div>
                          : "该文档解析失败，可点击「解析」按钮重试"
                      }
                    >
                      <Tag color="red" style={{ margin: "0 4px 0 0" }}>⚠ 解析失败</Tag>
                    </Tooltip>
                  ) : (
                    <Tag color="orange" style={{ margin: "0 4px 0 0" }}>待解析</Tag>
                  )}
                  {/* ✅ 解析耗时（> 0 才显示） */}
                  {parseTimeLabel && Number(doc.parse_time) > 0 && (
                    <Tag style={{ margin: "0 4px 0 0" }}>⏱ {parseTimeLabel}</Tag>
                  )}
                  {isParsed && doc.truncated && (
                    <Tooltip title="内容达到旧版上限可能被截断，建议点「重新解析」获取完整内容">
                      <Tag color="volcano" style={{ margin: "0 4px 0 0" }}>⚠ 可能被截断</Tag>
                    </Tooltip>
                  )}
                  {/* ✅ 解析器诊断告警（PDF 加密 / OCR 兜底等，已持久化到 parse_warnings） */}
                  {isParsed && Array.isArray(doc.parse_warnings) && doc.parse_warnings.length > 0 && (
                    <Tooltip title={<div>{doc.parse_warnings.map((w: string, i: number) => <div key={i}>· {w}</div>)}</div>}>
                      <Tag color="gold" style={{ margin: "0 4px 0 0" }}>⚠ 解析提示 {doc.parse_warnings.length}</Tag>
                    </Tooltip>
                  )}
                  {/* ✅ 上传时间（SQLite 本地时间 → 可读短格式；非法/空值不渲染占位） */}
                  {formatUploadTime(doc.created_at) && (
                    <Tag style={{ margin: "0 4px 0 0", color: "#999", borderColor: "transparent", background: "transparent" }}>
                      🕒 {formatUploadTime(doc.created_at)}
                    </Tag>
                  )}
                </div>
              </div>
            </div>
          </List.Item>
        );
      }}
    />
    {/* 2026-09-23：compact 模式下文件超过 defaultVisibleCount 时，
        显示"展开全部/收起"按钮，让用户按需展开，默认折叠减少右侧总高度 ~72px。 */}
    {compact && docs.length > defaultVisibleCount && (
      <div style={{ textAlign: "center", padding: "2px 0" }}>
        <Button
          type="link"
          size="small"
          onClick={() => setExpanded(!expanded)}
          style={{ padding: "0 4px", height: 22, fontSize: 12 }}
        >
          {expanded
            ? `收起（仅显示前 ${defaultVisibleCount} 项）`
            : `展开全部（共 ${docs.length} 项，当前显示 ${defaultVisibleCount}）`}
        </Button>
      </div>
    )}
    </div>
  );
}
