import { Checkbox } from "antd";
import { useEffect, useState } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export function OutlineGateList({
  outline,
  selectedRef,
}: {
  /** 一级章节行（OutlineNodeInput 等来源的公共形状：只声明本组件读取的键） */
  outline: { title?: string; description?: string }[];
  selectedRef: { current: number[] };
}) {
  const [checked, setChecked] = useState<string[]>(outline.map((_, i) => String(i)));
  // 同步勾选结果到 ref（modal.confirm onOk 读取，避免闭包陈旧值）
  useEffect(() => {
    selectedRef.current = checked.map(Number);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked]);
  const toggle = (i: number, on: boolean) => {
    setChecked((prev) => (on ? [...prev, String(i)] : prev.filter((x) => x !== String(i))));
  };
  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <Text type="secondary">
          AI 已生成 {outline.length} 个一级章节。取消勾选不需要的章节（其全部子级一并排除）；
          确认保存后一级目录将被锁定。取消勾选过少可能导致方案覆盖不全，请谨慎操作。
        </Text>
      </div>
      <div
        style={{
          maxHeight: 320,
          overflow: "auto",
          border: "1px solid #f0f0f0",
          borderRadius: 6,
          padding: "4px 12px",
        }}
      >
        {outline.map((n, i) => (
          // ✅ R174 F7：key 由纯 index 改为「title+idx」组合键（审计建议口径），
          //    outline 引用变化引起列表重排时降低邻行 DOM 复用错位风险；
          //    勾选状态仍按 index 记录（selectedRef 消费方契约不变）
          <div key={`${i}-${n.title || `__unnamed_${i}`}`} style={{ padding: "4px 0" }}>
            <Checkbox
              checked={checked.includes(String(i))}
              onChange={(e) => toggle(i, e.target.checked)}
            >
              {n.title || `（未命名第 ${i + 1} 章）`}
            </Checkbox>
            {n.description ? (
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 8, whiteSpace: "normal" }}>
                {String(n.description).slice(0, 60)}
              </Text>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}
