import { useEffect, useState } from "react";
import {
  BookOpen,
  Check,
  Database,
  FileText,
  FolderOpen,
  NotebookPen,
  X,
} from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui";
import { EmptyState } from "@/components/ui/EmptyState";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import { cn } from "@/lib/cn";
import type { ContextAttachment, Note, ResearchSource } from "@/types";

type Step = "main" | "notes";

/** A selectable source card in the study-hub visual language:
 *  icon tile + title/hint + check indicator, lift on hover. */
function SourceCard({
  icon: Icon,
  title,
  hint,
  selected,
  disabled,
  onClick,
}: {
  icon: typeof BookOpen;
  title: string;
  hint: string;
  selected: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "lift flex w-full items-center gap-3 rounded-md border px-3 py-2.5 text-left disabled:opacity-50",
        selected
          ? "border-accent/60 bg-accent-soft"
          : "border-border bg-surface-elevated hover:bg-surface-sunken",
      )}
    >
      <span
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
          selected ? "bg-accent text-white" : "bg-accent-soft text-accent-strong",
        )}
      >
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-md font-medium text-primary">{title}</span>
        <span className="block truncate text-xs text-tertiary">{hint}</span>
      </span>
      {selected && (
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-pill bg-accent text-white">
          <Check className="h-3 w-3" />
        </span>
      )}
    </button>
  );
}

export function ContextSourcesModal({
  open,
  onOpenChange,
  value,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  value: ContextAttachment;
  onConfirm: (att: ContextAttachment) => void;
}) {
  const [step, setStep] = useState<Step>("main");
  const [primary, setPrimary] = useState<ContextAttachment["primary"]>("none");
  const [notes, setNotes] = useState<Note[]>([]);
  const [selectedNotes, setSelectedNotes] = useState<Set<string>>(new Set());
  const [files, setFiles] = useState<ResearchSource[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) {
      setStep("main");
      return;
    }
    setPrimary(value.primary);
    setSelectedNotes(new Set(value.notes.map((n) => n.path)));
    setFiles(value.files);
  }, [open, value]);

  const openNotes = async () => {
    setLoading(true);
    try {
      setNotes(await listNotes());
      setPrimary("notes");
      setStep("notes");
    } finally {
      setLoading(false);
    }
  };

  const pickFiles = async () => {
    setLoading(true);
    try {
      const api = getApi();
      const paths = (await api?.pick_local_files?.()) || [];
      if (!paths.length) return;
      const picked: ResearchSource[] = paths.map((path) => ({
        kind: "file" as const,
        path,
        title: path.replace(/\\/g, "/").split("/").pop() || path,
      }));
      setFiles((prev) => {
        const seen = new Set(prev.map((p) => p.path));
        const next = [...prev];
        for (const p of picked) {
          if (!seen.has(p.path)) next.push(p);
        }
        return next;
      });
    } finally {
      setLoading(false);
    }
  };

  const toggleNote = (filename: string) => {
    setSelectedNotes((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const confirm = () => {
    const noteSources: ResearchSource[] =
      primary === "notes"
        ? notes
            .filter((n) => selectedNotes.has(n.filename))
            .map((n) => ({ kind: "note" as const, path: n.filename, title: n.title }))
        : [];
    if (primary === "notes" && noteSources.length === 0) {
      onConfirm({ primary: "none", notes: [], files });
    } else {
      onConfirm({
        primary: primary === "notes" && noteSources.length === 0 ? "none" : primary,
        notes: noteSources,
        files,
      });
    }
    onOpenChange(false);
  };

  const selectKnowledge = () => {
    setPrimary((p) => (p === "knowledge" ? "none" : "knowledge"));
    setSelectedNotes(new Set());
    setStep("main");
  };

  const removeFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={step === "main" ? "对话资料来源" : "选择资料库笔记"}
    >
      {step === "main" && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-tertiary">
              <span className="h-2.5 w-0.5 rounded-pill bg-accent" />
              主来源（二选一）
            </span>
            <SourceCard
              icon={BookOpen}
              title="资料库"
              hint={
                primary === "notes" && selectedNotes.size
                  ? `已选 ${selectedNotes.size} 篇笔记`
                  : "Agent 笔记 · 多选后整篇读取"
              }
              selected={primary === "notes"}
              disabled={loading}
              onClick={() => void openNotes()}
            />
            <SourceCard
              icon={Database}
              title="知识库"
              hint="已入库文件 · 混合检索 + 重排序"
              selected={primary === "knowledge"}
              disabled={loading}
              onClick={selectKnowledge}
            />
          </div>

          <div className="flex flex-col gap-2">
            <span className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-tertiary">
              <span className="h-2.5 w-0.5 rounded-pill bg-accent" />
              额外叠加本机文件
            </span>
            <SourceCard
              icon={FolderOpen}
              title="选择本机文件"
              hint={
                files.length
                  ? `已选 ${files.length} 个文件`
                  : "txt / md / json / pdf 等，可叠加在主来源上"
              }
              selected={false}
              disabled={loading}
              onClick={() => void pickFiles()}
            />
            {files.length > 0 && (
              <ul className="flex flex-wrap gap-1.5">
                {files.map((f, i) => (
                  <li
                    key={`${f.path}:${i}`}
                    className="inline-flex max-w-full items-center gap-1 rounded-pill border border-border bg-surface-sunken py-0.5 pl-2.5 pr-1 text-xs text-secondary"
                  >
                    <span className="truncate" title={f.path}>
                      {f.title || f.path}
                    </span>
                    <button
                      type="button"
                      aria-label="移除"
                      className="shrink-0 rounded-pill p-0.5 text-tertiary hover:bg-surface-elevated hover:text-error"
                      onClick={() => removeFile(i)}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex justify-end gap-2 border-t border-border pt-3">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button onClick={confirm}>确认</Button>
          </div>
        </div>
      )}

      {step === "notes" && (
        <div className="flex flex-col gap-3">
          {notes.length === 0 ? (
            <EmptyState
              icon={NotebookPen}
              title="资料库还没有笔记"
              hint="让助手「保存为笔记」或在学习库手动添加后，这里就能选它作为对话资料。"
            />
          ) : (
            <ul className="flex max-h-[40vh] flex-col gap-1.5 overflow-y-auto pr-0.5">
              {notes.map((n) => {
                const on = selectedNotes.has(n.filename);
                return (
                  <li key={n.filename}>
                    <button
                      type="button"
                      onClick={() => toggleNote(n.filename)}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-md border px-3 py-2 text-left text-sm",
                        on
                          ? "border-accent/60 bg-accent-soft text-accent-strong"
                          : "border-border bg-surface-elevated text-primary hover:bg-surface-sunken",
                      )}
                    >
                      <span
                        className={cn(
                          "flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
                          on ? "bg-accent text-white" : "bg-accent-soft text-accent-strong",
                        )}
                      >
                        {on ? <Check className="h-3.5 w-3.5" /> : <FileText className="h-3.5 w-3.5" />}
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {n.title || n.filename}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="flex justify-between gap-2 border-t border-border pt-3">
            <Button variant="ghost" onClick={() => setStep("main")}>
              返回
            </Button>
            <Button
              onClick={() => {
                setPrimary(selectedNotes.size ? "notes" : "none");
                setStep("main");
              }}
            >
              完成选择（{selectedNotes.size}）
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
