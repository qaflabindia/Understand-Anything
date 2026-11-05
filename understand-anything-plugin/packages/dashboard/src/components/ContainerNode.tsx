import { memo } from "react";
import { Handle, Position } from "@xyflow/react";
import type { NodeProps, Node } from "@xyflow/react";
import { getLayerColor } from "./LayerLegend";

export interface ContainerNodeData extends Record<string, unknown> {
  containerId: string;
  name: string;
  childCount: number;
  previewNames: string[];
  strategy: "folder" | "prefix";
  colorIndex: number;
  isExpanded: boolean;
  hasSearchHits: boolean;
  searchHitCount?: number;
  isDiffAffected: boolean;
  isFocusedViaChild: boolean;
  onToggle: (containerId: string) => void;
}

export type ContainerFlowNode = Node<ContainerNodeData, "container">;

function ContainerNodeComponent({ data, width, height }: NodeProps<ContainerFlowNode>) {
  const color = getLayerColor(data.colorIndex);

  const borderColor = data.isDiffAffected
    ? "var(--color-diff-changed)"
    : data.isExpanded || data.isFocusedViaChild
      ? "rgba(212,165,116,0.6)"
      : "rgba(212,165,116,0.25)";
  const borderWidth = data.isExpanded || data.isFocusedViaChild ? 1.5 : 1;

  const labelDimmed = data.name === "~";
  const labelText = labelDimmed ? "(root)" : data.name;

  const handleToggle = (e: React.SyntheticEvent) => {
    e.stopPropagation();
    data.onToggle(data.containerId);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-expanded={data.isExpanded}
      aria-label={`${labelText} container, ${data.childCount} item${data.childCount !== 1 ? "s" : ""}, ${data.isExpanded ? "expanded" : "collapsed"}`}
      className="rounded-xl cursor-pointer transition-all focus:outline-none focus:ring-2 focus:ring-[rgba(212,165,116,0.6)]"
      style={{
        width,
        height,
        background: "rgba(255,255,255,0.02)",
        border: `${borderWidth}px solid ${borderColor}`,
        position: "relative",
      }}
      onClick={handleToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleToggle(e);
        }
      }}
    >
      {/* Without handles React Flow drops every edge between boxes. */}
      <Handle type="target" position={Position.Top} className="!bg-text-muted !w-2 !h-2" />
      <div
        className="flex items-center justify-between font-heading"
        style={{
          padding: data.isExpanded ? "12px 16px" : "16px 16px 8px",
          color: color.label,
          fontSize: data.isExpanded ? 14 : 22,
          gap: 8,
          fontWeight: 400,
        }}
      >
        <span
          className={labelDimmed ? "opacity-50" : ""}
          style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0, overflowWrap: "anywhere" }}
        >
          {data.isExpanded && <span style={{ fontSize: 10 }}>▾</span>}
          {labelText}
          {data.searchHitCount != null && data.searchHitCount > 0 && (
            <span
              className="font-mono"
              style={{
                marginLeft: 6,
                fontSize: 10,
                background: "rgba(212,165,116,0.2)",
                color: "var(--color-gold, #d4a574)",
                padding: "1px 6px",
                borderRadius: 8,
              }}
            >
              {data.searchHitCount} hit{data.searchHitCount !== 1 ? "s" : ""}
            </span>
          )}
        </span>
        <span style={{ color: "#a39787", fontSize: data.isExpanded ? 11 : 14, flexShrink: 0 }}>
          {data.childCount}
        </span>
      </div>
      {!data.isExpanded && (
        <div style={{ padding: "0 16px 14px" }}>
          <ul className="font-mono" style={{ fontSize: 12, lineHeight: 1.6, color: "#a39787" }}>
            {data.previewNames.map((name) => (
              <li key={name} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {name}
              </li>
            ))}
            {data.childCount > data.previewNames.length && (
              <li style={{ opacity: 0.6 }}>+{data.childCount - data.previewNames.length} more · click to open</li>
            )}
          </ul>
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-text-muted !w-2 !h-2" />
    </div>
  );
}

const ContainerNode = memo(ContainerNodeComponent);
ContainerNode.displayName = "ContainerNode";

export default ContainerNode;
