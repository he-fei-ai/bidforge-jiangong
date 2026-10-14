/**
 * SingleChainFlowChart — 单链流程图统一样式组件（竖向 / 横向双模式）。
 *
 * 来源：《流程图统一样式规范：竖向单链 + 横向单链双模式》§4（2026-10-11 R66 落地）。
 * 视觉规范与后端 PIL 渲染器（mermaid_flowchart.py）及 mermaidRuntime.ts 主题同套：
 *   节点浅紫底 #f0edff + 紫边框 #a89aff + 深灰文字 #333333 + 深灰箭头 #333333；
 *   竖向节点 280×80、横向节点 200×80，间距 40。
 * 适用于「无分支、无判定」的纯线性单链工序流程预览；含判定菱形/分支的流程图
 * 仍走 mermaid.js（MarkdownRenderer）渲染轨。
 *
 * ⚠️ 验证状态：本机 Node 工具链已卸载，本组件按规范代码逐字移植，
 *    构建 / 类型检查 / vitest 留待 Node 环境恢复后补跑。
 */
import React from 'react';

export interface FlowData {
  title?: string;
  steps: string[];
}

interface Props {
  data: FlowData;
  direction?: 'vertical' | 'horizontal';
  nodeWidth?: number;
  nodeHeight?: number;
  gap?: number;
}

export const SingleChainFlowChart: React.FC<Props> = ({
  data,
  direction = 'vertical',
  nodeWidth = direction === 'vertical' ? 280 : 200,
  nodeHeight = 80,
  gap = 40,
}) => {
  const isVertical = direction === 'vertical';

  const containerStyle: React.CSSProperties = {
    display: 'inline-flex',
    flexDirection: isVertical ? 'column' : 'row',
    alignItems: 'center',
    padding: 20,
  };

  const nodeStyle: React.CSSProperties = {
    boxSizing: 'border-box',
    width: nodeWidth,
    minHeight: nodeHeight,
    background: '#f0edff',
    border: '1px solid #a89aff',
    borderRadius: 0,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '16px 24px',
    fontSize: 15,
    color: '#333333',
    letterSpacing: 0.5,
    textAlign: 'center',
    lineHeight: 1.4,
    flexShrink: 0,
    transition: 'box-shadow 0.2s',
  };

  const arrowContainerStyle: React.CSSProperties = {
    width: isVertical ? '100%' : gap,
    height: isVertical ? gap : '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  };

  const renderArrow = () => {
    if (isVertical) {
      return (
        <svg
          width="2"
          height={gap}
          style={{ overflow: 'visible' }}
          xmlns="http://www.w3.org/2000/svg"
        >
          <line
            x1="0"
            y1="0"
            x2="0"
            y2={gap - 10}
            stroke="#333333"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
          <polygon
            points={`0,${gap} -5,${gap - 10} 5,${gap - 10}`}
            fill="#333333"
          />
        </svg>
      );
    } else {
      return (
        <svg
          width={gap}
          height="2"
          style={{ overflow: 'visible' }}
          xmlns="http://www.w3.org/2000/svg"
        >
          <line
            x1="0"
            y1="0"
            x2={gap - 10}
            y2="0"
            stroke="#333333"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
          <polygon
            points={`${gap},0 ${gap - 10},-5 ${gap - 10},5`}
            fill="#333333"
          />
        </svg>
      );
    }
  };

  return (
    <div style={containerStyle}>
      {data.steps.map((text, i) => (
        <React.Fragment key={i}>
          <div
            style={nodeStyle}
            onMouseEnter={e => {
              (e.currentTarget as HTMLDivElement).style.boxShadow =
                '0 2px 8px rgba(168, 154, 255, 0.3)';
            }}
            onMouseLeave={e => {
              (e.currentTarget as HTMLDivElement).style.boxShadow = '';
            }}
          >
            {text}
          </div>

          {i < data.steps.length - 1 && (
            <div style={arrowContainerStyle}>{renderArrow()}</div>
          )}
        </React.Fragment>
      ))}
    </div>
  );
};
