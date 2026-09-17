import type { AgentApi, AgentEvent } from "@/types";
import { dispatch, actions } from "@/lib/store";

/** Typed accessor for the pywebview-injected Python API. */
export function getApi(): AgentApi | null {
  return (typeof window !== "undefined" && window.pywebview?.api) || null;
}

/**
 * 预取空会话开场 chips：画像/学习库变更后后台生成并写入后端缓存，
 * 用户打开新会话即秒出。防抖（连续变更只触发一次 LLM）；fire-and-forget。
 */
let prefetchChipsTimer: ReturnType<typeof setTimeout> | null = null;
export function prefetchStartChips(delayMs = 1000): void {
  if (prefetchChipsTimer) clearTimeout(prefetchChipsTimer);
  prefetchChipsTimer = setTimeout(() => {
    prefetchChipsTimer = null;
    getApi()
      ?.generate_start_chips?.()
      .catch(() => {});
  }, delayMs);
}

/**
 * Resolve once pywebview has injected `window.pywebview.api`. pywebview fires
 * a `pywebviewready` event after injection; fall back to polling for the edge
 * case where the event already fired before we attached the listener.
 */
export function waitForReady(): Promise<AgentApi> {
  return new Promise((resolve, reject) => {
    const api = getApi();
    if (api) return resolve(api);
    const cleanup = () => {
      window.removeEventListener("pywebviewready", onReady);
      window.clearInterval(tick);
    };
    const onReady = () => {
      const a = getApi();
      if (a) {
        cleanup();
        resolve(a);
      }
    };
    window.addEventListener("pywebviewready", onReady);
    // Poll fallback — pywebviewready may fire before this listener attaches.
    const t0 = Date.now();
    const tick = window.setInterval(() => {
      const a = getApi();
      if (a) {
        cleanup();
        resolve(a);
      } else if (Date.now() - t0 > 15000) {
        cleanup();
        reject(new Error("pywebview.api not ready within 15s"));
      }
    }, 80);
  });
}

/** Attach the single agent-event listener that fans events into the store. */
export function attachAgentEvents(): void {
  window.addEventListener("agent-event", (e: Event) => {
    const detail = (e as CustomEvent<AgentEvent>).detail;
    if (detail && typeof detail === "object" && "type" in detail) {
      dispatch(detail);
    }
  });
}

/**
 * User-initiated window-state change: swap the React view AND tell Python to
 * resize/move the window. (Edge-snap transitions — Python-initiated — skip
 * this and only sync the view, via the 'state' event in the dispatcher.)
 */
export function goState(state: "main" | "docked-search" | "popup-chat"): void {
  actions.setState(state);
  getApi()?.change_state(state).catch(() => {});
}

/** One-shot bootstrap: wait for the bridge, fetch init data, hydrate the store. */
export async function bootstrap(): Promise<void> {
  try {
    const api = await waitForReady();
    attachAgentEvents();
    const data = await api.get_init_data();
    actions.init(data);
    // Conversation list refresh (Python may have created one just now).
    const convs = await api.list_conversations();
    actions.setConversations(convs, data.active_conversation);
    // 启动预热：后台预生成开场 chips（缓存随进程重启清空）。
    setTimeout(() => prefetchStartChips(0), 2000);
  } catch (err) {
    console.error("AgentAssistant init failed", err);
    // Still mark ready so the shell paints instead of a permanent blank window.
    actions.init({
      settings: {},
      conversations: [],
      active_conversation: null,
      messages: [],
      scenes: [],
      tools: [],
      state: "main",
      model: "",
      version: "0.1.0",
      drag_params: {
        maxDrag: 200,
        settleThreshold: 100,
        mainHeight: 560,
        dockedHeight: 48,
      },
    });
  }
}
