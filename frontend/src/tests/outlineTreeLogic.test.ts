/**
 * 「目录生成」Tab · 纯函数逻辑测试（2026-09-20 补齐组件级交互测试覆盖）。
 *
 * 目录 Tab 的编号格式化、目录树遍历/编辑、以及「导入目录（智能识别）」结果
 * 诊断此前全部内联在 7500 行工作台页面里、零单测覆盖。现已把这些无闭包依赖的
 * 纯函数提升到模块级并导出，本文件锁定其行为，重点守住：
 *   1. 编号口径与后端 strip_outline_numbering / renumber_outline 同源（防双重编号）；
 *   2. 目录树三级上限、正文探测、就地更新的不可变性；
 *   3. 导入识别结果诊断：空结果绝不吞掉真实原因、诊断告警如实透传
 *      （对应本次修复的端到端数据传递缺口）。
 */
import { describe, it, expect } from "vitest";
import {
  stripOutlineNumbering,
  formatOutlineTitle,
  countOutline,
  describeReorganizeReport,
  summarizeUploadOutlineResult,
  collectAllKeys,
  hasAnyContent,
  treeNodesWithContent,
  outlineTreeDepth,
  findFirstContentDescendant,
  updateNodeFields,
  findSectionById,
  type TreeNode,
} from "../pages/SchemeWorkbenchPage";

// 便捷构造：只关心测试用到的字段，其余给默认值
function node(partial: Partial<TreeNode> & { key: string }): TreeNode {
  return {
    title: partial.title ?? partial.key,
    level: partial.level ?? 1,
    status: partial.status ?? "empty",
    word_count: partial.word_count ?? 0,
    word_budget: partial.word_budget ?? 1500,
    children: partial.children ?? [],
    ...partial,
  };
}

// ============================================================
// 编号格式化 / 剥离（与后端同源的显示层收口）
// ============================================================
describe("stripOutlineNumbering（剥离标题内嵌编号）", () => {
  it("剥离「第一章 / （三） / 2.1 / 一、」等前缀", () => {
    expect(stripOutlineNumbering("第一章 工程概况")).toBe("工程概况");
    expect(stripOutlineNumbering("（三）施工组织")).toBe("施工组织");
    expect(stripOutlineNumbering("2.1 相关法律法规")).toBe("相关法律法规");
    expect(stripOutlineNumbering("一、安全保证")).toBe("安全保证");
  });

  it("每次只剥离一层（与后端 count=1 同口径）——双重编号剥离一次后仍余一层", () => {
    expect(stripOutlineNumbering("第一章 第一章 工程概况")).toBe("第一章 工程概况");
  });

  it("正常数字开头标题不受影响（无分隔符不剥离）", () => {
    expect(stripOutlineNumbering("2023年规范")).toBe("2023年规范");
    expect(stripOutlineNumbering("3D打印技术")).toBe("3D打印技术");
  });

  it("整条标题就是编号时回退原文（避免清空）", () => {
    expect(stripOutlineNumbering("第一章")).toBe("第一章");
  });

  // ✅ 与后端 numbering.strip_outline_numbering 的 _PURE_NUMBER_TITLE_RE 同源。
  //    修复前无此前置短路："2.1" 会被剥成 "1"、"1.2.3" 剥成 "3"
  //    （"." 同时属于分隔符字符类，只剥半截），与后端返回原文的契约相反，
  //    导致同一节点前后端显示不一致，叠加位置编号后出现编号漂移/双重编号。
  it("整条标题是纯编号路径时原样返回（与后端短路契约一致，不得剥半截）", () => {
    expect(stripOutlineNumbering("2.1")).toBe("2.1");
    expect(stripOutlineNumbering("1.2.3")).toBe("1.2.3");
    expect(stripOutlineNumbering("1.2.3.4.5.6.7.8")).toBe("1.2.3.4.5.6.7.8");
    expect(stripOutlineNumbering("7")).toBe("7");
  });

  it("纯编号标题带首尾空白时按 trim 后原样返回", () => {
    expect(stripOutlineNumbering("  2.1  ")).toBe("2.1");
  });

  it("纯编号判定不影响「编号+文字」的正常剥离", () => {
    expect(stripOutlineNumbering("2.1 编制依据")).toBe("编制依据");
    expect(stripOutlineNumbering("1.0 零级")).toBe("零级");
  });

  it("空标题安全返回（不抛异常）", () => {
    expect(stripOutlineNumbering("")).toBe("");
  });
});

