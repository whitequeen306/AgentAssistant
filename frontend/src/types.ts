/* Shared types — mirror of the Python bridge API (src/agent_assistant/ui/bridge.py). */

/** Three UI morphs only: search pill → compact chat → full main. */
export type WindowState = "main" | "docked-search" | "popup-chat";

export type Page = "chat" | "scenes" | "study" | "settings" | "about";

/* ── 练习室（学习库）── */
export interface PracticeQuestion {
  type: "choice" | "short" | string;
  question: string;
  options?: string[];
  /** choice: 正确选项索引；short: 参考答案字符串 */
  answer: number | string;
  explain?: string;
}

export interface PracticeQuizResult {
  ok: boolean;
  error?: string;
  raw?: string;
  title?: string;
  source_title?: string;
  source_kind?: "note" | "kb";
  questions?: PracticeQuestion[];
}

export type ThemePref = "system" | "light" | "dark";

export type VoiceState = "idle" | "listening" | "speaking";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Conversation {
  id: string;
  title: string;
  pinned?: boolean;
  created_at?: string;
}

export type MessageRole = "user" | "assistant" | "system" | "tool";

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
}

export interface ParamSpec {
  name: string;
  /** Chinese UI label when provided by bridge. */
  label?: string;
  type: string;
  description: string;
  required: boolean;
}

export interface ToolSpec {
  name: string;
  /** Chinese UI label when provided by bridge. */
  label?: string;
  description: string;
  requires_confirm: boolean;
  parameters: ParamSpec[];
}

export interface SceneAction {
  tool: string;
  args: Record<string, string>;
}

export interface Scene {
  id?: string;
  name: string;
  trigger_phrases: string[];
  actions: SceneAction[];
  created_at?: string;
}

export interface Note {
  filename: string;
  title: string;
  mtime: number;
  size: number;
}

/** Attached note or local file (direct read). */
export type ResearchSource =
  | { kind: "note"; path: string; title?: string }
  | { kind: "file"; path: string; title?: string };

/** Per-turn context: library XOR knowledge + optional local files. */
export interface ContextAttachment {
  primary: "none" | "notes" | "knowledge";
  notes: ResearchSource[];
  files: ResearchSource[];
}

/** User-uploaded knowledge-base document (vector-indexed). */
export interface KnowledgeFile {
  file_id: string;
  title: string;
  filename: string;
  stored_path: string;
  size: number;
  chunk_count: number;
  created_at: number;
}

export type Settings = Record<string, string>;

export interface DragParams {
  maxDrag: number;
  settleThreshold: number;
  mainHeight: number;
  dockedHeight: number;
}

export interface InitData {
  settings: Settings;
  conversations: Conversation[];
  active_conversation: string | null;
  messages: Message[];
  scenes: Scene[];
  tools: ToolSpec[];
  /** Persisted subagent tasks of the active conversation. */
  subagents?: SubagentTaskView[];
  state: WindowState;
  model: string;
  version: string;
  drag_params: DragParams;
}

/* ── Chat item model (unified list rendered in the chat area) ── */

/** One tool step inside a collapsible activity group (Cursor-style). */
export interface ActivityStep {
  id: string;
  name: string;
  status: "pending" | "ok" | "fail";
  args?: string;
  data?: unknown;
  category: string;
  label: string;
  /** Epoch ms when a Thinking step started (for Thought duration). */
  startedAt?: number;
  /** Model chain-of-thought (Thought / Thinking rows); collapsed by default. */
  detail?: string;
  /** Live rolling status for sub-agent (research) steps while they run. */
  progress?: { turn: number; maxTurns: number; label: string };
}

