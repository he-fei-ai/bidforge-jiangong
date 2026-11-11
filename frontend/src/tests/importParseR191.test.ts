/// <reference types="vite/client" />
/**
 * 解析提取链 · R191 前端护栏（2026-11-09）。
 *
 * 收口面（配套后端护栏 tests/test_import_parse_r191_20261109.py）：
 *   C-F1  切方案 loadDocuments 竞态：列表 effect 的 cleanup 中止上一轮请求，
 *         切方案清理 effect 不再 abort（误杀新方案刚发出的请求且无重发 →
 *         新方案页面残留上一个方案的文档列表），并补 setDocList([]) 占位清空。
 *   C-F2  单份解析响应 note 透传（R181 只收口了 parse-all 批量分支）。
 *   C-F3  上传中行内「解析/重新解析/删除」门禁统一（uploadingFacts）。
 *   C-F4  取消预览清 pipelineDocIdRef（管线动作回执不再写状态/多发请求）。
 *   C-F5  parse-all 零结果时优先采用后端早退分支 message。
 *   C-F6  ParseTruncatedItem.reason 消费（截断归因不再一律「内容超长」）。
 *   C-F7  非法上限回落收敛到 resolveMaxUploadBytes（挡 Infinity/NaN）。
 *   C-F9  上传/解析链补 ref 防重入（同 tick 双发防线）。
 *   C1    DocumentPipelineSummary 渲染 layer_errors（R186-R188 失败语义可达用户）。
 *   C3    语义层 hint 与后端边界声明对齐（预留归档，不参与生成）。
 *
 * 判据实现沿用本仓既有范式（importParseR144.test.tsx / contentTerminalParity）：
 * Vite import.meta.glob(..., { query: "?raw" }) 读源码文本，不引 node:fs。
 */
import { describe, it, expect } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"] || "";
const WORKFLOW = RAW["pages/schemeWorkbench/useDocumentWorkflow.tsx"] || "";
const DOC_LIST = RAW["components/DocumentParseList.tsx"] || "";
const PIPELINE = RAW["components/DocumentPipelineSummary.tsx"] || "";
const UPLOAD_TAB = RAW["components/UploadParseTab.tsx"] || "";

/** 截取「文档列表加载」effect 源码段（以 R191 注释为锚）。 */
function docListEffectBody(src: string): string {
  const start = src.indexOf("进入工作台即拉取资料文档列表");
  if (start < 0) return "";
  const end = src.indexOf("一次性元数据加载", start);
  return src.slice(start, end < 0 ? src.length : end);
}

/** 截取「切方案清理」effect 源码段。 */
function schemeSwitchBody(src: string): string {
  const start = src.indexOf("const previousSchemeId = mountedSchemeIdRef.current;");
  if (start < 0) return "";
  const end = src.indexOf("}, [id]);", start);
  return src.slice(start, end < 0 ? src.length : end);
}

/** 截取 handleCancelPreview 函数体。 */
function cancelPreviewBody(src: string): string {
  const start = src.indexOf("const handleCancelPreview = () => {");
  if (start < 0) return "";
  const end = src.indexOf("};", start);
  return src.slice(start, end < 0 ? src.length : end);
}

describe("R191 · C-F1 切方案 loadDocuments 竞态", () => {
  it("文档列表 effect 必须在 cleanup 中止上一轮请求", () => {
    const body = docListEffectBody(PAGE);
    expect(body).not.toBe("");
    const cleanupIdx = body.indexOf("return () => {");
    expect(cleanupIdx).toBeGreaterThan(-1);
    const cleanup = body.slice(cleanupIdx);
    expect(cleanup).toContain("factsDocsListAbortRef.current?.abort();");
  });

  it("切方案清理 effect 不得再 abort 文档列表请求（会误杀新方案刚发出的请求）", () => {
    const body = schemeSwitchBody(PAGE);
    expect(body).not.toBe("");
    expect(body).not.toContain("factsDocsListAbortRef.current?.abort();");
  });

  it("切方案时必须清空 docList（新方案页面不得闪现旧方案文档）", () => {
    const body = schemeSwitchBody(PAGE);
    expect(body).toContain("setDocList([]);");
  });
});

describe("R191 · C-F2 单份解析 note 透传", () => {
  it("handleParseDocument 成功分支必须拼入 data.note（与 parse-all 口径一致）", () => {
    const start = WORKFLOW.indexOf("const handleParseDocument = async");
    const body = WORKFLOW.slice(start, WORKFLOW.indexOf("const handleCancelPreview", start));
    expect(body).toContain('typeof data.note === "string" && data.note');
    expect(body).toContain("${note}");
  });
});

describe("R191 · C-F3 上传中行内按钮门禁", () => {
  it("DocumentParseList 的 parseDisabled 与删除按钮必须含 uploadingFacts", () => {
    expect(DOC_LIST).toContain("const parseDisabled = parsingDocs || !!parsingDocId || uploadingFacts;");
    expect(DOC_LIST).toContain(
      "disabled={parsingDocs || uploadingFacts || parsingDocId === doc.id}",
    );
    // 删除按钮既有语义（仅解析行禁删）不得被改宽
    expect(DOC_LIST).not.toContain("!!parsingDocId || uploadingFacts || parsingDocId === doc.id");
  });

  it("UploadParseTab 必须向 DocumentParseList 传 uploadingFacts", () => {
    expect(UPLOAD_TAB).toContain("uploadingFacts={uploadingFacts}");
  });
});

