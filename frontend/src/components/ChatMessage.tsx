import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, AlertTriangle, Bot, Check, X } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { SubagentList } from "@/components/SubagentList";
import { summarizeActivity } from "@/lib/activity";
import { getApi } from "@/lib/bridge";
import { cn } from "@/lib/cn";
import { actions } from "@/lib/store";
import type { ActivityStep, ChatItem } from "@/types";

function formatData(v: unknown): string {
  try {
    const obj = typeof v === "string" ? JSON.parse(v) : v;
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(v);
  }
}

export function ChatMessage({ item }: { item: ChatItem }) {
  if (item.kind === "message") {
    return <MessageBubble role={item.role} content={item.content} />;
  }
  if (item.kind === "streaming") {
    return (
      <div className="self-start max-w-[85%] animate-[msg-in_0.22s_ease-out]">
        <div
          data-selectable
          className="rounded-lg rounded-bl-[4px] border border-border bg-surface-elevated px-3 py-2 text-primary"
        >
          <span className="whitespace-pre-wrap break-words">{item.content}</span>
          <span className="ml-0.5 inline-block w-[6px] animate-[blink-cursor_0.8s_step-end_infinite] text-accent">
            ▊
          </span>
        </div>
      </div>
    );
  }
  if (item.kind === "activity") {
    return <ActivityBlock item={item} />;
  }
  if (item.kind === "subagents") {
    return <SubagentList taskIds={item.taskIds} />;
  }
  if (item.kind === "tool") {
    return <ToolRow item={item} />;
  }
  return <SystemLine item={item} />;
}

function MessageBubble({ role, content }: { role: string; content: string }) {
  const isUser = role === "user";
  return (
    <div
      className={cn(
        "flex flex-col max-w-[85%] animate-[msg-in_0.22s_ease-out]",
        isUser ? "self-end items-end" : "self-start items-start",
      )}
    >
      <div
        data-selectable
        className={cn(
          "px-3.5 py-2 rounded-lg text-base break-words",
          isUser
            ? "bubble-user text-[var(--color-bubble-user-text)] rounded-br-[4px] whitespace-pre-wrap shadow-[var(--shadow-ambient)]"
            : "rounded-bl-[4px] border border-[var(--color-bubble-assistant-border)] bg-[var(--color-bubble-assistant-bg)] text-primary shadow-[var(--shadow-ambient)] backdrop-blur-md",
        )}
      >
        {isUser ? content : <Markdown>{content}</Markdown>}
      </div>
    </div>
  );
}

