import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Globe,
  Search,
  StickyNote,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import type { StreamEvent } from "@/types";

export const STREAM_EVENT_TYPES = [
  "thinking",
  "tool_call",
  "tool_result",
  "say",
  "notice",
] as const;

const TOOL_ICONS: Record<string, LucideIcon> = {
  web_search: Search,
  read_page: FileText,
  extract_content: FileText,
  read_local_source: FileText,
  read_attached_source: FileText,
  save_note: StickyNote,
  search_papers: BookOpen,
  search_knowledge: BookOpen,
};

const STATUS_LABELS: Record<string, string> = {
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

/** 兜底行：老通道的 progress / 未知类型，以及状态变更。 */
function PlainLine({
  type,
  payload,
}: {
  type: string;
  payload: Record<string, unknown>;
}) {
  if (type === "status") {
    const status = String(payload.status || "");
    return (
      <span className="text-tertiary">
        状态 → {STATUS_LABELS[status] ?? status}
      </span>
    );
  }
  if (type === "result") {
    const result = payload.result as { summary?: string } | undefined;
    return <span className="text-tertiary">结果：{result?.summary || "已返回"}</span>;
  }
  const label = typeof payload.label === "string" ? payload.label : type;
  const turn = payload.turn;
  const prefix = typeof turn === "number" ? `第 ${turn} 轮 · ` : "";
  return (
    <span className="text-tertiary">
      {prefix}
      {label}
    </span>
  );
}

/** 单条「思考」——默认展开可读，能收起；长文限高内滚。 */
function ThinkingRow({ text }: { text: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="rounded-sm border-l-2 border-border-strong bg-surface-sunken px-2.5 py-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-tertiary hover:text-secondary"
      >
        <Brain className="h-3 w-3 shrink-0" />
        <span>思考</span>
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
      </button>
      {open && (
        <p
          data-selectable
          className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] italic leading-relaxed text-tertiary"
        >
          {text}
        </p>
      )}
    </div>
  );
}

