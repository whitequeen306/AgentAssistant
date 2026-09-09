import { useState } from "react";
import { MousePointerClick, Pause, Square } from "lucide-react";
import { cn } from "@/lib/cn";
import { getApi } from "@/lib/bridge";
import { useStore } from "@/lib/store";

/**
 * Persistent banner while an Operator subagent controls the desktop.
 * Fixed at the top of the window; buttons act directly (no chat messages,
 * no conversation switching). Disappears when the task pauses or ends.
 */
export function OperatorControlBar() {
  const taskId = useStore((s) => s.activeOperatorTaskId);
  const task = useStore((s) => (s.activeOperatorTaskId ? s.subagents[s.activeOperatorTaskId] : undefined));
  const [busy, setBusy] = useState<"pause" | "cancel" | null>(null);

  if (!taskId || !task) return null;

  const control = async (action: "pause" | "cancel") => {
    const api = getApi();
    if (!api || busy) return;
    setBusy(action);
    try {
      await api.control_subagent(taskId, action);
    } catch {
      /* authoritative state arrives via subagent_event */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      className={cn(
        "pointer-events-auto fixed inset-x-2 top-1 z-50 flex items-center gap-2",
        "rounded-md border border-accent/60 bg-accent-soft px-2.5 py-1.5",
        "shadow-[var(--shadow-ambient)] backdrop-blur-md",
      )}
    >
      <MousePointerClick className="h-3.5 w-3.5 shrink-0 animate-pulse text-accent" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium text-primary">
          子 Agent 正在操作电脑：{task.title}
        </div>
        <div className="truncate text-[10px] text-secondary">
          {task.label || "执行中…"}
          <span className="ml-1 text-tertiary">
            （此期间你的鼠标键盘操作可能改变界面，影响执行）
          </span>
        </div>
      </div>
      <button
        type="button"
        title="暂停（到安全点后让出控制）"
        disabled={busy !== null}
        onClick={() => control("pause")}
        className="flex shrink-0 items-center gap-1 rounded-sm border border-border bg-surface px-1.5 py-0.5 text-[11px] text-secondary transition-colors hover:bg-surface-elevated hover:text-primary disabled:opacity-50"
      >
        <Pause className="h-3 w-3" />
        暂停
      </button>
      <button
        type="button"
        title="停止（进度保留）"
        disabled={busy !== null}
        onClick={() => control("cancel")}
        className="flex shrink-0 items-center gap-1 rounded-sm border border-error/50 bg-surface px-1.5 py-0.5 text-[11px] text-error transition-colors hover:bg-error/10 disabled:opacity-50"
      >
        <Square className="h-3 w-3" />
        停止
      </button>
    </div>
  );
}
