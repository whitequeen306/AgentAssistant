import { useEffect, useState } from "react";
import { Download, NotebookPen, Pencil, Trash2 } from "lucide-react";
import { IconButton, Button } from "@/components/ui";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { Tooltip } from "@/components/ui/Tooltip";
import { Markdown } from "@/components/Markdown";
import { actions } from "@/lib/store";
import { deleteNote, exportNote, listNotes, readNote, updateNote } from "@/lib/library";
import type { Note } from "@/types";

/** 学习库 · 资料面板：笔记列表 + 查看/编辑（原资料库页迁移）。 */
export function NotesPanel() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [filename, setFilename] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = async () => setNotes(await listNotes());
  useEffect(() => {
    void refresh();
  }, []);

  const openNote = async (n: Note) => {
    const res = await readNote(n.filename);
    if (!res.ok) return;
    setFilename(n.filename);
    setTitle(n.title);
    setContent(res.content || "");
    setEditing(false);
    setOpen(true);
  };

  const save = async () => {
    if (!filename) return;
    setBusy(true);
    try {
      const res = await updateNote(filename, content, title);
      if (!res.ok) return;
      setEditing(false);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (n: Note, e?: { stopPropagation: () => void }) => {
    e?.stopPropagation();
    if (!confirm(`删除笔记「${n.title}」？此操作不可撤销。`)) return;
    setBusy(true);
    try {
      const res = await deleteNote(n.filename);
      if (!res.ok) return;
      if (filename === n.filename) {
        setOpen(false);
        setFilename("");
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  /** 导出笔记到本机（系统另存为对话框）；取消静默，成功/失败提示。 */
  const exportOne = async (n: Note, e?: { stopPropagation: () => void }) => {
    e?.stopPropagation();
    const res = await exportNote(n.filename);
    if (res.ok && res.path) {
      alert(`已导出到：\n${res.path}`);
    } else if (!res.cancelled && res.error) {
      alert("导出失败: " + res.error);
    }
  };

  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {notes.length === 0 ? (
          <EmptyState
            icon={NotebookPen}
            title="还没有学习资料"
            hint="让助手「保存为笔记」、在调研卡片点「存入资料库」，或直接粘贴内容进来。资料可用于练习室出题。"
          />
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-1.5">
            {notes.map((n) => (
              <div
                key={n.filename}
                onClick={() => void openNote(n)}
                className="group flex cursor-pointer items-center gap-3 rounded-md border border-border bg-surface-elevated px-3 py-2.5 hover:bg-surface-sunken"
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent-strong">
                  <NotebookPen className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1 truncate text-base text-primary">
                  {n.title}
                </span>
                <span className="shrink-0 text-xs text-tertiary">
                  {new Date(n.mtime * 1000).toLocaleDateString()}
                </span>
                <div className="flex shrink-0 items-center gap-0.5 opacity-0 group-hover:opacity-100">
                  <Tooltip label="导出到本地">
                    <IconButton
                      aria-label="导出到本地"
                      onClick={(e) => void exportOne(n, e)}
                    >
                      <Download className="h-3.5 w-3.5" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip label="编辑">
                    <IconButton
                      aria-label="编辑"
                      onClick={(e) => {
                        e.stopPropagation();
                        void (async () => {
                          await openNote(n);
                          setEditing(true);
                        })();
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip label="删除">
                    <IconButton aria-label="删除" onClick={(e) => void remove(n, e)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </IconButton>
                  </Tooltip>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <Modal
        open={open}
        onOpenChange={(v) => {
          setOpen(v);
          if (!v) setEditing(false);
        }}
        title={editing ? "编辑笔记" : title}
        className="w-[min(560px,94vw)]"
        footer={
          editing ? (
            <>
              <Button variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
                取消
              </Button>
              <Button onClick={() => void save()} disabled={busy}>
                保存
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="ghost"
                onClick={() => {
                  setOpen(false);
                  actions.setPage("chat");
                }}
              >
                重问
              </Button>
              <Button variant="ghost" onClick={() => setEditing(true)}>
                编辑
              </Button>
              <Button
                variant="ghost"
                onClick={() => void exportOne({ filename, title, mtime: 0, size: 0 })}
                disabled={busy}
              >
                <Download className="h-4 w-4" />
                导出到本地
              </Button>
              <Button
                variant="ghost"
                onClick={() =>
                  filename &&
                  void remove({ filename, title, mtime: 0, size: 0 })
                }
              >
                删除
              </Button>
            </>
          )
        }
      >
        {editing ? (
          <div className="flex flex-col gap-2">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="标题"
              className="rounded-md border border-border bg-surface-sunken px-2 py-1.5 text-sm text-primary outline-none focus:border-accent"
            />
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={14}
              className="min-h-[220px] resize-y rounded-md border border-border bg-surface-sunken px-2 py-1.5 font-mono text-xs text-primary outline-none focus:border-accent"
            />
          </div>
        ) : (
          <div className="max-h-[50vh] min-h-[120px] overflow-y-auto text-sm">
            <Markdown>{content}</Markdown>
          </div>
        )}
      </Modal>
    </>
  );
}
