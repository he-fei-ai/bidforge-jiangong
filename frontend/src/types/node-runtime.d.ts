/**
 * 前端仓是纯浏览器工程，**未安装 @types/node**（package.json devDependencies 无它）。
 * R251 护栏（tests/rightPaneScrollR251.test.tsx）需要在 vitest（node 运行时）下
 * 直读磁盘上的 index.css 做滚动契约断言 —— vitest 默认 css:false，会把 CSS 导入
 * 桩成空串（`?raw` / `import.meta.glob({as:"raw"})` 均无效），只能走 fs。
 *
 * 为此声明用到的最小 API，避免为了一个护栏引入 @types/node 新依赖。
 * 若日后正式安装 @types/node，本文件可整份删除（届时以下声明会与之重复，需移除）。
 */
declare module "node:fs" {
  const fs: {
    readFileSync(path: string, encoding: "utf8"): string;
    existsSync(path: string): boolean;
  };
  export default fs;
}

declare module "node:path" {
  const path: {
    resolve(...segments: string[]): string;
  };
  export default path;
}

declare const process: {
  cwd(): string;
};