describe("R191 · C-F4 取消预览清管线归属", () => {
  it("handleCancelPreview 必须清 pipelineDocIdRef.current", () => {
    const body = cancelPreviewBody(WORKFLOW);
    expect(body).toContain("pipelineDocIdRef.current = null;");
  });
});

describe("R191 · C-F5 parse-all 零结果采用后端 message", () => {
  it("parsed=0 且无失败/截断时必须消费 data.message（早退分支真相）", () => {
    expect(WORKFLOW).toContain('typeof data.message === "string" && data.message');
  });
});

describe("R191 · C-F6 截断归因 reason", () => {
  it("ParseTruncatedItem 声明 reason 且 truncation 分支消费", () => {
    expect(WORKFLOW).toContain("reason?: string;");
    expect(WORKFLOW).toContain("原因：${reasons}");
  });
});

describe("R191 · C-F7 上限口径收敛", () => {
  it("UploadParseTab / useDocumentWorkflow 必须走 resolveMaxUploadBytes", () => {
    expect(UPLOAD_TAB).toContain("resolveMaxUploadBytes(");
    expect(WORKFLOW).toContain("resolveMaxUploadBytes(");
    // 旧写法（不挡 Infinity/NaN）不得残留
    expect(WORKFLOW).not.toContain('typeof maxUploadBytes === "number" && maxUploadBytes > 0');
    expect(UPLOAD_TAB).not.toContain('typeof maxUploadBytes === "number" && maxUploadBytes > 0');
  });
});

describe("R191 · C-F9 上传/解析链 ref 防重入", () => {
  it("uploadInFlightRef / parseInFlightRef 声明且三条 guard 消费", () => {
    expect(WORKFLOW).toContain("const uploadInFlightRef = useRef(false);");
    expect(WORKFLOW).toContain("const parseInFlightRef = useRef(false);");
    // 上传 guard（R208 FE-9：guard 条件与 ref 消费关系不变，仅从单行 return
    // 展开为「给用户可见反馈 + return」的多行块 —— 重锚到**意图**而非字面量）
    expect(WORKFLOW).toContain("uploadingFacts || uploadInFlightRef.current");
    // 批量解析 guard
    expect(WORKFLOW).toContain("parsingDocs || parsingDocId || parseInFlightRef.current");
    // 单份解析 guard
    expect(WORKFLOW).toContain("parsingDocId || parsingDocs || parseInFlightRef.current");
    // ✅ R208 FE-9 加强：三条 guard 命中时必须给出用户可见反馈（此前静默
    //    return → 用户点击「完全没反应」= 假动作）。三条 guard 各自带
    //    msg.info 提示，防止将来被「简化」回静默 return。
    expect(WORKFLOW.match(/msg\.info\(/g)?.length).toBeGreaterThanOrEqual(3);
    // finally 必须解除（abort 抢占时 state 不复位但请求已结束）
    expect(WORKFLOW.match(/uploadInFlightRef\.current = false;/g)?.length).toBe(1);
    expect(WORKFLOW.match(/parseInFlightRef\.current = false;/g)?.length).toBe(2);
  });

  it("批量解析的置位必须在早退之后（早退路径不经 finally）", () => {
    const start = WORKFLOW.indexOf("const handleParseDocuments = async");
    const body = WORKFLOW.slice(start, WORKFLOW.indexOf("const handleParseDocument = async", start));
    const setIdx = body.indexOf("parseInFlightRef.current = true;");
    const earlyIdx = body.indexOf('msg.info("所有文档均已解析");');
    const forceIdx = body.indexOf('msg.info("还没有上传资料文件");');
    expect(setIdx).toBeGreaterThan(earlyIdx);
    expect(setIdx).toBeGreaterThan(forceIdx);
  });
});

describe("R191 · C1/C3 管线失败语义与语义层文案", () => {
  it("DocumentPipelineSummary 必须消费 layer_errors（volcano 警示 Tag）", () => {
    expect(PIPELINE).toContain("layer_errors?: Record<string, string>;");
    // 赋值表达式必须真实读取 status.layer_errors（A/B M11 教训：仅锁
    // Tag 颜色三元表达式时，摘除赋值仍静态通过——静默降级回中性灰 Tag）。
    expect(PIPELINE).toContain("status.layer_errors && typeof status.layer_errors[layer.key] === \"string\"");
    expect(PIPELINE).toContain('layerError ? "volcano"');
    expect(PIPELINE).toContain("{layerError ? \"⚠ \" : \"\"}");
  });

  it("语义层 hint 与后端边界声明对齐（预留归档，不参与生成）", () => {
    expect(PIPELINE).toContain("预留归档（向量检索未接入，不参与生成链路）");
    expect(PIPELINE).not.toContain("供目录/事实提取定位");
  });
});
