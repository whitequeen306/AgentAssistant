/* Shared types — mirror of the Python bridge API (src/agent_assistant/ui/bridge.py). */

/** Three UI morphs only: search pill → compact chat → full main. */
export type WindowState = "main" | "docked-search" | "popup-chat";

export type Page = "chat" | "scenes" | "study" | "settings" | "about";

/* ── 练习室（学习库）── */
export interface PracticeQuestion {
  /** Persisted question id (backend questions table). */
  id?: string;
  type: "choice" | "short" | string;
  question: string;
  options?: string[];
  /** choice: 正确选项索引（"0"-"3"）；short: 参考答案字符串 */
  answer: number | string;
  explain?: string;
  difficulty?: string;
  /** 知识点类别（自定义出题模式）。 */
  category?: string;
}

export interface PracticeQuizResult {
  ok: boolean;
  error?: string;
  raw?: string;
  /** Backend session id — submit / wrong-retry / report need it. */
  session_id?: string;
  title?: string;
  source_title?: string;
  source_kind?: "note" | "kb" | "review" | "wrong" | "custom";
  mode?: "paper" | "card";
  questions?: PracticeQuestion[];
}

/** AI 判分结果（简答题）。score ∈ [0,1]，verdict: right/partial/wrong。 */
export interface PracticeGradeResult {
  ok: boolean;
  error?: string;
  score?: number;
  verdict?: "right" | "partial" | "wrong";
  feedback?: string;
}

export interface PracticeSubmitAnswer {
  question_id: string;
  user_answer: string;
  /** Present = frontend already graded (card mode passthrough). */
  score?: number;
  feedback?: string;
}

export interface PracticeSubmitResult {
  ok: boolean;
  error?: string;
  score_pct?: number;
  correct_count?: number;
  total?: number;
  per_question?: {
    question_id: string;
    score: number;
    is_correct: boolean;
    verdict: "right" | "partial" | "wrong";
    feedback: string;
  }[];
}

export interface PracticeDueInfo {
  ok: boolean;
  error?: string;
  due_count?: number;
  boxes?: Record<string, number>;
}

export interface PracticeSessionSummary {
  session_id: string;
  title: string;
  source_kind: string;
  source_id: string;
  source_title: string;
  mode: string;
  question_count: number;
  correct_count: number | null;
  score_pct: number | null;
  created_at: number;
}

