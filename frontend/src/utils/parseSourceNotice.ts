/**
 * 解析可信度提示派生（纯函数层）。
 *
 * 背景：后端已经能判定「文档内容不完整」——
 *   · `project_documents.parse_truncated`（解析器级截断：PDF 超页 / 表格超行 / 字数上限）
 *   · `parse_warnings`（编码降级、OCR 兜底、二进制伪装等诊断）
 *   · 提取侧的 `source_truncated` / `truncated_docs` / `dropped_doc_count`
 * 但这些信号此前停在后端或只进了日志：用户在「预览」弹窗里看到的正文与完整文档
 * 一样长（都远小于预览上限），在「项目提取」里也看不到「提取依据不完整」，
 * 于是放心地拿着残缺资料往下走。
 *
 * 本文件把后端信号翻译成用户可读的提示文案，集中在纯函数里，避免页面/组件
 * 各自拼字符串而漂移；文案规则由 tests/parseSourceNotice.test.ts 钉住。
 */

/** 后端 SSE `text_stats` / HTTP 响应里的「提取依据完整性」报告 */
export type SourceReportLike = {
  source_truncated?: boolean;
  truncated_docs?: Array<{ name?: string; category?: string; chars?: number }>;
  used_doc_count?: number;
  input_doc_count?: number;
  dropped_doc_count?: number;
};

/** 预览接口响应（只声明本文件关心的字段，其余原样透传） */
export type PreviewDataLike = {
  is_parsed?: boolean;
  text_len?: number;
  /** 解析级截断：文档正文在解析阶段就不完整（内容可能真的缺了） */
  truncated?: boolean;
  /** 预览级截断：只是本弹窗没显示全，完整内容仍在库里 */
  preview_truncated?: boolean;
  parse_warnings?: string[];
  message?: string;
};

export type ParseNotice = {
  /** warning = 内容可能真的不完整（用户需要行动）；info = 展示层截取 */
  kind: "warning" | "info";
  title: string;
  detail?: string;
};

function _joinNames(docs: Array<{ name?: string }>): string {
  const names = docs.map((d) => d.name).filter(Boolean);
  if (names.length === 0) return "";
  if (names.length === 1) return names[0] as string;
  return names.slice(0, 3).join("、") + (names.length > 3 ? ` 等 ${names.length} 份` : "");
}

/**
 * 由「提取依据完整性」报告派生提示。
 *
 * 优先级：被截断的文档（最严重，内容确实缺）> 被整份跳过的文档
 * （预算耗尽，一个字都没用上）> 无异常返回 null。
 */
export function sourceNotice(
  report: SourceReportLike | null | undefined,
): ParseNotice | null {
  if (!report || !report.source_truncated) return null;
  const trunc = (report.truncated_docs || []).filter((d) => d.name);
  const dropped = report.dropped_doc_count || 0;

  if (trunc.length > 0) {
    const detail = dropped > 0
      ? `其中 ${dropped} 份因长度预算耗尽未被使用`
      : undefined;
    return {
      kind: "warning",
      title: `提取依据不完整：${_joinNames(trunc)} 的解析结果已被截断`,
      detail,
    };
  }
  if (dropped > 0) {
    return {
      kind: "warning",
      title: `提取依据不完整：${dropped} 份文档因长度预算耗尽未被使用`,
      detail: "建议拆分上传超长的资料文件，或先剔除无关附件",
    };
  }
  return {
    kind: "warning",
    title: "提取依据不完整：部分源文档内容缺失",
  };
}

/**
 * 由预览接口响应派生提示列表。
 *
 * 关键区分（旧实现的缺口）：
 *   · `truncated`（解析级）→ warning，内容真的可能缺了，必须提示「建议拆分后重新上传」；
 *   · `preview_truncated`（预览级）→ info，只是弹窗没显示全，完整内容仍在库里；
 *   · `parse_warnings` → info，编码降级 / OCR 兜底等诊断，用户需要知情但不必行动。
 * 两者绝不能互相覆盖：一份「50 页后被截断」的 PDF 正文很短，
 * `preview_truncated` 为 false，只有解析级信号能暴露问题。
 */
export function previewNotices(
  data: PreviewDataLike | null | undefined,
): ParseNotice[] {
  if (!data) return [];
  const out: ParseNotice[] = [];

  if (data.truncated) {
    out.push({
      kind: "warning",
      title: "该文档内容不完整（解析时被截断）",
      detail: "建议拆分文件后重新上传，以获得完整的解析结果",
    });
  } else if (data.preview_truncated) {
    out.push({
      kind: "info",
      title: "预览仅显示前部分内容",
      detail: data.text_len
        ? `已解析正文共 ${data.text_len.toLocaleString()} 字`
        : undefined,
    });
  }

  const warnings = Array.isArray(data.parse_warnings)
    ? data.parse_warnings.filter((w) => w)
    : [];
  if (warnings.length > 0) {
    out.push({
      kind: "info",
      title: `解析过程提示 ${warnings.length} 条`,
      detail: warnings.join("；"),
    });
  }
  return out;
}