describe("formatOutlineTitle（按层级套编号）", () => {
  it("一级：第X章 + 中文数字", () => {
    expect(formatOutlineTitle("1", 1, "工程概况")).toBe("第一章 工程概况");
    expect(formatOutlineTitle("2", 1, "第一章 施工部署")).toBe("第二章 施工部署");
  });
  it("二级：取末段阿拉伯数字", () => {
    expect(formatOutlineTitle("1.2", 2, "现场布置")).toBe("2 现场布置");
  });
  it("三级：取末两段点分编号", () => {
    expect(formatOutlineTitle("1.2.3", 3, "3.1 现场勘查")).toBe("2.3 现场勘查");
  });
  it("无编号路径时只返回裸标题", () => {
    expect(formatOutlineTitle("", 1, "工程概况")).toBe("工程概况");
  });
  it("空标题原样返回", () => {
    expect(formatOutlineTitle("1", 1, "")).toBe("");
  });
});

// ============================================================
// 目录树遍历 / 编辑纯函数
// ============================================================
describe("collectAllKeys / findSectionById", () => {
  const tree: TreeNode[] = [
    node({ key: "a", children: [node({ key: "a1", children: [node({ key: "a11" })] })] }),
    node({ key: "b" }),
  ];
  it("深度优先收集全部节点 key", () => {
    expect(collectAllKeys(tree)).toEqual(["a", "a1", "a11", "b"]);
  });
  it("按 key 命中嵌套节点 / 未命中返回 null", () => {
    expect(findSectionById(tree, "a11")?.key).toBe("a11");
    expect(findSectionById(tree, "zzz")).toBeNull();
  });
});

describe("正文探测（hasAnyContent / treeNodesWithContent）", () => {
  it("content 或 word_count>0 均视为有正文", () => {
    const tree: TreeNode[] = [
      node({ key: "a", content: "有内容" }),
      node({ key: "b", word_count: 12 }),
      node({ key: "c" }),
    ];
    expect(hasAnyContent(tree)).toBe(true);
    expect(treeNodesWithContent(tree)).toBe(2);
    expect(hasAnyContent([node({ key: "x" })])).toBe(false);
    expect(treeNodesWithContent([])).toBe(0);
  });
  it("嵌套子节点中的正文也被统计", () => {
    const tree: TreeNode[] = [node({ key: "a", children: [node({ key: "a1", word_count: 5 })] })];
    expect(treeNodesWithContent(tree)).toBe(1);
  });
});

describe("outlineTreeDepth（三级上限判定）", () => {
  it("空树深度 0，三级树深度 3", () => {
    expect(outlineTreeDepth([])).toBe(0);
    const three: TreeNode[] = [
      node({ key: "1", children: [node({ key: "1.1", children: [node({ key: "1.1.1" })] })] }),
    ];
    expect(outlineTreeDepth(three)).toBe(3);
  });
});

describe("findFirstContentDescendant", () => {
  it("自身有正文返回自身", () => {
    const n = node({ key: "a", content: "x" });
    expect(findFirstContentDescendant(n)?.key).toBe("a");
  });
  it("自身无正文则下钻首个有正文子孙", () => {
    const tree = node({
      key: "a",
      children: [node({ key: "a1" }), node({ key: "a2", word_count: 3 })],
    });
    expect(findFirstContentDescendant(tree)?.key).toBe("a2");
  });
  it("全无正文返回 null", () => {
    expect(findFirstContentDescendant(node({ key: "a", children: [node({ key: "a1" })] }))).toBeNull();
  });
});

describe("updateNodeFields（不可变更新）", () => {
  it("只改目标节点、返回新数组、不突变原树", () => {
    const tree: TreeNode[] = [
      node({ key: "a", children: [node({ key: "a1", word_count: 1 })] }),
    ];
    const next = updateNodeFields(tree, "a1", { word_count: 99, status: "done" });
    expect(next[0].children![0].word_count).toBe(99);
    expect(next[0].children![0].status).toBe("done");
    // 原树保持不变
    expect(tree[0].children![0].word_count).toBe(1);
    expect(next).not.toBe(tree);
  });
});

