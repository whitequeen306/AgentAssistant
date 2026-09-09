import { resizeCursor, useWindowResize, type ResizeEdge } from "@/lib/resize";

const EDGES: { edge: ResizeEdge; className: string }[] = [
  { edge: "n", className: "left-2 right-2 top-0 h-1.5 cursor-ns-resize" },
  { edge: "s", className: "left-2 right-2 bottom-0 h-1.5 cursor-ns-resize" },
  { edge: "e", className: "top-2 bottom-2 right-0 w-1.5 cursor-ew-resize" },
  { edge: "w", className: "top-2 bottom-2 left-0 w-1.5 cursor-ew-resize" },
  { edge: "ne", className: "right-0 top-0 h-3 w-3 cursor-nesw-resize" },
  { edge: "nw", className: "left-0 top-0 h-3 w-3 cursor-nwse-resize" },
  { edge: "se", className: "right-0 bottom-0 h-3 w-3 cursor-nwse-resize" },
  { edge: "sw", className: "left-0 bottom-0 h-3 w-3 cursor-nesw-resize" },
];

/** Invisible hit targets on the main card edges for free-form resize. */
export function ResizeHandles() {
  const { onResizePointerDown } = useWindowResize();

  return (
    <>
      {EDGES.map(({ edge, className }) => (
        <div
          key={edge}
          data-no-drag
          aria-hidden
          title="拖动调整窗口大小"
          onPointerDown={(e) => onResizePointerDown(edge, e)}
          className={`absolute z-50 touch-none ${className}`}
          style={{ cursor: resizeCursor(edge) }}
        />
      ))}
    </>
  );
}
