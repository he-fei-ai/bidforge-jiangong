/** 全局事实提取的进度日志条目（阶段/分段级） */
export type FactsLogItem = {
  progress: number; // 0~1
  message: string;
  time: number; // epoch ms
};

/** 目录生成的进度日志条目（阶段/章节级，实时显示工作内容） */
export type OutlineLogItem = {
  progress: number; // 0~1
  message: string;
  done?: number; // 已完成章节数（长方案分步生成时下发）
  total?: number; // 总章节数
  time: number; // epoch ms
};
