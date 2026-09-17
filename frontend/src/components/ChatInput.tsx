import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowUp, Mic, Paperclip, Square, Telescope, X } from "lucide-react";
import { ContextSourcesModal } from "@/components/ContextSourcesModal";
import { cn } from "@/lib/cn";
import { getApi } from "@/lib/bridge";
import { actions, getState, useStore } from "@/lib/store";
import { togglePtt } from "@/lib/voice";
import type { ContextAttachment } from "@/types";

// Shared command history across input instances (J16).
let cmdHistory: string[] = [];
let cmdIndex = -1;

const EMPTY_ATT: ContextAttachment = { primary: "none", notes: [], files: [] };

function autosize(el: HTMLTextAreaElement | null) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

function hasAttachment(att: ContextAttachment): boolean {
  return att.primary !== "none" || att.files.length > 0 || att.notes.length > 0;
}

export function ChatInput({
  active,
  placeholder,
  onSend,
  compact = false,
  autoFocus = false,
  className,
  enableContextSources = false,
  startChips,
}: {
  active: boolean; // this input is the live dictation target
  placeholder: string;
  onSend: (
    text: string,
    contextAttachment?: ContextAttachment,
    research?: boolean,
  ) => void;
  compact?: boolean;
  autoFocus?: boolean;
  className?: string;
  /** Show sources picker (资料库 / 知识库 / 本机文件). */
  enableContextSources?: boolean;
  /** Draft chips (above input): click → prefill the textarea. */
  startChips?: { label: string; draft?: string }[];
}) {
  const [value, setValue] = useState("");
  const [attachment, setAttachment] = useState<ContextAttachment>(EMPTY_ATT);
  const [research, setResearch] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const dictating = useStore((s) => s.dictating);
  const thinking = useStore((s) => s.thinking);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const voiceBaseRef = useRef("");

  useEffect(() => {
    if (autoFocus) taRef.current?.focus();
  }, [autoFocus]);

  // Consume live STT partials/finals only when this input is active.
  useEffect(() => {
    if (!active) return;
    const onPartial = (e: Event) => {
      if (!getState().dictating) return;
      setValue(voiceBaseRef.current + ((e as CustomEvent<string>).detail || ""));
    };
    const onFinal = (e: Event) => {
      const text = (e as CustomEvent<string>).detail || "";
      if (!text) return;
      voiceBaseRef.current = (voiceBaseRef.current + text).trimEnd() + " ";
      setValue(voiceBaseRef.current);
      autosize(taRef.current);
      taRef.current?.focus();
    };
    window.addEventListener("stt-partial", onPartial as EventListener);
    window.addEventListener("stt-final", onFinal as EventListener);
    return () => {
      window.removeEventListener("stt-partial", onPartial as EventListener);
      window.removeEventListener("stt-final", onFinal as EventListener);
    };
  }, [active]);

  const send = () => {
    if (thinking) return;
    const t = value.trim();
    if (!t) return;
    cmdHistory.push(t);
    cmdIndex = cmdHistory.length;
    onSend(t, hasAttachment(attachment) ? attachment : undefined, research);
    setValue("");
    setAttachment(EMPTY_ATT);
    voiceBaseRef.current = "";
    requestAnimationFrame(() => autosize(taRef.current));
  };

  const stop = () => {
    // Optimistic clear — don't wait for the backend cancel/response round-trip
    // or the Stop button can stick after the answer is already on screen.
    actions.endThought();
    actions.setThinking(false);
    void getApi()?.stop_generation?.().catch(() => {});
  };

  const togglePttHandler = () => {
    void togglePtt();
  };

  // Snapshot current text as the dictation base the moment dictating turns on,
  // regardless of which control triggered it (input mic or header mic).
  const wasDictating = useRef(false);
  useEffect(() => {
    if (dictating && !wasDictating.current) {
      voiceBaseRef.current = value ? value.trimEnd() + " " : "";
    }
    wasDictating.current = dictating;
  }, [dictating, value]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!thinking) send();
    } else if (e.key === "ArrowUp" && !value) {
      if (cmdIndex > 0) {
        cmdIndex--;
        setValue(cmdHistory[cmdIndex] || "");
        requestAnimationFrame(() => autosize(taRef.current));
      }
    } else if (e.key === "ArrowDown" && cmdIndex < cmdHistory.length) {
      cmdIndex++;
      setValue(cmdHistory[cmdIndex] || "");
      requestAnimationFrame(() => autosize(taRef.current));
    }
  };

  const chips: { key: string; label: string; onRemove: () => void }[] = [];
  if (attachment.primary === "knowledge") {
    chips.push({
      key: "kb",
      label: "知识库",
      onRemove: () => setAttachment((a) => ({ ...a, primary: "none" })),
    });
  }
  if (attachment.primary === "notes") {
    for (let i = 0; i < attachment.notes.length; i++) {
      const n = attachment.notes[i];
      chips.push({
        key: `note:${n.path}`,
        label: `资料库 · ${n.title || n.path}`,
        onRemove: () =>
          setAttachment((a) => {
            const notes = a.notes.filter((_, j) => j !== i);
            return {
              ...a,
              notes,
              primary: notes.length ? "notes" : "none",
            };
          }),
      });
    }
  }
  for (let i = 0; i < attachment.files.length; i++) {
    const f = attachment.files[i];
    chips.push({
      key: `file:${f.path}:${i}`,
      label: `本机 · ${f.title || f.path}`,
      onRemove: () =>
        setAttachment((a) => ({
          ...a,
          files: a.files.filter((_, j) => j !== i),
        })),
    });
  }

  return (
    <div
      className={cn("border-t border-border p-3 backdrop-blur-md", className)}
      style={{
        background:
          "color-mix(in srgb, var(--color-surface-elevated) 50%, transparent)",
      }}
    >
      {enableContextSources && startChips && startChips.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {startChips.map((c) => (
            <button
              key={c.label}
              type="button"
              onClick={() => {
                setValue(c.draft || "");
                requestAnimationFrame(() => {
                  autosize(taRef.current);
                  taRef.current?.focus();
                });
              }}
              className="interactive-morph inline-flex items-center gap-1.5 rounded-pill border border-dashed border-border-strong px-3 py-1 text-xs text-secondary hover:border-accent hover:text-accent-strong"
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
      {enableContextSources && chips.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <span
              key={c.key}
              className="inline-flex max-w-full items-center gap-1 rounded-pill border border-border bg-surface-sunken py-0.5 pl-2.5 pr-1 text-xs text-secondary"
            >
              <span className="truncate">{c.label}</span>
              <button
                type="button"
                onClick={c.onRemove}
                className="shrink-0 text-tertiary hover:text-primary"
                aria-label="移除"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex items-end gap-1.5">
        <textarea
          ref={taRef}
          rows={1}
          value={value}
          placeholder={placeholder}
          onChange={(e) => {
            setValue(e.target.value);
            autosize(taRef.current);
          }}
          onKeyDown={onKeyDown}
          className={cn(
            "flex-1 min-w-0 max-h-[120px] resize-none rounded-lg border border-border bg-surface-sunken px-3.5 py-2 text-base leading-relaxed text-primary shadow-[inset_0_1px_2px_rgba(18,28,52,0.04)] placeholder:text-tertiary outline-none transition-[border-color,box-shadow] duration-[var(--duration-fast)] ease-[var(--ease-standard)] focus:border-accent focus:bg-surface-elevated focus:shadow-[var(--shadow-focus)]",
            compact && "text-sm py-1.5",
          )}
        />
        {enableContextSources && (
          <button
            type="button"
            onClick={() => setPickerOpen(true)}
            title="资料来源：资料库 / 知识库 / 本机文件"
            className={cn(
              "interactive-morph inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill border",
              hasAttachment(attachment)
                ? "border-accent/50 bg-accent-soft text-accent-strong"
                : "border-border bg-transparent text-secondary hover:bg-surface-elevated hover:text-primary",
            )}
          >
            <Paperclip className="h-[18px] w-[18px]" />
          </button>
        )}
        {enableContextSources && (
          <button
            type="button"
            onClick={() => setResearch((v) => !v)}
            title={research ? "深度研究：开（本条消息将派出调研子代理）" : "深度研究：关"}
            aria-pressed={research}
            className={cn(
              "interactive-morph inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill border",
              research
                ? "border-accent/50 bg-accent-soft text-accent-strong"
                : "border-border bg-transparent text-secondary hover:bg-surface-elevated hover:text-primary",
            )}
          >
            <Telescope className="h-[18px] w-[18px]" />
          </button>
        )}
        <button
          type="button"
          onClick={togglePttHandler}
          title="点击录音，再点结束"
          className={cn(
            "interactive-morph inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill border",
            dictating
              ? "border-transparent bg-accent-strong text-accent-contrast animate-[mic-pulse_1.4s_ease-in-out_infinite]"
              : "border-border bg-transparent text-secondary hover:bg-surface-elevated hover:text-primary",
          )}
        >
          <Mic className="h-[18px] w-[18px]" />
        </button>
        {thinking ? (
          <button
            type="button"
            onClick={stop}
            title="停止生成"
            aria-label="停止生成"
            className="interactive-morph inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill bg-accent-strong text-accent-contrast"
          >
            <Square className="h-3 w-3 fill-current" />
          </button>
        ) : (
          <button
            type="button"
            onClick={send}
            title="发送"
            aria-label="发送"
            className="interactive-morph inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill bg-accent-strong text-accent-contrast"
          >
            <ArrowUp className="h-[18px] w-[18px]" />
          </button>
        )}
      </div>
      {enableContextSources && (
        <ContextSourcesModal
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          value={attachment}
          onConfirm={setAttachment}
        />
      )}
    </div>
  );
}