export type ChatItem =
  | { id: string; kind: "message"; role: MessageRole; content: string }
  | { id: string; kind: "streaming"; content: string }
  | {
      id: string;
      kind: "activity";
      expanded: boolean;
      steps: ActivityStep[];
    }
  | {
      /** Subagent task rows for one parent turn (Cursor-style block). */
      id: string;
      kind: "subagents";
      taskIds: string[];
    }
  | {
      /** @deprecated Prefer kind "activity"; kept for rare legacy rows. */
      id: string;
      kind: "tool";
      name: string;
      status: "pending" | "ok" | "fail";
      args?: string;
      data?: unknown;
      expanded: boolean;
    }
  | {
      id: string;
      kind: "system";
      text: string;
      variant?: "info" | "error";
    };

/* ── Backend → frontend events (CustomEvent('agent-event')) ── */
type TurnIdentity = {
  conversation_id?: string;
  parent_turn_id?: string;
};

export type AgentEvent = TurnIdentity & ({ type: "thinking" } |
  { type: "thinking_chunk"; chunk: string } |
  { type: "tool_call"; name: string; arguments: string } |
  { type: "tool_result"; name: string; ok: boolean; data?: unknown } |
  { type: "response_chunk"; chunk: string } |
  { type: "response"; text: string } |
  { type: "error"; message: string } |
  { type: "voice_state"; state: VoiceState } |
  { type: "stt_result"; text: string } |
  { type: "stt_partial"; text: string } |
  { type: "stt_final"; text: string } |
  { type: "perf_report"; conv_id?: string; title?: string; message?: string } |
  { type: "briefing_report"; conv_id?: string; title?: string; message?: string } |
  { type: "conversations_changed" } |
  { type: "state"; state: WindowState; is_snap?: boolean } |
  { type: "scene_triggered"; name: string; conv_id?: string } |
  { type: "scene_test"; name: string; conv_id: string } |
  { type: "status"; text: string } |
  { type: "file_invoke"; path?: string; name?: string; is_dir?: boolean; conv_id: string } |
  { type: "text_invoke"; text?: string; conv_id: string } |
  { type: "confirm_request"; tool: string; description: string } |
  { type: "confirm_cleared" } |
  { type: "subagent_progress"; tool: string; label: string; turn: number; max_turns: number } |
  {
    type: "subagent_event";
    /** Inner task event kind (renamed from "type" to survive the envelope). */
    event_type: string;
    event_id: string;
    task_id: string;
    sequence: number;
    role: SubagentRole;
    payload: Record<string, unknown>;
    timestamp: string;
  } |
  { type: "maximized"; maximized: boolean });

/* ── Subagent tasks (Cursor-style rows) ── */

export type SubagentRole = "explorer" | "researcher" | "operator" | "organizer";

export type SubagentStatus =
  | "created"
  | "queued"
  | "running"
  | "waiting_user"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface SubagentTimelineEvent {
  event_id: string;
  sequence: number;
  /** Task-level event kind: status | progress | activity | result | … */
  type: string;
  payload: Record<string, unknown>;
  timestamp: string;
}

export interface SubagentResultView {
  task_id: string;
  role: SubagentRole;
  status: SubagentStatus;
  attempt: number;
  summary: string;
  output?: unknown;
  artifacts?: Record<string, unknown>;
  evidence?: { type?: string; ref?: string; claim?: string }[];
  partial_result?: unknown;
  next_action?: string | null;
  stats?: Record<string, unknown>;
  error_category?: string | null;
  error?: string | null;
}

export interface SubagentTaskView {
  task_id: string;
  conversation_id: string;
  parent_turn_id: string;
  role: SubagentRole;
  title: string;
  goal: string;
  status: SubagentStatus;
  attempt: number;
  /** Latest processed event sequence — stale events are ignored. */
  sequence: number;
  /** Latest one-line progress label while running. */
  label?: string;
  events: SubagentTimelineEvent[];
  result?: SubagentResultView | null;
}

/** One tool's permission row for the settings page. */
export interface ToolPermission {
  name: string;
  label: string;
  permission: "auto" | "ask";
}

