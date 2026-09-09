import { useSyncExternalStore } from "react";
import { categorizeTool, formatThoughtLabel, stepLabel } from "@/lib/activity";
import type {
  AgentEvent,
  ActivityStep,
  ChatItem,
  Conversation,
  DragParams,
  InitData,
  Message,
  Page,
  Scene,
  Settings,
  SubagentResultView,
  SubagentStatus,
  SubagentTaskView,
  ToolSpec,
  VoiceState,
  WindowState,
} from "@/types";

export interface AppState {
  ready: boolean;
  state: WindowState;
  page: Page;
  settings: Settings;
  conversations: Conversation[];
  activeConv: string | null;
  /** Items rendered in the chat area (messages + tool rows + system lines). */
  chat: ChatItem[];
  /** ids already rendered — S3 dedup guard against duplicate pushes. */
  renderedIds: Set<string>;
  scenes: Scene[];
  tools: ToolSpec[];
  version: string;
  model: string;
  dragParams: DragParams;
  voiceState: VoiceState;
  /** Agent working (thinking / streaming / running a tool) — drives the
   * status dot's amber state, separate from voice listening. */
  thinking: boolean;
  dictating: boolean;
  /** Transient tool/status line under the chat ("正在执行…"). */
  statusText: string | null;
  /** Live dictation: committed base text partials append onto. */
  voiceBase: string;
  /** Pending backend file/text invoke prep (cleared after load). */
  pendingInvoke: { convId: string; kind: "file" | "text" | "scene" } | null;
  /** Framework confirm gate waiting on the user (save_note, kill_process…). */
  confirmRequest: { tool: string; description: string } | null;
  /** Top slide-down toast (perf/briefing reports) — never touches chat items. */
  toast: { id: string; text: string; convId?: string } | null;
  /**
   * Sticky tip under docked-search. Set with confirm_request; cleared only when
   * the user opens the tip / answers the modal — not by backend confirm_cleared
   * (timeout must not yank the tip away before they notice).
   */
  confirmTip: { tool: string; description: string } | null;
  /** Custom work-area maximize on main (not OS Aero maximize). */
  maximized: boolean;
  /** Subagent tasks of the active conversation, by task_id. */
  subagents: Record<string, SubagentTaskView>;
  /** parent_turn_id → chat item id of that turn's "subagents" block. */
  subagentItemByTurn: Record<string, string>;
  /** Operator task currently controlling the desktop (running only). */
  activeOperatorTaskId: string | null;
}

const INITIAL: AppState = {
  ready: false,
  state: "main" as const,
  page: "chat",
  settings: {},
  conversations: [],
  activeConv: null,
  chat: [],
  renderedIds: new Set<string>(),
  scenes: [],
  tools: [],
  version: "0.2.0",
  model: "",
  dragParams: { maxDrag: 200, settleThreshold: 100, mainHeight: 560, dockedHeight: 48 },
  voiceState: "idle",
  thinking: false,
  dictating: false,
  statusText: null,
  voiceBase: "",
  pendingInvoke: null,
  confirmRequest: null,
  confirmTip: null,
  toast: null,
  maximized: false,
  subagents: {},
  subagentItemByTurn: {},
  activeOperatorTaskId: null,
};

let state: AppState = INITIAL;
const listeners = new Set<() => void>();

export function getState(): AppState {
  return state;
}

function set(patch: Partial<AppState> | ((s: AppState) => Partial<AppState>)) {
  const next =
    typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...next };
  for (const l of listeners) l();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** Subscribe to a stable slice; re-renders only when that slice's reference changes. */
export function useStore<T>(selector: (s: AppState) => T): T {
  return useSyncExternalStore(
    subscribe,
    () => selector(state),
    () => selector(INITIAL),
  );
}

const uid = (): string =>
  crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random());

/* ═══════════════════════════════════════════════════════════════
   Actions
   ═══════════════════════════════════════════════════════════════ */

