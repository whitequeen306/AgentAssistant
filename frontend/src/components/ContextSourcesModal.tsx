import { useEffect, useState } from "react";
import { BookOpen, Database, FileText, FolderOpen } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import { cn } from "@/lib/cn";
import type { ContextAttachment, Note, ResearchSource } from "@/types";

type Step = "main" | "notes";

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
    setPrimary("knowledge");
    setSelectedNotes(new Set());
    setStep("main");
  };

  const selectNone = () => {
    setPrimary("none");
    setSelectedNotes(new Set());
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
        <div className="flex flex-col gap-3">
          <p className="text-sm text-secondary">
            资料库与知识库二选一；本机文件可额外叠加（会弹出系统文件选择框）。
          </p>

          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-tertiary">
              主来源
            </span>
            <button
              type="button"
              disabled={loading}
              onClick={() => void openNotes()}
              className={cn(
                "flex items-center gap-3 rounded-md border px-3 py-3 text-left transition-colors",
                primary === "notes"
                  ? "border-accent bg-accent-soft"
                  : "border-border hover:bg-surface-elevated",
              )}
            >
              <BookOpen className="h-5 w-5 text-accent" />
              <span>
                <span className="block text-md font-medium text-primary">资料库</span>
                <span className="block text-xs text-tertiary">
                  Agent 笔记 · 列表多选后整篇读取
                  {primary === "notes" && selectedNotes.size
                    ? ` · 已选 ${selectedNotes.size}`
                    : ""}
                </span>
              </span>
            </button>
            <button
              type="button"
              disabled={loading}
              onClick={selectKnowledge}
              className={cn(
                "flex items-center gap-3 rounded-md border px-3 py-3 text-left transition-colors",
                primary === "knowledge"
                  ? "border-accent bg-accent-soft"
                  : "border-border hover:bg-surface-elevated",
              )}
            >
              <Database className="h-5 w-5 text-accent" />
              <span>
                <span className="block text-md font-medium text-primary">知识库</span>
                <span className="block text-xs text-tertiary">
                  已入库文件 · 混合检索 + 重排序
                </span>
              </span>
            </button>
            {primary !== "none" && (
              <button
                type="button"
                onClick={selectNone}
                className="self-start text-xs text-tertiary underline-offset-2 hover:text-secondary hover:underline"
              >
                清除主来源
              </button>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-tertiary">
              额外叠加
            </span>
            <button
              type="button"
              disabled={loading}
              onClick={() => void pickFiles()}
              className="flex items-center gap-3 rounded-md border border-border px-3 py-3 text-left transition-colors hover:bg-surface-elevated"
            >
              <FolderOpen className="h-5 w-5 text-accent" />
              <span>
                <span className="block text-md font-medium text-primary">本机文件</span>
                <span className="block text-xs text-tertiary">
                  点击后选择本地 txt / md / json 等
                  {files.length ? ` · 已选 ${files.length}` : ""}
                </span>
              </span>
            </button>
            {files.length > 0 && (
              <ul className="flex max-h-[20vh] flex-col gap-1 overflow-y-auto">
                {files.map((f, i) => (
                  <li
                    key={`${f.path}:${i}`}
                    className="flex items-center justify-between gap-2 rounded-sm bg-surface-sunken px-2 py-1.5 text-xs text-secondary"
                  >
                    <span className="truncate" title={f.path}>
                      {f.title || f.path}
                    </span>
                    <button
                      type="button"
                      className="shrink-0 text-tertiary hover:text-primary"
                      onClick={() => removeFile(i)}
                    >
                      移除
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex justify-end gap-2">
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
            <p className="py-6 text-center text-sm text-tertiary">资料库还没有笔记</p>
          ) : (
            <ul className="flex max-h-[40vh] flex-col gap-1 overflow-y-auto">
              {notes.map((n) => {
                const on = selectedNotes.has(n.filename);
                return (
                  <li key={n.filename}>
                    <button
                      type="button"
                      onClick={() => toggleNote(n.filename)}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-sm px-2 py-2 text-left text-sm transition-colors",
                        on
                          ? "bg-accent-soft text-accent-strong"
                          : "hover:bg-surface-elevated text-primary",
                      )}
                    >
                      <FileText className="h-4 w-4 shrink-0" />
                      <span className="truncate">{n.title || n.filename}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="flex justify-between gap-2">
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
