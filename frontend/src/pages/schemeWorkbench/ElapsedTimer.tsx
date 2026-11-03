import { memo, useEffect, useState } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

// ============================================================
// ✅ P0-6 计时器隔离：实时耗时只让本行组件每秒自更新（React.memo），
//    消除生成期间整页 1Hz 重渲（旧实现：父组件 setNowTick 驱动全页重渲）
// ============================================================
export const ElapsedTimer = memo(function ElapsedTimer({ startTime }: { startTime: number }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((v) => v + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <Text type="secondary" style={{ fontSize: 11, marginLeft: 4, flexShrink: 0 }}>
      {Math.max(0, Math.floor((Date.now() - startTime) / 1000))}s
    </Text>
  );
});

// ============================================================
// 目录标题编号（对齐后端 heading_standard.py 的编号原则）
//   L1: 第X章   L2: N   L3: N.M   L4: N.M.K   L5: （X）
// 编号直接由节点 id（点分路径，如 "1" / "1.1" / "1.1.1"）推导，
// 与后端 renumber_outline / HeadingNumberingGeneratorV2 保持一致，
// 保证目录树与正文/导出编号始终同步。展示时套用、存储仍保留裸标题。
// ============================================================