/* ── Typed mirror of window.pywebview.api ── */
export interface AgentApi {
  get_init_data(): Promise<InitData>;
  get_tools(): Promise<ToolSpec[]>;
  get_tool_permissions(): Promise<ToolPermission[]>;
  set_tool_permission(name: string, mode: string): Promise<{ ok: boolean; error?: string }>;
  send_message(
    text: string,
    msg_id?: string,
    research_sources?: ResearchSource[],
    context_attachment?: ContextAttachment,
  ): Promise<void>;
  stop_generation(): Promise<boolean>;
  pick_local_files(): Promise<string[]>;
  list_knowledge_files(): Promise<KnowledgeFile[]>;
  ingest_knowledge_files(paths?: string[]): Promise<{
    ok: boolean;
    ingested: KnowledgeFile[];
    errors: { path: string; error: string }[];
    cancelled?: boolean;
  }>;
  delete_knowledge_file(file_id: string): Promise<{ ok: boolean; error?: string }>;
  change_state(state: WindowState): Promise<void>;
  begin_drag(): Promise<Rect>;
  begin_resize(): Promise<Rect>;
  end_drag(): Promise<void>;
  live_resize(w: number, h: number, x: number, y: number): Promise<void>;
  live_move(x: number, y: number): Promise<void>;
  check_edge_snap(): Promise<void>;
  grow_to_main(): Promise<void>;
  decide_drag_settle(state: WindowState, drag_delta_y: number): Promise<WindowState>;
  toggle_maximize(): Promise<boolean>;
  is_maximized(): Promise<boolean>;
  quit_app(): Promise<void>;
  respond_confirm(approved: boolean): Promise<void>;
  /** Grow docked-search height so a confirm tip can sit under the pill. */
  set_docked_confirm_banner?(visible: boolean): Promise<void>;
  list_conversations(query?: string): Promise<Conversation[]>;
  new_conversation(): Promise<Conversation>;
  switch_conversation(conv_id: string): Promise<Message[]>;
  rename_conversation(conv_id: string, title: string): Promise<void>;
  delete_conversation(conv_id: string): Promise<void>;
  pin_conversation(conv_id: string, pinned: boolean): Promise<void>;
  list_scenes(): Promise<Scene[]>;
  save_scene(scene: Scene): Promise<Scene>;
  delete_scene(scene_id: string): Promise<void>;
  test_scene(scene_id: string): Promise<void>;
  get_settings(): Promise<Settings>;
  save_setting(key: string, value: string): Promise<void>;
  list_notes(): Promise<Note[]>;
  read_note(filename: string): Promise<{ ok: boolean; content?: string; error?: string }>;
  update_note(
    filename: string,
    content: string,
    title?: string,
  ): Promise<{ ok: boolean; error?: string; filename?: string }>;
  delete_note(filename: string): Promise<{ ok: boolean; error?: string }>;
  /** 学习库：把笔记导出到用户选择的本机位置（系统另存为对话框）。 */
  export_note(
    filename: string,
  ): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }>;
  /** 学习库：前端直接存笔记（调研卡片「存入资料库」按钮）。 */
  save_note_file(
    title: string,
    content: string,
    tag?: string,
  ): Promise<{ ok: boolean; error?: string; filename?: string; path?: string }>;
  /** 练习室：从学习库材料生成结构化测验（后端直连 LLM，不进会话）。 */
  practice_generate(
    source_kind: "note" | "kb",
    source_id: string,
    count?: number,
    qtype?: "choice" | "short" | "mixed",
  ): Promise<PracticeQuizResult>;
  start_dictation(): Promise<boolean>;
  stop_dictation(): Promise<void>;
  list_subagent_tasks(conv_id?: string): Promise<SubagentTaskView[]>;
  control_subagent(
    task_id: string,
    action: "cancel" | "pause" | "resume" | "retry" | "continue",
    instruction?: string,
  ): Promise<{ ok: boolean; error?: string; task?: SubagentTaskView }>;
  respond_organizer_plan(
    task_id: string,
    manifest_hash: string,
    approved: boolean,
  ): Promise<{ ok: boolean; error?: string; error_category?: string }>;
}

declare global {
  interface Window {
    pywebview?: {
      api?: AgentApi;
    };
  }
}
