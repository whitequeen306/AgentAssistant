import { useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, FolderCog, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { getApi } from "@/lib/bridge";
import type { SubagentTaskView } from "@/types";

interface PlanPayload {
  manifest_hash: string;
  moves: number;
  skipped: number;
  destinations: string[];
  operations_preview: { src: string; dst: string }[];
  skips_preview: { src: string; reason: string }[];
  plan_summary: string;
}

/** Extract the latest waiting_user prompt from the task's event tail. */
function latestPlan(task: SubagentTaskView): PlanPayload | null {
  for (let i = task.events.length - 1; i >= 0; i--) {
    const payload = task.events[i].payload as Record<string, unknown>;
    if (payload?.status === "waiting_user" && typeof payload.manifest_hash === "string") {
      return {
        manifest_hash: payload.manifest_hash,
        moves: Number(payload.moves ?? 0),
        skipped: Number(payload.skipped ?? 0),
        destinations: Array.isArray(payload.destinations)
          ? (payload.destinations as string[])
          : [],
        operations_preview: Array.isArray(payload.operations_preview)
          ? (payload.operations_preview as { src: string; dst: string }[])
          : [],
        skips_preview: Array.isArray(payload.skips_preview)
          ? (payload.skips_preview as { src: string; reason: string }[])
          : [],
        plan_summary: typeof payload.plan_summary === "string" ? payload.plan_summary : "",
      };
    }
  }
  return null;
}

/**
 * Inline approval card for an Organizer manifest. Only the two buttons send a
 * decision — clicking elsewhere or pressing Escape does nothing. Buttons lock
 * after submission until the backend pushes the next task state.
 */
export function OrganizerApproval({ task }: { task: SubagentTaskView }) {
  const plan = useMemo(() => latestPlan(task), [task]);
  const [expanded, setExpanded] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");

  if (task.status !== "waiting_user" || !plan) return null;

  const decide = async (approved: boolean) => {
    const api = getApi();
    if (!api || submitted) return;
    setSubmitted(true);
    setError("");
    try {
      const response = await api.respond_organizer_plan(
        task.task_id,
        plan.manifest_hash,
        approved,
      );
      if (!response.ok) {
        setError(response.error || "操作失败");
        setSubmitted(false);
      }
      // On success the backend emits the next status event, which re-renders
      // this row out of waiting_user; the lock stays until then.
    } catch {
      setError("操作失败，请重试");
      setSubmitted(false);
    }
  };

  return (
    <div className="mt-1.5 rounded-md border border-accent/60 bg-accent-soft p-2 pl-2.5 text-xs">
      <div className="flex items-center gap-1.5 text-primary">
        <FolderCog className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="font-medium">
          整理计划待确认：移动 {plan.moves} 个文件
          {plan.skipped > 0 ? `（另有 ${plan.skipped} 个跳过）` : ""}
        </span>
      </div>
      {plan.plan_summary && (
        <div data-selectable className="mt-1 break-words text-[11px] text-secondary">
          {plan.plan_summary}
        </div>
      )}
      {plan.destinations.length > 0 && (
        <div className="mt-1 text-[11px] text-tertiary">
          目标目录：{plan.destinations.join("、")}
        </div>
      )}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="mt-1 flex items-center gap-1 text-[11px] text-secondary hover:text-primary"
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        查看完整清单
      </button>
      {expanded && (
        <div
          data-selectable
          className="mt-1 max-h-48 overflow-auto rounded-sm bg-surface-sunken p-2 text-[11px] leading-relaxed text-tertiary"
        >
          {plan.operations_preview.map((operation, index) => (
            <div key={index} className="break-words">
              {operation.src} → {operation.dst}
            </div>
          ))}
          {plan.skips_preview.length > 0 && (
            <div className="mt-1 text-error/80">
              {plan.skips_preview.map((skip, index) => (
                <div key={index} className="break-words">
                  跳过 {skip.src}：{skip.reason}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {error && <div className="mt-1 text-[11px] text-error">{error}</div>}
      <div className="mt-1.5 flex items-center gap-1.5">
        <button
          type="button"
          disabled={submitted}
          onClick={() => decide(true)}
          className={cn(
            "flex items-center gap-1 rounded-sm border border-success/60 bg-surface px-2 py-0.5 text-[11px] text-success transition-colors",
            submitted ? "cursor-default opacity-50" : "hover:bg-success/10",
          )}
        >
          <Check className="h-3 w-3" />
          批准执行
        </button>
        <button
          type="button"
          disabled={submitted}
          onClick={() => decide(false)}
          className={cn(
            "flex items-center gap-1 rounded-sm border border-error/50 bg-surface px-2 py-0.5 text-[11px] text-error transition-colors",
            submitted ? "cursor-default opacity-50" : "hover:bg-error/10",
          )}
        >
          <X className="h-3 w-3" />
          拒绝
        </button>
        <span className="text-[10px] text-tertiary">批准前不会移动任何文件</span>
      </div>
    </div>
  );
}