export const actions = {
  /** Hydrate from get_init_data(). */
  init(data: InitData) {
    set({
      ready: true,
      settings: data.settings || {},
      conversations: data.conversations || [],
      activeConv: data.active_conversation || null,
      chat: (data.messages || []).map(toChatMessage),
      renderedIds: new Set((data.messages || []).map((m) => m.id)),
      scenes: data.scenes || [],
      tools: data.tools || [],
      version: data.version || "0.2.0",
      model: data.model || "",
      dragParams: data.drag_params || state.dragParams,
      state: data.state || "main",
    });
    actions.hydrateSubagents(data.subagents || []);
  },

  setState(next: WindowState | string) {
    const normalized = normalizeWindowState(next);
    if (normalized === state.state) return;
    set({ state: normalized });
  },

  setPage(page: Page) {
    set({ page });
  },

  /* ── Chat ── */
  sendMessage(text: string, msgId?: string) {
    const t = (text || "").trim();
    if (!t) return;
    const id = msgId || uid();
    set((s) => ({
      chat: [...s.chat, { id, kind: "message" as const, role: "user", content: t }],
      renderedIds: new Set(s.renderedIds).add(id),
      thinking: true,
      statusText: null,
    }));
    // Show Thinking… in the reply stream immediately (not under the input).
    actions.beginThought();
  },

  addAssistantMessage(content: string, id?: string) {
    const mid = id || uid();
    if (state.renderedIds.has(mid)) return;
    set((s) => ({
      chat: [...s.chat, { id: mid, kind: "message", role: "assistant", content }],
      renderedIds: new Set(s.renderedIds).add(mid),
    }));
  },

  upsertStreaming(chunk: string) {
    actions.endThought();
    set((s) => {
      const last = s.chat[s.chat.length - 1];
      if (last && last.kind === "streaming") {
        const next = s.chat.slice();
        next[next.length - 1] = { ...last, content: last.content + chunk };
        return { chat: next, thinking: true, statusText: null };
      }
      const id = uid();
      return {
        chat: [...s.chat, { id, kind: "streaming", content: chunk }],
        thinking: true,
        statusText: null,
      };
    });
  },

  finalizeStreaming(text: string) {
    actions.endThought();
    set((s) => {
      const last = s.chat[s.chat.length - 1];
      const content = text || (last?.kind === "streaming" ? last.content : "");
      if (last?.kind === "streaming") {
        const id = uid();
        const next = s.chat.slice();
        next[next.length - 1] = { id, kind: "message", role: "assistant", content };
        return {
          chat: next,
          renderedIds: new Set(s.renderedIds).add(id),
          voiceState: "idle" as VoiceState,
          thinking: false,
          statusText: null,
        };
      }
      const id = uid();
      return {
        chat: [...s.chat, { id, kind: "message", role: "assistant", content }],
        renderedIds: new Set(s.renderedIds).add(id),
        voiceState: "idle" as VoiceState,
        thinking: false,
        statusText: null,
      };
    });
  },

  /** Append a step to the current activity group (create group if needed). */
  _appendActivityStep(step: ActivityStep) {
    set((s) => {
      const chat = s.chat.slice();
      const last = chat[chat.length - 1];
      if (last?.kind === "activity") {
        chat[chat.length - 1] = { ...last, steps: [...last.steps, step] };
        return { chat };
      }
      chat.push({ id: uid(), kind: "activity", expanded: false, steps: [step] });
      return { chat };
    });
  },

  beginThought() {
    set((s) => {
      const chat = s.chat.slice();
      let actIdx = -1;
      for (let i = chat.length - 1; i >= 0; i--) {
        if (chat[i].kind === "activity") {
          actIdx = i;
          break;
        }
        if (chat[i].kind === "message" && (chat[i] as { role?: string }).role === "user") {
          break;
        }
        if (chat[i].kind === "message" || chat[i].kind === "streaming") break;
      }

      if (actIdx >= 0) {
        const act = chat[actIdx];
        if (act.kind !== "activity") return {};
        const last = act.steps[act.steps.length - 1];
        if (last?.category === "thought" && last.status === "pending") {
          return {}; // already thinking
        }
        const step: ActivityStep = {
          id: uid(),
          name: "__thought__",
          status: "pending",
          category: "thought",
          label: "Thinking…",
          startedAt: Date.now(),
        };
        chat[actIdx] = { ...act, steps: [...act.steps, step] };
        return { chat };
      }

      const step: ActivityStep = {
        id: uid(),
        name: "__thought__",
        status: "pending",
        category: "thought",
        label: "Thinking…",
        startedAt: Date.now(),
      };
      chat.push({ id: uid(), kind: "activity", expanded: false, steps: [step] });
      return { chat };
    });
  },

  endThought() {
    set((s) => {
      const chat = s.chat.slice();
      for (let i = chat.length - 1; i >= 0; i--) {
        const it = chat[i];
        if (it.kind !== "activity") continue;
        const steps = it.steps.slice();
        for (let j = steps.length - 1; j >= 0; j--) {
          if (steps[j].category === "thought" && steps[j].status === "pending") {
            const started = steps[j].startedAt || Date.now();
            steps[j] = {
              ...steps[j],
              status: "ok",
              label: formatThoughtLabel(started),
            };
            chat[i] = { ...it, steps };
            return { chat };
          }
        }
        break;
      }
      return {};
    });
  },

  /** Append streamed model CoT onto the current pending Thought step. */
  appendThinkingChunk(chunk: string) {
    const text = chunk || "";
    if (!text) return;
    set((s) => {
      const chat = s.chat.slice();
      for (let i = chat.length - 1; i >= 0; i--) {
        const it = chat[i];
        if (it.kind !== "activity") continue;
        const steps = it.steps.slice();
        for (let j = steps.length - 1; j >= 0; j--) {
          if (steps[j].category === "thought" && steps[j].status === "pending") {
            steps[j] = {
              ...steps[j],
              detail: (steps[j].detail || "") + text,
            };
            chat[i] = { ...it, steps };
            return { chat };
          }
        }
        break;
      }
      // No pending thought yet — create one with this chunk.
      const step: ActivityStep = {
        id: uid(),
        name: "__thought__",
        status: "pending",
        category: "thought",
        label: "Thinking…",
        startedAt: Date.now(),
        detail: text,
      };
      const last = chat[chat.length - 1];
      if (last?.kind === "activity") {
        chat[chat.length - 1] = { ...last, steps: [...last.steps, step] };
        return { chat };
      }
      chat.push({ id: uid(), kind: "activity", expanded: false, steps: [step] });
      return { chat };
    });
  },

  addToolRow(name: string, args?: string) {
    actions.endThought();
    const step: ActivityStep = {
      id: uid(),
      name,
      status: "pending",
      args,
      category: categorizeTool(name),
      label: stepLabel(name, args),
    };
    actions._appendActivityStep(step);
  },

  finishToolRow(name: string, ok: boolean, data?: unknown) {
    set((s) => {
      const chat = s.chat.slice();
      for (let i = chat.length - 1; i >= 0; i--) {
        const it = chat[i];
        if (it.kind !== "activity") continue;
        const steps = it.steps.slice();
        for (let j = steps.length - 1; j >= 0; j--) {
          if (steps[j].name === name && steps[j].status === "pending") {
            steps[j] = {
              ...steps[j],
              status: ok ? "ok" : "fail",
              data,
            };
            chat[i] = { ...it, steps };
            return { chat, statusText: null };
          }
        }
        break;
      }
      // No matching pending step — create a finished one-step activity.
      const step: ActivityStep = {
        id: uid(),
        name,
        status: ok ? "ok" : "fail",
        data,
        category: categorizeTool(name),
        label: stepLabel(name),
      };
      return {
        chat: [
          ...chat,
          { id: uid(), kind: "activity", expanded: false, steps: [step] },
        ],
        statusText: null,
      };
    });
  },

  /** Sub-agent live progress: update the latest still-running research step's
   *  rolling status line. One line replaces the previous — visible motion
   *  without a growing list (no noise). */
  updateResearchProgress(label: string, turn: number, maxTurns: number) {
    set((s) => {
      const chat = s.chat.slice();
      for (let i = chat.length - 1; i >= 0; i--) {
        const it = chat[i];
        if (it.kind !== "activity") continue;
        const steps = it.steps.slice();
        for (let j = steps.length - 1; j >= 0; j--) {
          if (steps[j].category === "research" && steps[j].status === "pending") {
            steps[j] = { ...steps[j], progress: { turn, maxTurns, label } };
            chat[i] = { ...it, steps };
            return { chat };
          }
        }
        break;
      }
      return {};
    });
  },

  /** Sequence-safe reducer for persisted subagent task events. */
  applySubagentEvent(ev: Extract<AgentEvent, { type: "subagent_event" }>) {
    if (!ev.task_id || typeof ev.sequence !== "number") return;
    set((s) => {
      const existing = s.subagents[ev.task_id];
      if (existing && ev.sequence <= existing.sequence) return {}; // stale/dup
      const payload = (ev.payload || {}) as Record<string, unknown>;
      const view: SubagentTaskView = existing
        ? { ...existing, events: existing.events.slice() }
        : {
            task_id: ev.task_id,
            conversation_id: ev.conversation_id || s.activeConv || "",
            parent_turn_id: ev.parent_turn_id || "",
            role: ev.role,
            title: "子任务",
            goal: "",
            status: "queued",
            attempt: 1,
            sequence: 0,
            events: [],
            result: null,
          };
      view.sequence = ev.sequence;
      if (typeof payload.title === "string" && payload.title) view.title = payload.title;
      if (typeof payload.goal === "string" && payload.goal) view.goal = payload.goal;
      if (typeof payload.attempt === "number") view.attempt = payload.attempt;
      if (ev.event_type === "status" && typeof payload.status === "string") {
        view.status = payload.status as SubagentStatus;
        if (payload.status === "queued") {
          // Requeue (retry/continue/resume): previous outcome no longer applies.
          view.result = null;
          view.label = undefined;
        }
      }
      if (
        (ev.event_type === "progress" || ev.event_type === "activity") &&
        typeof payload.label === "string" &&
        payload.label
      ) {
        view.label = payload.label;
      }
      if (ev.event_type === "result" && payload.result && typeof payload.result === "object") {
        view.result = payload.result as SubagentResultView;
        const status = (payload.result as SubagentResultView).status;
        if (status) view.status = status;
      }
      view.events = [
        ...view.events,
        {
          event_id: ev.event_id,
          sequence: ev.sequence,
          type: ev.event_type,
          payload,
          timestamp: ev.timestamp,
        },
      ].slice(-50);

      const subagents = { ...s.subagents, [ev.task_id]: view };
      const patch: Partial<AppState> = {
        subagents,
        activeOperatorTaskId: deriveActiveOperator(subagents),
      };

      if (!existing) {
        // First event of this task: mount (or extend) the turn's block.
        const turnKey = view.parent_turn_id || "orphan";
        const itemId = s.subagentItemByTurn[turnKey];
        if (itemId) {
          const chat = s.chat.slice();
          const index = chat.findIndex((item) => item.id === itemId);
          const item = index >= 0 ? chat[index] : undefined;
          if (item && item.kind === "subagents" && !item.taskIds.includes(ev.task_id)) {
            chat[index] = { ...item, taskIds: [...item.taskIds, ev.task_id] };
            patch.chat = chat;
          }
        } else {
          const id = uid();
          patch.chat = [...s.chat, { id, kind: "subagents", taskIds: [ev.task_id] }];
          patch.subagentItemByTurn = { ...s.subagentItemByTurn, [turnKey]: id };
        }
      }
      return patch;
    });
  },

  /** Replace subagent state from persisted views (init / conversation switch). */
  hydrateSubagents(tasks: SubagentTaskView[]) {
    set((s) => {
      if (!tasks || tasks.length === 0) {
        return { subagents: {}, subagentItemByTurn: {}, activeOperatorTaskId: null };
      }
      const subagents: Record<string, SubagentTaskView> = {};
      const byTurn = new Map<string, string[]>();
      for (const task of tasks) {
        const events = task.events || [];
        subagents[task.task_id] = {
          ...task,
          events,
          label: task.label ?? lastEventLabel(events),
        };
        const key = task.parent_turn_id || "orphan";
        byTurn.set(key, [...(byTurn.get(key) || []), task.task_id]);
      }
      const chat = s.chat.slice();
      const subagentItemByTurn: Record<string, string> = {};
      for (const [turn, taskIds] of byTurn) {
        const id = uid();
        subagentItemByTurn[turn] = id;
        chat.push({ id, kind: "subagents", taskIds });
      }
      return {
        subagents,
        subagentItemByTurn,
        chat,
        activeOperatorTaskId: deriveActiveOperator(subagents),
      };
    });
  },

  toggleActivityExpanded(id: string) {
    set((s) => {
      const next = s.chat.slice();
      const idx = next.findIndex((i) => i.id === id);
      if (idx === -1) return {};
      const it = next[idx];
      if (it.kind === "activity") next[idx] = { ...it, expanded: !it.expanded };
      if (it.kind === "tool") next[idx] = { ...it, expanded: !it.expanded };
      return { chat: next };
    });
  },

  /** @deprecated alias */
  toggleToolExpanded(id: string) {
    actions.toggleActivityExpanded(id);
  },

  addSystemLine(text: string, variant: "info" | "error" = "info") {
    const id = uid();
    set((s) => ({ chat: [...s.chat, { id, kind: "system", text, variant }] }));
  },

  showToast(text: string, convId?: string) {
    set({ toast: { id: uid(), text, convId } });
  },

  dismissToast() {
    set({ toast: null });
  },

  setStatus(text: string | null) {
    set({ statusText: text });
  },

  setVoiceState(v: VoiceState) {
    set({ voiceState: v });
  },

  setThinking(v: boolean) {
    set({ thinking: v });
  },

  setDictating(on: boolean) {
    set({ dictating: on });
  },

  setVoiceBase(base: string) {
    set({ voiceBase: base });
  },

  clearChat() {
    set({
      chat: [],
      renderedIds: new Set<string>(),
      statusText: null,
      subagents: {},
      subagentItemByTurn: {},
      activeOperatorTaskId: null,
    });
  },

  loadMessages(messages: Message[]) {
    set({
      chat: messages.map(toChatMessage),
      renderedIds: new Set(messages.map((m) => m.id)),
      statusText: null,
      subagents: {},
      subagentItemByTurn: {},
      activeOperatorTaskId: null,
    });
  },

  /* ── Conversations ── */
  setConversations(list: Conversation[], activeConv?: string | null) {
    set({
      conversations: list,
      ...(activeConv !== undefined ? { activeConv: activeConv ?? null } : {}),
    });
  },

  setPendingInvoke(kind: "file" | "text" | "scene", convId: string) {
    set({ pendingInvoke: { kind, convId } });
  },

  clearPendingInvoke() {
    set({ pendingInvoke: null });
  },

  setConfirmRequest(req: { tool: string; description: string } | null) {
    set({ confirmRequest: req });
  },

  clearConfirmRequest() {
    set({ confirmRequest: null });
  },

  setConfirmTip(tip: { tool: string; description: string } | null) {
    set({ confirmTip: tip });
  },

  clearConfirmTip() {
    set({ confirmTip: null });
  },

  setMaximized(v: boolean) {
    set({ maximized: !!v });
  },

  /* ── Settings ── */
  setSetting(key: string, value: string) {
    set((s) => ({ settings: { ...s.settings, [key]: value } }));
  },

  /* ── Scenes ── */
  setScenes(list: Scene[]) {
    set({ scenes: list });
  },
};