function ActivityBlock({
  item,
}: {
  item: Extract<ChatItem, { kind: "activity" }>;
}) {
  const summary = useMemo(() => summarizeActivity(item.steps), [item.steps]);
  const toolSteps = item.steps.filter((s) => s.category !== "thought");
  const pending = item.steps.some((s) => s.status === "pending");
  // Only the last tool paints the header red. One early miss used to keep
  // a 30-step group red for the whole turn, looking like "every tool failed".
  const lastTool = toolSteps[toolSteps.length - 1];
  const failed = !pending && lastTool?.status === "fail";
  const thoughtDetail = useMemo(
    () =>
      item.steps
        .filter((s) => s.category === "thought")
        .map((s) => (s.detail || "").trim())
        .filter(Boolean)
        .join("\n\n"),
    [item.steps],
  );

  // Thought-only: click "Thinking…" / "Thought for Xs" to reveal CoT.
  if (toolSteps.length === 0) {
    const canExpand = !!thoughtDetail;
    return (
      <div className="self-start max-w-[92%] min-w-[10rem] text-sm text-secondary">
        <button
          type="button"
          disabled={!canExpand}
          onClick={() => canExpand && actions.toggleActivityExpanded(item.id)}
          className={cn(
            "flex w-full items-center gap-1.5 rounded-sm px-2 py-1 text-left transition-colors",
            canExpand && "hover:bg-surface-elevated cursor-pointer",
            !canExpand && "cursor-default",
            failed && "text-error",
            pending && !failed && "text-accent",
          )}
        >
          {canExpand ? (
            item.expanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-tertiary" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-tertiary" />
            )
          ) : (
            <span className="inline-block h-3.5 w-3.5 shrink-0" />
          )}
          <span className={cn("truncate text-xs", pending && !failed && "text-shine")}>{summary}</span>
        </button>
        {item.expanded && thoughtDetail && (
          <div
            data-selectable
            className="ml-2 mt-0.5 max-h-56 overflow-auto border-l border-border pl-2.5 pr-1 text-[11px] leading-relaxed text-tertiary whitespace-pre-wrap break-words"
          >
            {thoughtDetail}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="self-start max-w-[92%] min-w-[12rem] text-sm text-secondary">
      <button
        type="button"
        onClick={() => actions.toggleActivityExpanded(item.id)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-sm px-2 py-1 text-left transition-colors hover:bg-surface-elevated",
          failed && "text-error",
          pending && !failed && "text-accent",
        )}
      >
        {item.expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-tertiary" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-tertiary" />
        )}
        <span className={cn("truncate text-xs", pending && !failed && "text-shine")}>{summary}</span>
      </button>
      {item.expanded ? (
        <ul className="ml-2 mt-0.5 flex flex-col gap-0.5 border-l border-border pl-2.5">
          {item.steps.map((step) => (
            <ActivityStepRow key={step.id} step={step} />
          ))}
        </ul>
      ) : (
        // Sub-agent cards stay visible even collapsed — a dispatched research
        // agent is significant enough to surface without a click (Cursor-style).
        <ResearchCards steps={item.steps} />
      )}
    </div>
  );
}

function ResearchCards({ steps }: { steps: ActivityStep[] }) {
  const research = steps.filter((s) => s.category === "research");
  if (research.length === 0) return null;
  return (
    <ul className="ml-2 mt-0.5 flex flex-col gap-1 border-l border-border pl-2.5">
      {research.map((step) => (
        <ActivityStepRow key={step.id} step={step} />
      ))}
    </ul>
  );
}

/** Cursor-style sub-agent block: dispatched deep-research runs get their own
 *  bordered card with status chip; shimmer while running, static when done. */