describe("countOutline（节点总数）", () => {
  it("递归统计含所有层级", () => {
    const outline = [
      { title: "A", children: [{ title: "A1", children: [{ title: "A11" }] }] },
      { title: "B" },
    ];
    expect(countOutline(outline)).toBe(4);
    expect(countOutline([])).toBe(0);
  });
});

// ============================================================
// 导入目录（智能识别）结果诊断 —— 本次修复的端到端数据传递缺口
// ============================================================
describe("describeReorganizeReport（标准骨架归位统计）", () => {
  it("非法输入返回空串", () => {
    expect(describeReorganizeReport(null)).toBe("");
    expect(describeReorganizeReport(undefined)).toBe("");
  });
  it("拼接匹配/补全/补充章节统计", () => {
    const s = describeReorganizeReport({
      template: "deep_pit",
      standard_chapters: 5,
      matched_chapters: 3,
      kept_standard_skeleton: 2,
      appended_extras: 1,
    });
    expect(s).toContain("模板 deep_pit");
    expect(s).toContain("匹配 3/5 章");
    expect(s).toContain("补全空骨架 2 章");
    expect(s).toContain("1 章归入「补充章节」");
  });
  it("补充章节为 0 时省略该项", () => {
    const s = describeReorganizeReport({
      standard_chapters: 4,
      matched_chapters: 4,
      kept_standard_skeleton: 0,
      appended_extras: 0,
    });
    expect(s).toContain("匹配 4/4 章");
    expect(s).not.toContain("补充章节");
  });
});

describe("summarizeUploadOutlineResult（识别结果诊断）", () => {
  it("空目录：判为不可应用，如实回传原因，outline 置空（前端据此不替换目录树）", () => {
    const r = summarizeUploadOutlineResult({
      outline: [],
      empty_text: true,
      warning: "未从文件中解析到有效文本（可能是扫描件）",
      file_name: "scan.pdf",
    });
    expect(r.ok).toBe(false);
    expect(r.level).toBe("warning");
    expect(r.nodeCount).toBe(0);
    expect(r.outline).toEqual([]);
    expect(r.message).toContain("扫描件");
  });

  it("空目录且无 warning：给出可读的兜底原因", () => {
    const r = summarizeUploadOutlineResult({ outline: [] });
    expect(r.ok).toBe(false);
    expect(r.message).toContain("未识别到目录结构");
  });

  it("正常结果：成功 + 节点统计", () => {
    const r = summarizeUploadOutlineResult({
      outline: [{ title: "A", children: [{ title: "A1" }] }, { title: "B" }],
      file_name: "投标.pdf",
    });
    expect(r.ok).toBe(true);
    expect(r.level).toBe("success");
    expect(r.nodeCount).toBe(3);
    expect(r.message).toContain("投标.pdf");
    expect(r.message).toContain("共 3 个章节");
    expect(r.message).not.toContain("标准章节骨架");
  });

  it("整理为标准结构：主提示标注 + 归位统计进入 notices", () => {
    const r = summarizeUploadOutlineResult({
      outline: [{ title: "A" }],
      file_name: "f.docx",
      reorganized: true,
      reorganize_report: {
        template: "t", standard_chapters: 5, matched_chapters: 4,
        kept_standard_skeleton: 1, appended_extras: 0,
      },
    });
    expect(r.ok).toBe(true);
    expect(r.reorganized).toBe(true);
    expect(r.message).toContain("已按标准章节骨架整理");
    expect(r.notices.some((n) => n.includes("匹配 4/5 章"))).toBe(true);
  });

  it("parse_warnings 逐条透传为 notices", () => {
    const r = summarizeUploadOutlineResult({
      outline: [{ title: "A" }],
      file_name: "f.pdf",
      parse_warnings: ["PDF 可能截页", { message: "表格被截断" }],
    });
    expect(r.notices).toContain("PDF 可能截页");
    expect(r.notices).toContain("表格被截断");
  });

  it("非空但原文截断：仍成功，warning 作为首要 notice", () => {
    const r = summarizeUploadOutlineResult({
      outline: [{ title: "A" }],
      file_name: "big.pdf",
      raw_text_truncated: true,
      warning: "原文 8000 字，保存记录时截断至 5000 字",
    });
    expect(r.ok).toBe(true);
    expect(r.notices[0]).toContain("截断");
  });
});
