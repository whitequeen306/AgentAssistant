import { useMemo, useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  FolderSearch,
  Globe,
  FolderCog,
  MousePointerClick,
  Pause,
  Play,
  RotateCcw,
  Send,
  Square,
} from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { OrganizerApproval } from "@/components/OrganizerApproval";
import { cn } from "@/lib/cn";
import { getApi } from "@/lib/bridge";
import { useStore } from "@/lib/store";
import type { SubagentRole, SubagentStatus, SubagentTaskView } from "@/types";

const ROLE_LABELS: Record<SubagentRole, string> = {
  explorer: "探索",
  researcher: "调研",
  operator: "操作",
  organizer: "整理",
};

const STATUS_LABELS: Record<SubagentStatus, string> = {
  created: "已创建",
  queued: "排队中",
  running: "运行中",
  waiting_user: "等待确认",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

function RoleIcon({ role, className }: { role: SubagentRole; className?: string }) {
  if (role === "explorer") return <FolderSearch className={className} />;
  if (role === "researcher") return <Globe className={className} />;
  if (role === "operator") return <MousePointerClick className={className} />;
  if (role === "organizer") return <FolderCog className={className} />;
  return <Bot className={className} />;
}

function isActive(status: SubagentStatus): boolean {
  return status === "queued" || status === "running";
}

function statusTone(status: SubagentStatus): string {
  if (status === "failed" || status === "interrupted") return "text-error";
  if (status === "completed") return "text-success";
  if (isActive(status)) return "text-accent";
  return "text-tertiary";
}

/** Cursor-style block: one bordered row per dispatched subagent task. */
export function SubagentList({ taskIds }: { taskIds: string[] }) {
  const subagents = useStore((s) => s.subagents);
  const tasks = taskIds
    .map((id) => subagents[id])
    .filter((task): task is SubagentTaskView => !!task);
  if (tasks.length === 0) return null;
  return (
    <div className="flex w-full max-w-[92%] flex-col gap-1 self-start">
      {tasks.map((task) => (
        <SubagentRow key={task.task_id} task={task} />
      ))}
    </div>
  );
}

function SubagentRow({ task }: { task: SubagentTaskView }) {
  const [expanded, setExpanded] = useState(false);
  const active = isActive(task.status);
  const failed = task.status === "failed" || task.status === "interrupted";

  return (
    <div
      className={cn(
        "rounded-md border px-2.5 py-1.5 text-sm",
        failed
          ? "border-error/50 bg-error/5"
          : active
            ? "border-accent/50 bg-accent-soft"
            : "border-border bg-surface-elevated",
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-tertiary" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-tertiary" />
        )}
        <RoleIcon
          role={task.role}
          className={cn(
            "h-3.5 w-3.5 shrink-0",
            failed ? "text-error" : active ? "animate-pulse text-accent" : "text-success",
          )}
        />
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-primary">
          {task.title}
        </span>
        <span className="shrink-0 text-[10px] uppercase tracking-wide text-tertiary">
          {ROLE_LABELS[task.role] ?? task.role}
          {task.attempt > 1 ? ` · 第${task.attempt}次` : ""}
        </span>
        <span
          className={cn(
            "shrink-0 text-[11px]",
            statusTone(task.status),
            active && "text-shine",
          )}
        >
          {STATUS_LABELS[task.status] ?? task.status}
        </span>
      </button>

      {/* Rolling one-line progress while running (no click needed). */}
      {task.label && active && (
        <div
          data-selectable
          title={task.label}
          className="mt-1 truncate pl-6 text-[11px] text-accent text-shine"
        >
          {task.label}
        </div>
      )}

      {task.role === "organizer" && task.status === "waiting_user" && (
        <OrganizerApproval task={task} />
      )}
      {expanded && <SubagentDetail task={task} />}
      <SubagentControls task={task} />
    </div>
  );
}

function SubagentDetail({ task }: { task: SubagentTaskView }) {
  const output = useMemo(() => {
    const result = task.result;
    if (!result) return "";
    if (typeof result.output === "string" && result.output.trim()) return result.output;
    if (result.summary) return result.summary;
    return "";
  }, [task.result]);

  const partial = useMemo(() => {
    const result = task.result;
    if (!result || !result.partial_result) return "";
    try {
      return JSON.stringify(result.partial_result, null, 2);
    } catch {
      return String(result.partial_result);
    }
  }, [task.result]);

  return (
    <div className="mt-1.5 flex flex-col gap-1.5 pl-6">
      {task.goal && (
        <div data-selectable className="break-words text-[11px] text-secondary">
          {task.goal}
        </div>
      )}
      {/* Activity timeline (persisted events, newest last). */}
      {task.events.length > 0 && (
        <ul className="flex max-h-48 flex-col gap-0.5 overflow-auto border-l border-border pl-2.5">
          {task.events.map((event) => (
            <li key={event.event_id} className="text-[11px] leading-relaxed text-tertiary">
              <TimelineLine type={event.type} payload={event.payload} />
            </li>
          ))}
        </ul>
      )}
      {task.result?.error && (
        <div data-selectable className="text-[11px] text-error">
          {task.result.error}
        </div>
      )}
      {output && (
        <div
          data-selectable
          className="max-h-96 overflow-auto rounded-sm border border-border bg-surface-sunken p-2.5 text-[13px] leading-relaxed text-primary"
        >
          <Markdown>{output}</Markdown>
        </div>
      )}
      {!output && partial && (
        <div className="flex flex-col gap-1">
          <span className="text-[11px] text-error">任务未完成，以下为已保留的进度：</span>
          <pre
            data-selectable
            className="max-h-56 overflow-auto rounded-sm bg-surface-sunken p-2 text-[11px] leading-relaxed text-tertiary whitespace-pre-wrap break-words"
          >
            {partial}
          </pre>
        </div>
      )}
    </div>
  );
}

function TimelineLine({
  type,
  payload,
}: {
  type: string;
  payload: Record<string, unknown>;
}) {
  if (type === "status") {
    const status = String(payload.status || "");
    return <span>状态 → {STATUS_LABELS[status as SubagentStatus] ?? status}</span>;
  }
  if (type === "progress" || type === "activity") {
    const label = typeof payload.label === "string" ? payload.label : "";
    const turn = payload.turn;
    const prefix = typeof turn === "number" ? `第 ${turn} 轮 · ` : "";
    return (
      <span className="break-words">
        {prefix}
        {label || type}
      </span>
    );
  }
  if (type === "result") {
    const result = payload.result as { summary?: string } | undefined;
    return <span className="break-words">结果：{result?.summary || "已返回"}</span>;
  }
  return <span>{type}</span>;
}

function SubagentControls({ task }: { task: SubagentTaskView }) {
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const active = isActive(task.status);
  const canPause = task.status === "running" && task.role === "operator";
  const canResume = task.status === "paused";
  const canRetry =
    task.status === "failed" ||
    task.status === "cancelled" ||
    task.status === "interrupted";
  const canContinue = task.status === "completed" || task.status === "failed";

  const control = async (
    action: "cancel" | "pause" | "resume" | "retry" | "continue",
    text?: string,
  ) => {
    const api = getApi();
    if (!api || busy) return;
    setBusy(true);
    try {
      await api.control_subagent(task.task_id, action, text);
      if (action === "continue") setInstruction("");
    } catch {
      /* backend pushes the authoritative state via events */
    } finally {
      setBusy(false);
    }
  };

  if (!active && !canPause && !canResume && !canRetry && !canContinue) return null;

  return (
    <div className="mt-1.5 flex items-center gap-1.5 pl-6">
      {active && (
        <ControlButton label="停止" onClick={() => control("cancel")} disabled={busy}>
          <Square className="h-3 w-3" />
        </ControlButton>
      )}
      {canPause && (
        <ControlButton label="暂停" onClick={() => control("pause")} disabled={busy}>
          <Pause className="h-3 w-3" />
        </ControlButton>
      )}
      {canResume && (
        <>
          <ControlButton label="继续" onClick={() => control("resume")} disabled={busy}>
            <Play className="h-3 w-3" />
          </ControlButton>
          <ControlButton label="停止" onClick={() => control("cancel")} disabled={busy}>
            <Square className="h-3 w-3" />
          </ControlButton>
        </>
      )}
      {canRetry && (
        <ControlButton label="重试" onClick={() => control("retry")} disabled={busy}>
          <RotateCcw className="h-3 w-3" />
        </ControlButton>
      )}
      {canContinue && (
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <input
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && instruction.trim()) {
                void control("continue", instruction.trim());
              }
            }}
            placeholder="追问这个任务…"
            className="min-w-0 flex-1 rounded-sm border border-border bg-surface px-1.5 py-0.5 text-[11px] text-primary placeholder:text-tertiary focus:outline-none focus:ring-1 focus:ring-accent"
          />
          <ControlButton
            label="发送"
            onClick={() => instruction.trim() && control("continue", instruction.trim())}
            disabled={busy || !instruction.trim()}
          >
            <Send className="h-3 w-3" />
          </ControlButton>
        </div>
      )}
    </div>
  );
}

function ControlButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-[11px] text-secondary transition-colors",
        disabled ? "cursor-default opacity-50" : "hover:bg-surface-elevated hover:text-primary",
      )}
    >
      {children}
      <span>{label}</span>
    </button>
  );
}
