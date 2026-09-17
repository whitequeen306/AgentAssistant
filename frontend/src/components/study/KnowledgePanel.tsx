import { useEffect, useState } from "react";
import { FileUp, RefreshCw, Trash2, Database } from "lucide-react";
import { IconButton, Button } from "@/components/ui";
import { EmptyState } from "@/components/ui/EmptyState";
import { Tooltip } from "@/components/ui/Tooltip";
import { getApi, prefetchStartChips } from "@/lib/bridge";
import type { KnowledgeFile } from "@/types";

/** 学习库 · 知识库面板：上传向量化 + 文件管理（原知识库页迁移）。 */
export function KnowledgePanel() {
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  const refresh = async () => {
    const api = getApi();
    const list = (await api?.list_knowledge_files?.()) || [];
    setFiles(list);
  };

  useEffect(() => {
    void refresh();
  }, []);

  const upload = async () => {
    const api = getApi();
    if (!api?.ingest_knowledge_files) return;
    setBusy(true);
    setStatus("入库中…");
    try {
      const res = await api.ingest_knowledge_files();
      if (res.cancelled) {
        setStatus("");
        return;
      }
      const n = res.ingested?.length || 0;
      const errN = res.errors?.length || 0;
      setStatus(
        errN
          ? `已入库 ${n} 个，失败 ${errN} 个`
          : n
            ? `已入库 ${n} 个文件`
            : "没有文件入库",
      );
      await refresh();
      // 知识库变更 → 后台预生成开场 chips（防抖）。
      if (n) prefetchStartChips();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (fileId: string) => {
    const api = getApi();
    if (!api?.delete_knowledge_file) return;
    if (!confirm("删除该文件及其向量索引？")) return;
    setBusy(true);
    try {
      await api.delete_knowledge_file(fileId);
      await refresh();
      setStatus("已删除");
      prefetchStartChips();
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="px-4 pt-3">
        <div className="mx-auto flex max-w-2xl items-center justify-between gap-3 rounded-lg border border-border bg-surface-elevated px-3.5 py-3">
          <p className="text-sm leading-relaxed text-secondary">
            上传文本文件后会清洗、切块并向量化入库（按文件隔离）。对话时可启用「知识库」检索，也可在练习室基于它出题。
          </p>
          <Button
            onClick={() => void upload()}
            disabled={busy}
            className="h-8 shrink-0 px-3 py-1.5"
          >
            <FileUp className="h-3.5 w-3.5" />
            上传文件
          </Button>
        </div>
        {status && (
          <p className="mx-auto mt-1.5 max-w-2xl text-xs text-tertiary">{status}</p>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {files.length === 0 ? (
          <EmptyState
            icon={Database}
            title="知识库还是空的"
            hint="点击「上传文件」选择 txt / md / json 等文本文件，入库后对话即可检索、练习室可出题。"
          />
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-1.5">
            {files.map((f) => (
              <div
                key={f.file_id}
                className="group flex items-center gap-3 rounded-md border border-border bg-surface-elevated px-3 py-2.5 hover:bg-surface-sunken"
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent-strong">
                  <Database className="h-4 w-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-base text-primary">
                    {f.title || f.filename}
                  </div>
                  <div className="truncate text-xs text-tertiary">
                    {f.filename} · {f.chunk_count} 块 ·{" "}
                    {new Date(f.created_at * 1000).toLocaleString()}
                  </div>
                </div>
                <Tooltip label="刷新">
                  <IconButton
                    onClick={() => void refresh()}
                    aria-label="刷新"
                    disabled={busy}
                    className="hidden group-hover:inline-flex"
                  >
                    <RefreshCw className="h-4 w-4" />
                  </IconButton>
                </Tooltip>
                <IconButton
                  onClick={() => void remove(f.file_id)}
                  aria-label="删除"
                  disabled={busy}
                  className="opacity-0 group-hover:opacity-100"
                >
                  <Trash2 className="h-4 w-4" />
                </IconButton>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
