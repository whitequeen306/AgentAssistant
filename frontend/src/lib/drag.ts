import { useCallback, useRef } from "react";
import { getApi } from "@/lib/bridge";
import { useStore } from "@/lib/store";
import type { Rect, WindowState } from "@/types";

interface DragCtx {
  sx: number;
  sy: number;
  ox: number;
  oy: number;
  ow: number;
  oh: number;
  state: WindowState;
  captured: HTMLElement | null;
  pointerId?: number;
}

let _drag: DragCtx | null = null;
let _rafId: number | null = null;
let _dragStarting = false;
let _settling = false;

const NO_DRAG = "button, input, textarea, select, a, [data-no-drag]";

const safe = (p: Promise<unknown> | undefined) => {
  if (p && typeof p.catch === "function") p.catch(() => {});
};

/**
 * Live window drag via JS pointer tracking + Python live_resize/live_move.
 *
 * Pure repositioning only — the window follows the cursor. No state morph on
 * drag. docked-search is draggable (centered on enter; user may reposition).
 */
export function useWindowDrag() {
  const state = useStore((s) => s.state);
  const stateRef = useRef(state);
  stateRef.current = state;

  const onPointerDown = useCallback(async (e: React.PointerEvent) => {
    if (_drag || _dragStarting || _settling) return;
    if (e.button !== 0) return;
    if (e.target instanceof Element && e.target.closest(NO_DRAG)) return;
    const api = getApi();
    if (!api) return;
    e.preventDefault();
    _dragStarting = true;
    let rect: Rect | undefined;
    try {
      rect = await api.begin_drag();
    } finally {
      _dragStarting = false;
    }
    if (!rect || !rect.w) return;
    _drag = {
      sx: e.screenX,
      sy: e.screenY,
      ox: rect.x,
      oy: rect.y,
      ow: rect.w,
      oh: rect.h,
      state: stateRef.current,
      captured: null,
    };
    if (e.pointerId !== undefined && e.currentTarget instanceof HTMLElement) {
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
        _drag.captured = e.currentTarget;
        _drag.pointerId = e.pointerId;
      } catch {
        /* ignore */
      }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  return { onPointerDown };
}

function onMove(e: PointerEvent) {
  if (!_drag) return;
  if (_rafId) return;
  const x = e.screenX;
  const y = e.screenY;
  _rafId = requestAnimationFrame(() => {
    _rafId = null;
    if (_drag) apply(x, y);
  });
}

function apply(sx: number, sy: number) {
  if (!_drag) return;
  const api = getApi();
  if (!api) return;
  // Pure reposition — Python live_move keeps size locked at begin_drag and
  // uses Win32 SWP_NOSIZE (no mid-drag resize → no Aero Snap re-enable).
  safe(api.live_move(_drag.ox + (sx - _drag.sx), _drag.oy + (sy - _drag.sy)));
}

async function onUp(_e: PointerEvent) {
  if (!_drag) return;
  window.removeEventListener("pointermove", onMove);
  window.removeEventListener("pointerup", onUp);
  if (_rafId) {
    cancelAnimationFrame(_rafId);
    _rafId = null;
  }
  if (_drag.captured && _drag.pointerId !== undefined && _drag.captured.releasePointerCapture) {
    try {
      _drag.captured.releasePointerCapture(_drag.pointerId);
    } catch {
      /* ignore */
    }
  }
  _drag = null;
  const api = getApi();
  if (!api) return;
  // Release Python-side drag-size lock so later resizes aren't pinned.
  if (typeof api.end_drag === "function") {
    safe(api.end_drag());
  }
}