function ResearchStepRow({ step }: { step: ActivityStep }) {
  const [open, setOpen] = useState(false);
  const pending = step.status === "pending";
  const failed = step.status === "fail";

  const goal = useMemo(() => {
    if (!step.args) return "";
    try {
      const g = JSON.parse(step.args)?.goal;
      if (typeof g !== "string") return "";
      return g.length > 120 ? g.slice(0, 119) + "…" : g;
    } catch {
      return "";
    }
  }, [step.args]);

  const [saved, setSaved] = useState(false);

  const saveReport = async () => {
    const api = getApi();
    if (!api?.save_note_file || !report) return;
    const res = await api.save_note_file(
      `调研：${(goal || "深度调研").slice(0, 40)}`,
      report,
      "调研",
    );
    setSaved(!!res.ok);
  };

  /** Full Markdown report returned by dispatch_research (the deliverable).
   *  On failure the salvaged progress (data.summary) takes its place so the
   *  user can see "进度在哪" directly in the card instead of being asked. */
  const report = useMemo(() => {
    const d = step.data;
    if (d && typeof d === "object") {
      const r = (d as { report?: unknown }).report;
      if (typeof r === "string" && r.trim()) return r;
      if (failed) {
        const s = (d as { summary?: unknown }).summary;
        if (typeof s === "string" && s.trim()) return s;
      }
    }
    return "";
  }, [step.data, failed]);

  const detail = useMemo(() => {
    if (step.data !== undefined && step.data !== null) return formatData(step.data);
    if (step.args) {
      try {
        return JSON.stringify(JSON.parse(step.args), null, 2);
      } catch {
        return step.args;
      }
    }
    return "";
  }, [step.args, step.data]);

  return (
    <li className="text-xs">
      <div
        className={cn(
          "rounded-md border px-2.5 py-1.5",
          failed
            ? "border-error/50 bg-error/5"
            : pending
              ? "border-accent/50 bg-accent-soft"
              : "border-border bg-surface-elevated",
        )}
      >
        <button
          type="button"
          disabled={!detail}
          onClick={() => detail && setOpen((v) => !v)}
          className={cn(
            "flex w-full items-center gap-2 text-left",
            detail ? "cursor-pointer" : "cursor-default",
          )}
        >
          <Bot
            className={cn(
              "h-3.5 w-3.5 shrink-0",
              failed ? "text-error" : pending ? "animate-pulse text-accent" : "text-success",
            )}
          />
          <span className="text-[11px] font-medium uppercase tracking-wide text-tertiary">
            子 Agent · 深度调研
          </span>
          <span
            className={cn(
              "ml-auto shrink-0 text-[11px]",
              failed && "text-error",
              pending && "text-accent text-shine",
              !pending && !failed && "text-success",
            )}
          >
            {pending ? "调研中…" : failed ? "调研失败" : "调研完成"}
          </span>
          {detail &&
            (open ? (
              <ChevronDown className="h-3 w-3 shrink-0 text-tertiary" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0 text-tertiary" />
            ))}
        </button>
        {goal && (
          <div data-selectable className="mt-1 break-words pl-5.5 text-secondary">
            {goal}
          </div>
        )}
        {step.progress && (
          <div
            data-selectable
            title={step.progress.label}
            className={cn(
              "mt-1 truncate pl-5.5 text-[11px]",
              pending ? "text-accent text-shine" : "text-tertiary",
            )}
          >
            {pending
              ? `第 ${step.progress.turn}/${step.progress.maxTurns} 轮 · ${step.progress.label}`
              : `共 ${step.progress.turn} 轮`}
          </div>
        )}
        {/* The report is the deliverable — render it inline as Markdown as
            soon as the run finishes, no click required. Raw JSON stays behind
            the chevron for debugging. */}
        {report && !pending && failed && (
          <div className="mt-1.5 pl-5.5 text-[11px] text-error">
            子 Agent 中断，以下为已 salvaged 的进度：
          </div>
        )}
        {report && !pending && (
          <div
            data-selectable
            className="mt-1.5 max-h-96 overflow-auto rounded-sm border border-border bg-surface-sunken p-2.5 text-[13px] leading-relaxed text-primary"
          >
            <Markdown>{report}</Markdown>
          </div>
        )}
        {report && !pending && (
          <button
            type="button"
            onClick={() => void saveReport()}
            disabled={saved}
            className={cn(
              "mt-1.5 inline-flex items-center gap-1 rounded-pill px-2.5 py-0.5 text-[11px]",
              saved
                ? "bg-success/15 text-success"
                : "bg-accent-soft text-accent-strong hover:brightness-105",
            )}
          >
            {saved ? (
              <>
                <Check className="h-3 w-3" />已存入资料库
              </>
            ) : (
              "存入资料库"
            )}
          </button>
        )}
        {open && detail && (
          <pre
            data-selectable
            className="mt-1 max-h-56 overflow-auto rounded-sm bg-surface-sunken p-2 text-[11px] leading-relaxed text-tertiary whitespace-pre-wrap break-words"
          >
            {detail}
          </pre>
        )}
      </div>
    </li>
  );
}

function ActivityStepRow({ step }: { step: ActivityStep }) {
  if (step.category === "research") {
    return <ResearchStepRow step={step} />;
  }
  return <PlainStepRow step={step} />;
}

