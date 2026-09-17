import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  FolderCog,
  FolderSearch,
  Globe,
  MousePointerClick,
  Pause,
  Play,
  RotateCcw,
  Send,
  Square,
} from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { OrganizerApproval } from "@/components/OrganizerApproval";
import { SaveReportButton } from "@/components/SaveReportButton";
import { SubagentStream } from "@/components/SubagentStream";
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
  const active = isActive(task.status);
  const failed = task.status === "failed" || task.status === "interrupted";
  // 运行中默认展开：让用户实时看到它在搜什么、读到什么，而不是只转一个状态灯。
  // 用户手动收起后不再自动弹开（否则一边看报告一边被顶开，很烦）。
  const [expanded, setExpanded] = useState(active);
  const [userCollapsed, setUserCollapsed] = useState(false);
  // 过程流（思考/工具行）单独控制：运行中常驻，完成后自动收起——
  // 此时用户要的是报告，几十行过程拼在报告上面只会碍事。失败/中断不收，方便排障。
  const [showProcess, setShowProcess] = useState(active);
  const prevActive = useRef(active);

  useEffect(() => {
    if (active && !userCollapsed) {
      setExpanded(true);
      setShowProcess(true);
    }
    if (prevActive.current && !active && task.status === "completed") {
      setShowProcess(false);
    }
    prevActive.current = active;
  }, [active, userCollapsed, task.status]);

  const toggle = () => {
    setExpanded((prev) => {
      const next = !prev;
      setUserCollapsed(!next);
      return next;
    });
  };

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
        onClick={toggle}
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
      {task.label && active && !expanded && (
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
      {expanded && (
        <SubagentDetail
          task={task}
          showProcess={showProcess}
          onToggleProcess={() => setShowProcess((v) => !v)}
        />
      )}
      <SubagentControls task={task} />
    </div>
  );
}

function SubagentDetail({
  task,
  showProcess,
  onToggleProcess,
}: {
  task: SubagentTaskView;
  showProcess: boolean;
  onToggleProcess: () => void;
}) {
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
      {/* 事件流：运行中逐条自上而下出现；完成后默认收起，点「查看过程」再展开 */}
      {task.events.length > 0 && (
        <>
          <button
            type="button"
            onClick={onToggleProcess}
            className="self-start text-[11px] text-tertiary hover:text-secondary"
          >
            {showProcess
              ? "收起过程 ↑"
              : `查看过程（${task.events.length} 条搜索与阅读记录）`}
          </button>
          {showProcess && (
            <SubagentStream events={task.events} active={isActive(task.status)} />
          )}
        </>
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
      {/* 调研报告落库入口。manager 路径下这张卡片是唯一的展示位置，
          存库按钮必须在这里也有（内联卡片会被去重隐藏）。 */}
      {task.role === "researcher" && output && (
        <SaveReportButton title={task.goal || task.title} content={output} />
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
