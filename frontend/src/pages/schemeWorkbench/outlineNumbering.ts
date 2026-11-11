import { storedIdParts } from "../../utils/outlineNumbering";

export const CN_NUMBERS: string[] = [
  "",
  "一",
  "二",
  "三",
  "四",
  "五",
  "六",
  "七",
  "八",
  "九",
  "十",
  "十一",
  "十二",
  "十三",
  "十四",
  "十五",
  "十六",
  "十七",
  "十八",
  "十九",
  "二十",
  "二十一",
  "二十二",
  "二十三",
  "二十四",
  "二十五",
  "二十六",
  "二十七",
  "二十八",
  "二十九",
  "三十",
];

// ✅ 与后端 strip_outline_numbering 同源：剥离标题开头已嵌入的编号前缀，
// 修复存量数据 "第一章 第一章 工程概况" / "2.1 2.1 相关法律法规" 双重编号
//
// ✅ BUG 修复（2026-09-27 · 标题损坏，与后端 numbering.py 同步修改）：
// 旧实现的点分编号分支写作 `[0-9]+(?:\.[0-9]+){0,7}[分隔符]+`，路径后**没有**
// 分隔符时 JS 正则同样会回退成「更短前缀 + 把点当分隔符」，把标题剥成残句：
//   "1.2.3（1）细部构造" → "3（1）细部构造"
//   "2.4.1钢筋工程"      → "1钢筋工程"（残留数字 + 叠加位置编号后双重编号）
// 现改为「前瞻捕获最长路径 + 反向引用整条吃满」（正则没有原子组语法，但前瞻
// 是原子的、反向引用不可回退），路径后要求紧跟分隔符或 CJK/全角字符。
// 分隔符与 CJK 边界字符类必须与后端 _SEP_CLASS / _CJK_BOUNDARY 逐字符一致。
//
// ✅ BUG 修复（2026-10-03 · 数量型标题丢首字，与后端 numbering.py 同步修改）：
// 点分编号分支的捕获原用 `[0-9]+(?:\.[0-9]+)*`（星号），于是「单段数字直接接
// 中文」（无分隔符）也被当编号剥离："2层作业平台" → "层作业平台"、"10个人" →
// "个人"。此 CJK 无分隔符剥离本就只为多段点分路径（"2.4.1钢筋工程"）设计，
// 现限定为至少一个点段 `(?:\.[0-9]+)+`，单段数字剥离仍由下方「[0-9]+分隔符+
// (?![0-9])」分支负责。必须与后端 _STRIP_NUMBER_RE 逐字符一致（parity 测试锁定）。
export const LEADING_NUMBER_SEP = "[、.．，,：: \\-—]";
export const LEADING_NUMBER_CJK = "[\\u3000-\\u303f\\u4e00-\\u9fff\\uff00-\\uffef]";
// ✅ R216（2026-11-11 · 全角编号）：后端 numbering.py::_STRIP_NUMBER_RE 已把
//    数字类并入全角 ０-９、点号扩为 [.．]、单段分支负向断言扩为 (?![0-9０-９])。
//    本侧必须**逐字符同步**（parity 测试 test_import_outline_deep_fix 锁定），
//    否则前端剥除与后端成稿口径分叉 ⇒ 全角编号双重编号 / 全角小数数量被剥坏。
export const LEADING_NUMBER_RE = new RegExp(
  "^\\s*(?:" +
    "第\\s*[一二三四五六七八九十百千零0-9０-９]+\\s*[章节]" + // 第X章 / 第 1 章
    "|[（(][一二三四五六七八九十0-9０-９]+[)）]" + // （三） / (3)
    "|[0-9０-９]+[)）]" + // 1） / 2）
    "|(?=([0-9０-９]+(?:[.．][0-9０-９]+)+))\\1(?:" +
    LEADING_NUMBER_SEP +
    "+|(?=" +
    LEADING_NUMBER_CJK +
    "))" +
    "|[0-9０-９]+" +
    LEADING_NUMBER_SEP +
    "+(?![0-9０-９])" + // 单段编号（"1 施工准备"）
    "|[一二三四五六七八九十百千零]+" +
    LEADING_NUMBER_SEP +
    "+" +
    // ✅ R218（2026-11-11 · P3 · N4）：与后端 numbering.py::_STRIP_NUMBER_RE 逐字符
    //    同步——Unicode 罗马数字（"Ⅰ. 总则"）与中文大写（"壹、总则"）是纯编号形态，
    //    仅后接分隔符才剥（紧邻字符不剥，"Ⅳ级" 原样）。ASCII 罗马（I. / II.）
    //    刻意不剥，与后端及导入识别侧口径一致（避免 "I 型钢" 被误剥）。
    "|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅬⅭⅮⅯⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹⅺⅻ]+" +
    LEADING_NUMBER_SEP +
    "+" +
    "|[壹贰叁肆伍陆柒捌玖拾佰仟零貳參陸]+" +
    LEADING_NUMBER_SEP +
    "+" +
    ")" +
    LEADING_NUMBER_SEP +
    "*",
);