/** 历史会话回看载荷：session 概要 + 题目 + 每题最后一次作答。 */
export interface PracticeSessionDetail {
  ok: boolean;
  error?: string;
  session?: PracticeSessionSummary;
  questions?: PracticeQuestion[];
  attempts?: {
    question_id: string;
    user_answer: string;
    score: number;
    is_correct: boolean;
    feedback: string;
  }[];
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

/** 目标轨道种类（spec）：一种目标怎么处理的只读配置。 */
export interface GoalSpec {
  kind: string;
  label: string;
  /** true = 骨架占位，UI 标灰「敬请期待」 */
  draft: boolean;
  output_fields: string[];
  milestone_count: number;
}

/** 用户的一个具体目标（可多个并存）。 */
export interface GoalTrack {
  track_id: string;
  kind: string;
  kind_label: string;
  title: string;
  status: "active" | "archived" | string;
  cycle_year: number | null;
  config: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

/** 轨道上的一个时间节点（spec 默认值 + 用户覆盖）。 */
export interface GoalMilestone {
  track_id: string;
  key: string;
  label: string;
  /** epoch seconds */
  due_at: number;
  done: boolean;
  note?: string;
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
  /** 到期/临近的目标节点提醒（规划 tab 之外的主动提醒入口）。 */
  due_milestones?: DueMilestone[];
}

/** 一条目标节点提醒（来自 goal_store.due_milestones）。 */
export interface DueMilestone {
  track_id: string;
  track_title: string;
  key: string;
  label: string;
  due_at: number;
  /** 自然日差：0=今天，正数=还有 N 天，负数=已逾期 N 天。 */
  days: number;
  note: string;
}

/* ── Chat item model (unified list rendered in the chat area) ── */

/** One tool step inside a collapsible activity group (Cursor-style). */
/** 子任务的流式事件（thinking / tool_call / tool_result / notice / say）。
 *  SubagentTimelineEvent 与它结构兼容，两种调研卡片共用同一个渲染器。 */
export interface StreamEvent {
  event_id: string;
  type: string;
  payload: Record<string, unknown>;
}

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
  /** 内联调研步骤的实时事件流（与 SubagentList 共用渲染器）。 */
  events?: StreamEvent[];
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
    research?: boolean,
  ): Promise<void>;
  /** 模型（Provider）：按 Key/URL 拉取 OpenAI 兼容 /models 列表。 */
  fetch_provider_models(
    api_key?: string,
    base_url?: string,
  ): Promise<{ ok: boolean; error?: string; models?: string[] }>;
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
  /** 空会话开场 chips：模型根据学习画像 + 学习库生成（带缓存，失败返回空）。 */
  generate_start_chips?(): Promise<{
    ok: boolean;
    chips?: { label: string; message?: string; draft?: string }[];
  }>;
  /** 目标轨道（学习库 · 规划 tab）：可新建的目标种类。 */
  list_goal_specs?(): Promise<{
    ok: boolean;
    error?: string;
    specs?: GoalSpec[];
  }>;
  /** 目标轨道列表；status 为空返回全部。 */
  list_goal_tracks?(status?: string): Promise<{
    ok: boolean;
    error?: string;
    tracks?: GoalTrack[];
  }>;
  create_goal_track?(
    kind: string,
    title?: string,
    cycle_year?: number,
  ): Promise<{ ok: boolean; error?: string; track_id?: string; milestone_count?: number }>;
  update_goal_track?(
    track_id: string,
    title?: string,
    cycle_year?: number,
    status?: string,
  ): Promise<{ ok: boolean; error?: string }>;
  delete_goal_track?(track_id: string): Promise<{ ok: boolean; error?: string }>;
  list_goal_milestones?(track_id: string): Promise<{
    ok: boolean;
    error?: string;
    milestones?: GoalMilestone[];
  }>;
  /** due_date 为 YYYY-MM-DD；传了即钉住，改周期年份不再覆盖。 */
  set_goal_milestone?(
    track_id: string,
    key: string,
    due_date?: string,
    done?: boolean,
  ): Promise<{ ok: boolean; error?: string }>;
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
  /** 学习库：笔记导出为排版化 Word 文档（markdown→docx 程序转换）。 */
  export_note_docx?(
    filename: string,
  ): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }>;
  /** 把 markdown 文本（如调研报告）直接导出为 Word 文档。 */
  export_markdown_docx?(
    title: string,
    content: string,
  ): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }>;
  /** 学习库：前端直接存笔记（调研卡片「存入资料库」按钮）。 */
  save_note_file(
    title: string,
    content: string,
    tag?: string,
  ): Promise<{ ok: boolean; error?: string; filename?: string; path?: string }>;
  /** 练习室：从学习库材料 / 自定义主题 / 本机文件生成结构化测验（不进会话）。 */
  practice_generate(
    source_kind: "note" | "kb" | "custom" | "file",
    source_id: string,
    count?: number,
    qtype?: "choice" | "short" | "mixed",
    mode?: "paper" | "card",
  ): Promise<PracticeQuizResult>;
  /** 练习室（逐题模式）：LLM 判单道简答题（不落库）。 */
  practice_grade_short(
    question_id: string,
    user_answer: string,
  ): Promise<PracticeGradeResult>;
  /** 练习室：交卷（落库 + Leitner 卡片更新）。 */
  practice_submit(
    session_id: string,
    answers: PracticeSubmitAnswer[],
  ): Promise<PracticeSubmitResult>;
  /** 练习室首页：到期复习统计。 */
  practice_due_info(): Promise<PracticeDueInfo>;
  /** 练习室仪表盘：掌握度分布 / 近14天活跃 / 连续打卡 / 薄弱知识点。 */
  practice_dashboard(): Promise<{
    ok: boolean;
    error?: string;
    cards_total?: number;
    mastered?: number;
    due_count?: number;
    boxes?: Record<string, number>;
    attempts_total?: number;
    correct_rate?: number;
    streak_days?: number;
    last_14_days?: { day: string; count: number; correct: number }[];
    weak_spots?: {
      question_id: string;
      question: string;
      category: string;
      attempts: number;
      avg_score: number;
    }[];
  }>;
  /** 练习室：开始今日复习（到期卡片）。 */
  practice_start_review(limit?: number): Promise<PracticeQuizResult>;
  /** 练习室：错题重练。 */
  practice_start_wrong(session_id: string): Promise<PracticeQuizResult>;
  /** 练习室：最近练习历史。 */
  practice_history(
    limit?: number,
  ): Promise<{ ok: boolean; error?: string; sessions?: PracticeSessionSummary[] }>;
  /** 练习室：已提交会话的完整回看载荷（题目+作答+判分）。 */
  practice_session_detail(session_id: string): Promise<PracticeSessionDetail>;
  /** 练习室：练习报告存入资料库。 */
  practice_save_report(
    session_id: string,
  ): Promise<{ ok: boolean; error?: string; filename?: string }>;
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