/** 一次工具调用 + 它的结果，配成一行（避免「开始搜/搜完了」两行噪声）。 */
function ToolRow({
  payload,
  result,
}: {
  payload: Record<string, unknown>;
  result?: Record<string, unknown>;
}) {
  const tool = String(payload.tool || "");
  const Icon = TOOL_ICONS[tool] ?? Globe;
  const label = typeof payload.label === "string" ? payload.label : tool;
  const args = (payload.args || {}) as Record<string, string>;
  const argValue = Object.values(args)[0] || "";
  const ok = result ? Boolean(result.ok) : undefined;
  const count = typeof result?.count === "number" ? result.count : undefined;
  const sources = Array.isArray(result?.sources) ? (result?.sources as string[]) : [];

  return (
    <div className="flex flex-col gap-0.5 rounded-sm px-1 py-0.5">
      <div className="flex items-start gap-1.5">
        <Icon className="mt-[3px] h-3 w-3 shrink-0 text-accent" />
        <span
          data-selectable
          className="min-w-0 flex-1 break-words text-[12px] text-secondary"
        >
          {label}
        </span>
        {ok === true && (
          <span className="flex shrink-0 items-center gap-1 text-[11px] text-success">
            <CheckCircle2 className="h-3 w-3" />
            {count ? `${count} 条` : "完成"}
          </span>
        )}
        {ok === false && (
          <span className="flex shrink-0 items-center gap-1 text-[11px] text-error">
            <XCircle className="h-3 w-3" />
            失败
          </span>
        )}
      </div>
      {argValue && !label.includes(argValue.slice(0, 20)) && (
        <span
          data-selectable
          className="break-all pl-[18px] text-[10px] leading-relaxed text-tertiary"
        >
          {argValue}
        </span>
      )}
      {ok === false && typeof result?.text === "string" && result.text && (
        <span
          data-selectable
          className="break-words pl-[18px] text-[10px] leading-relaxed text-error"
        >
          {result.text}
        </span>
      )}
      {sources.slice(0, 3).map((url) => (
        <span
          key={url}
          data-selectable
          title={url}
          className="truncate pl-[18px] text-[10px] text-tertiary"
        >
          {url.replace(/^https?:\/\//, "")}
        </span>
      ))}
    </div>
  );
}

/**
 * 子任务 / 调研的实时事件流：像主会话一样自上而下逐条冒出来。
 *
 * 两种卡片共用（ChatMessage 的内联调研步骤 + SubagentList 的任务行），
 * 保证用户看哪一张都是同一个流。
 */
export function SubagentStream({
  events,
  active,
  className,
}: {
  events: StreamEvent[];
  active: boolean;
  className?: string;
}) {
  const boxRef = useRef<HTMLDivElement | null>(null);

  // 新事件到达 → 滚到底
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events.length]);

  const rows = useMemo(() => {
    // 一旦有结构化事件，就不再重复渲染旧的 progress 行
    const hasStructured = events.some((e) =>
      (STREAM_EVENT_TYPES as readonly string[]).includes(e.type),
    );
    const out: { key: string; node: React.ReactNode }[] = [];
    events.forEach((event, index) => {
      const p = event.payload || {};
      switch (event.type) {
        case "thinking": {
          const text = typeof p.text === "string" ? p.text : "";
          if (text) out.push({ key: event.event_id, node: <ThinkingRow text={text} /> });
          break;
        }
        case "say": {
          const text = typeof p.text === "string" ? p.text : "";
          if (text)
            out.push({
              key: event.event_id,
              node: (
                <div
                  data-selectable
                  className="rounded-sm bg-surface-sunken px-2.5 py-1.5 text-[12px] leading-relaxed text-secondary"
                >
                  <Markdown>{text}</Markdown>
                </div>
              ),
            });
          break;
        }
        case "tool_call": {
          // 向后找同一 tool 最近的 tool_result，配对成一行
          let result: Record<string, unknown> | undefined;
          for (let i = index + 1; i < events.length; i++) {
            const nxt = events[i];
            if (nxt.type === "tool_call") break;
            if (nxt.type === "tool_result" && nxt.payload?.tool === p.tool) {
              result = nxt.payload as Record<string, unknown>;
            }
          }
          out.push({ key: event.event_id, node: <ToolRow payload={p} result={result} /> });
          break;
        }
        case "tool_result":
          // 没有前置 tool_call 的结果（异常路径）单独成行
          if (!events.slice(0, index).some((e) => e.type === "tool_call")) {
            out.push({
              key: event.event_id,
              node: <ToolRow payload={p} result={p as Record<string, unknown>} />,
            });
          }
          break;
        case "notice": {
          const text = typeof p.text === "string" ? p.text : "";
          if (text)
            out.push({
              key: event.event_id,
              node: (
                <div className="flex items-start gap-1.5 rounded-sm border border-warning/40 bg-warning/10 px-2 py-1 text-[11px] text-warning">
                  <AlertTriangle className="mt-[2px] h-3 w-3 shrink-0" />
                  <span data-selectable className="break-words">
                    {text}
                  </span>
                </div>
              ),
            });
          break;
        }
        case "status":
          break;
        default:
          if (!hasStructured)
            out.push({
              key: event.event_id,
              node: <PlainLine type={event.type} payload={p} />,
            });
      }
    });
    return out;
  }, [events]);

  if (rows.length === 0 && !active) return null;

  return (
    <div
      ref={boxRef}
      className={
        className ??
        "flex max-h-72 flex-col gap-1 overflow-auto rounded-sm border border-border bg-surface p-2"
      }
    >
      {rows.map((row) => (
        <div key={row.key} className="text-[11px] leading-relaxed">
          {row.node}
        </div>
      ))}
      {active && (
        <div className="flex items-center gap-1.5 pt-0.5 text-[11px] text-accent">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
          <span className="text-shine">进行中…</span>
        </div>
      )}
    </div>
  );
}
