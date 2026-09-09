import { useCallback } from "react";
import { getApi } from "@/lib/bridge";
import type { Rect } from "@/types";

/** Matches Python ``MAIN_MIN_SIZE`` (sidebar + readable chat column). */
export const MAIN_MIN_W = 880;
export const MAIN_MIN_H = 520;

export type ResizeEdge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

interface ResizeCtx {
  edge: ResizeEdge;
  sx: number;
  sy: number;
  ox: number;
  oy: number;
  ow: number;
  oh: number;
  captured: HTMLElement | null;
  pointerId?: number;
}

let _resize: ResizeCtx | null = null;
let _rafId: number | null = null;
let _starting = false;

const safe = (p: Promise<unknown> | undefined) => {
  if (p && typeof p.catch === "function") p.catch(() => {});
};

const CURSOR: Record<ResizeEdge, string> = {
  n: "ns-resize",
  s: "ns-resize",
  e: "ew-resize",
  w: "ew-resize",
  ne: "nesw-resize",
  sw: "nesw-resize",
  nw: "nwse-resize",
  se: "nwse-resize",
};

export function resizeCursor(edge: ResizeEdge): string {
  return CURSOR[edge];
}

/** Compute next window rect for an edge drag (screen coords). */
export function computeResizeRect(
  edge: ResizeEdge,
  start: Pick<ResizeCtx, "ox" | "oy" | "ow" | "oh" | "sx" | "sy">,
  sx: number,
  sy: number,
  minW = MAIN_MIN_W,
  minH = MAIN_MIN_H,
): { x: number; y: number; w: number; h: number } {
  const dx = sx - start.sx;
  const dy = sy - start.sy;
  let x = start.ox;
  let y = start.oy;
  let w = start.ow;
  let h = start.oh;
  const right = start.ox + start.ow;
  const bottom = start.oy + start.oh;

  if (edge.includes("e")) w = start.ow + dx;
  if (edge.includes("s")) h = start.oh + dy;
  if (edge.includes("w")) {
    w = start.ow - dx;
    x = start.ox + dx;
  }
  if (edge.includes("n")) {
    h = start.oh - dy;
    y = start.oy + dy;
  }

  if (w < minW) {
    if (edge.includes("w")) x = right - minW;
    w = minW;
  }
  if (h < minH) {
    if (edge.includes("n")) y = bottom - minH;
    h = minH;
  }

  return { x, y, w, h };
}

/**
 * Edge-resize for the main window. Uses begin_resize (size unlocked) +
 * live_resize. Handles carry data-no-drag so title-bar move doesn't steal.
 */
export function useWindowResize() {
  const onResizePointerDown = useCallback((edge: ResizeEdge, e: React.PointerEvent) => {
    if (_resize || _starting) return;
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    void startResize(edge, e);
  }, []);

  return { onResizePointerDown };
}

async function startResize(edge: ResizeEdge, e: React.PointerEvent) {
  const api = getApi();
  if (!api || typeof api.begin_resize !== "function") return;
  _starting = true;
  let rect: Rect | undefined;
  try {
    rect = await api.begin_resize();
  } finally {
    _starting = false;
  }
  if (!rect || !rect.w) return;

  _resize = {
    edge,
    sx: e.screenX,
    sy: e.screenY,
    ox: rect.x,
    oy: rect.y,
    ow: rect.w,
    oh: rect.h,
    captured: null,
  };

  if (e.pointerId !== undefined && e.currentTarget instanceof HTMLElement) {
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
      _resize.captured = e.currentTarget;
      _resize.pointerId = e.pointerId;
    } catch {
      /* ignore */
    }
  }

  document.body.style.cursor = CURSOR[edge];
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function onMove(e: PointerEvent) {
  if (!_resize) return;
  if (_rafId) return;
  const x = e.screenX;
  const y = e.screenY;
  _rafId = requestAnimationFrame(() => {
    _rafId = null;
    if (!_resize) return;
    const api = getApi();
    if (!api) return;
    const next = computeResizeRect(_resize.edge, _resize, x, y);
    safe(api.live_resize(next.w, next.h, next.x, next.y));
  });
}

async function onUp() {
  if (!_resize) return;
  window.removeEventListener("pointermove", onMove);
  window.removeEventListener("pointerup", onUp);
  if (_rafId) {
    cancelAnimationFrame(_rafId);
    _rafId = null;
  }
  if (_resize.captured && _resize.pointerId !== undefined) {
    try {
      _resize.captured.releasePointerCapture(_resize.pointerId);
    } catch {
      /* ignore */
    }
  }
  _resize = null;
  document.body.style.cursor = "";
  const api = getApi();
  if (api && typeof api.end_drag === "function") {
    safe(api.end_drag());
  }
}