// 整条标题本身就是一个编号（"1" / "1.1" / "1.2.3"）时必须原样返回，
// 不能走剥离流程 —— 否则 "." 同时属于分隔符字符类，会只剥半截
//（"1.1" → "1"、"1.2.3" → "3"），与后端 numbering.strip_outline_numbering
// 的 _PURE_NUMBER_TITLE_RE 短路契约不一致：同一节点前后端展示会不同，
// 叠加位置编号后即产生编号漂移 / 双重编号。
export const PURE_NUMBER_TITLE_RE = /^[0-9]+(?:[.．][0-9]+)*$/;

// ✅ 年份不是编号（与后端 _YEAR_PREFIX_RE 同口径）：
// "2023 年度安全生产计划" / "2024年施工计划" 原样保留，避免标题丢年份。
export const YEAR_PREFIX_RE = /^\s*[0-9]{4}\s*年/;

export function stripOutlineNumbering(title: string): string {
  if (!title) return title;
  const trimmed = title.trim();
  if (PURE_NUMBER_TITLE_RE.test(trimmed)) return trimmed;
  if (YEAR_PREFIX_RE.test(title)) return trimmed;
  const stripped = title.replace(LEADING_NUMBER_RE, "").trim();
  return stripped || trimmed;
}

/**
 * ✅ R203：Excel 风格字母编号 —— 1→a, 2→b, …, 26→z, 27→aa, 28→ab, …。
 *    替代旧 L7 的 alphaLetters[idx] 钳制（超 26 个同级尾注静默折叠为 z）。
 */
export function alphaLabel(n: number): string {
  const A = "abcdefghijklmnopqrstuvwxyz";
  let x = Math.max(1, Math.floor(n));
  let out = "";
  while (x > 0) {
    x -= 1;
    out = A[x % 26] + out;
    x = Math.floor(x / 26);
  }
  return out;
}

export function formatOutlineTitle(id: string | undefined, level: number, title: string): string {
  if (!title || !title.trim()) return title || "";
  const bareTitle = stripOutlineNumbering(title);
  // ✅ R87 FIX-1（P1）：合法性判据收敛到 `utils/outlineNumbering`（与后端
  //    `numbering.stored_id_parts` 同口径）。旧实现逐段 parseInt 挑数字，会把
  //    `abc1.2` → [2]、`0.3` → [3]、`1.0.2` → [1,2] **抢救**成看似合法的编号，
  //    而后端对同一输入 fail-closed ⇒ 目录树显示的章号与导出成稿对不上。
  //    非法编号一律**退回裸标题**（不编造编号）。
  const parts = storedIdParts(id);
  if (!parts.length) return bareTitle;

  let number = "";
  let sep = " ";
  if (level === 1) {
    number = `第${CN_NUMBERS[parts[0]] || parts[0]}章`;
  } else if (level === 2) {
    // L2: "1"（tail 不含章号，直接第一个 part）
    number = String(parts[parts.length - 1]);
  } else if (level === 3) {
    // L3: "1.1"（tail 最后两个 part）
    number = parts.slice(-2).join(".");
  } else if (level === 4) {
    // L4: "1.1.1"（tail 最后三个 part）
    number = parts.slice(-3).join(".");
  } else if (level === 5) {
    // L5: "1.1.1.1"（十进制延续，tail 最后四个）+ 、分隔
    number = parts.slice(-4).join(".");
    sep = "、";
  } else if (level === 6) {
    // L6: "1）"（括号数字，只用最后一个 part）+ 、分隔
    number = `${parts[parts.length - 1]}）`;
    sep = "、";
  } else if (level === 7) {
    // L7: "a" / "b"（小写字母，只用最后一个 part）+ 、分隔
    // ✅ R203 FIX：旧实现钳制 idx ≤ 25（z），超 26 个同级尾注静默折叠为 z。
    //    改用 Excel 风格双字母续编：26 以内 a-z，27 起 aa/ab/…/az/ba/…
    const n = parts[parts.length - 1] || 1;
    number = alphaLabel(n);
    sep = "、";
  } else {
    return bareTitle;
  }
  return `${number}${sep}${bareTitle}`;
}