function toChatMessage(m: Message): ChatItem {
  return { id: m.id, kind: "message", role: m.role, content: m.content };
}

/** Latest progress/activity label from a task's event tail. */
function lastEventLabel(events: SubagentTaskView["events"]): string | undefined {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "progress" || event.type === "activity") {
      const label = event.payload?.label;
      if (typeof label === "string" && label) return label;
    }
  }
  return undefined;
}

/** The single running Operator (drives the persistent control banner). */
function deriveActiveOperator(
  subagents: Record<string, SubagentTaskView>,
): string | null {
  for (const task of Object.values(subagents)) {
    if (task.role === "operator" && task.status === "running") return task.task_id;
  }
  return null;
}

/** Collapse legacy morphs (minimized / docked-sliver) into the 3-state model. */
function normalizeWindowState(raw: string): WindowState {
  if (raw === "main" || raw === "docked-search" || raw === "popup-chat") return raw;
  return "docked-search";
}

/* ═══════════════════════════════════════════════════════════════
   Event dispatcher — called by the bridge for each agent-event.
   ═══════════════════════════════════════════════════════════════ */
export function dispatch(ev: AgentEvent) {
  if (ev.conversation_id && ev.conversation_id !== state.activeConv) return;

  switch (ev.type) {
    case "thinking":
      actions.setThinking(true);
      actions.setStatus(null);
      actions.beginThought();
      break;
    case "thinking_chunk":
      actions.setThinking(true);
      actions.appendThinkingChunk(ev.chunk || "");
      break;
    case "tool_call":
      actions.addToolRow(ev.name, ev.arguments);
      actions.setThinking(true);
      actions.setStatus(null);
      break;
    case "tool_result":
      actions.finishToolRow(ev.name, !!ev.ok, ev.data);
      actions.setStatus(null);
      // Next model round will emit thinking → new Thinking… step.
      break;
    case "subagent_progress":
      actions.updateResearchProgress(ev.label || "", ev.turn || 0, ev.max_turns || 0);
      break;
    case "subagent_event":
      actions.applySubagentEvent(ev);
      break;
    case "response_chunk":
      actions.upsertStreaming(ev.chunk || "");
      break;
    case "response":
      actions.finalizeStreaming(ev.text || "");
      // Belt-and-suspenders: never leave the Stop button stuck after a turn.
      actions.setThinking(false);
      break;
    case "error":
      actions.endThought();
      actions.setVoiceState("idle");
      actions.setThinking(false);
      actions.setStatus(null);
      actions.addSystemLine(ev.message || "未知错误", "error");
      break;
    case "confirm_request": {
      const tip = {
        tool: ev.tool || "",
        description: ev.description || "",
      };
      actions.setConfirmRequest(tip);
      // Sticky dock tip — survives confirm_cleared until the user clicks it.
      actions.setConfirmTip(tip);
      // Search pill is too small for the modal; expand to main so the user
      // can Allow/Deny. Tip remains if they dock again before answering.
      if (getState().state === "docked-search") {
        // Lazy import avoids circular init with bridge ↔ store.
        void import("@/lib/bridge").then(({ goState }) => {
          actions.setPage("chat");
          goState("main");
        });
      }
      break;
    }
    case "confirm_cleared":
      // Drop live modal payload only; keep confirmTip until user acknowledges.
      actions.clearConfirmRequest();
      break;
    case "maximized":
      actions.setMaximized(!!ev.maximized);
      break;
    case "voice_state":
      actions.setVoiceState(ev.state);
      break;
    case "stt_result":
      actions.setVoiceState("idle");
      break;
    case "stt_partial":
    case "stt_final":
      // Handled in the PTT hook (needs the active input element ref).
      // Re-dispatch so a global listener can pick it up.
      window.dispatchEvent(new CustomEvent(`stt-${ev.type === "stt_partial" ? "partial" : "final"}`, { detail: ev.text }));
      break;
    case "perf_report":
      actions.showToast(ev.message || "你有新的电脑性能报告请查收", ev.conv_id);
      break;
    case "briefing_report":
      actions.showToast(ev.message || "晨间播报已生成，点击查看", ev.conv_id);
      break;
    case "conversations_changed":
      break; // caller refreshes via api
    case "state":
      // Edge snap decided by Python — sync view only.
      actions.setState(ev.state);
      break;
    case "scene_triggered":
      // System line is added after switch for scene_test; for chat-triggered
      // scenes (no conv_id) show it on the current view.
      if (!ev.conv_id) {
        actions.addSystemLine(`场景「${ev.name}」已触发`, "info");
      }
      break;
    case "scene_test": {
      actions.setConversations(state.conversations, ev.conv_id);
      actions.setPendingInvoke("scene", ev.conv_id);
      actions.setThinking(true);
      break;
    }
    case "status":
      actions.setStatus(ev.text || "");
      break;
    case "file_invoke":
    case "text_invoke": {
      const kind = ev.type === "file_invoke" ? "file" : "text";
      // Light prep: the App-level effect consumes the pending invoke
      // (switch conversation + system line + main view).
      actions.setConversations(state.conversations, ev.conv_id);
      actions.setPendingInvoke(kind, ev.conv_id);
      actions.setThinking(true);
      break;
    }
  }
}