function PlainStepRow({ step }: { step: ActivityStep }) {
  const [open, setOpen] = useState(false);
  const detail = useMemo(() => {
    if (step.category === "thought" && step.detail) return step.detail;
    if (step.data !== undefined && step.data !== null) return formatData(step.data);
    if (step.args) {
      try {
        return JSON.stringify(JSON.parse(step.args), null, 2);
      } catch {
        return step.args;
      }
    }
    return "";
  }, [step.args, step.data, step.detail, step.category]);

  return (
    <li className="text-xs">
      <button
        type="button"
        className={cn(
          "flex w-full items-start gap-1.5 rounded-sm px-1 py-0.5 text-left hover:bg-surface-elevated",
          step.status === "fail" && "text-error",
          step.status === "pending" && "text-accent",
          step.status === "ok" && "text-secondary",
          !detail && "cursor-default hover:bg-transparent",
        )}
        onClick={() => detail && setOpen((v) => !v)}
        disabled={!detail}
      >
        <StepIcon status={step.status} category={step.category} />
        <span
          className={cn(
            "min-w-0 flex-1 break-words",
            step.category === "thought" && step.status === "pending" && "text-accent",
            step.status === "pending" && "text-shine",
          )}
        >
          {step.label}
        </span>
        {detail && step.category === "thought" && (
          open ? (
            <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-tertiary" />
          ) : (
            <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-tertiary" />
          )
        )}
      </button>
      {open && detail && (
        <pre
          data-selectable
          className="mt-0.5 max-h-56 overflow-auto rounded-sm bg-surface-sunken p-2 text-[11px] leading-relaxed text-tertiary whitespace-pre-wrap break-words"
        >
          {detail}
        </pre>
      )}
    </li>
  );
}

function StepIcon({
  status,
  category,
}: {
  status: ActivityStep["status"];
  category?: string;
}) {
  if (category === "thought") {
    // Thought rows are status text — no checkmark clutter.
    return <span className="mt-0.5 inline-block h-3 w-3 shrink-0" />;
  }
  if (status === "fail") return <X className="mt-0.5 h-3 w-3 shrink-0" />;
  if (status === "ok") return <Check className="mt-0.5 h-3 w-3 shrink-0 text-success" />;
  return <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 animate-pulse" />;
}

function ToolRow({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const failed = item.status === "fail";
  const done = item.status !== "pending";
  const dataText = useMemo(
    () => (item.data !== undefined && item.data !== null ? formatData(item.data) : item.args || ""),
    [item.data, item.args],
  );
  return (
    <div
      className={cn(
        "self-start max-w-[90%] font-mono text-xs rounded-md",
        item.args || dataText ? "cursor-pointer" : "",
        failed ? "text-error" : done ? "text-success" : "text-accent",
      )}
      onClick={() => (item.args || dataText) && actions.toggleToolExpanded(item.id)}
    >
      <div className="flex items-center gap-1.5 rounded-md bg-[var(--color-surface-sunken)] px-2.5 py-1">
        {failed ? (
          <X className="h-3 w-3" />
        ) : done ? (
          <Check className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3 animate-pulse" />
        )}
        <span className={cn(!done && !failed && "text-shine")}>{item.name}</span>
      </div>
      {item.expanded && dataText && (
        <pre
          data-selectable
          className="mx-2 mt-1 mb-1 max-h-40 overflow-auto rounded-sm bg-surface-sunken p-2 text-secondary whitespace-pre-wrap break-all"
        >
          {dataText}
        </pre>
      )}
    </div>
  );
}

function SystemLine({ item }: { item: Extract<ChatItem, { kind: "system" }> }) {
  const error = item.variant === "error";
  return (
    <div
      data-selectable
      className={cn(
        "self-center mx-auto my-1 inline-flex items-center gap-1 rounded-pill px-3 py-1 text-center text-xs",
        error
          ? "bg-[var(--color-error-soft)] text-error"
          : "bg-[var(--color-surface-sunken)] text-tertiary",
      )}
    >
      {error && <AlertTriangle className="h-3 w-3 shrink-0" />}
      {item.text}
    </div>
  );
}
