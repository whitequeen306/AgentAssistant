import { useState } from "react";
import { Check, FileDown } from "lucide-react";
import { exportMarkdownDocx, saveNoteFile } from "@/lib/library";
import { cn } from "@/lib/cn";

/**
 * 调研报告的两个落盘入口：「存入资料库」+「导出 Word」。
 *
 * 抽成共享组件是因为调研卡片有两个渲染位置（会话内联步骤 / 子任务卡片），
 * 二者只能显示其一，但按钮必须两边都有。
 */
export function SaveReportButton({
  title,
  content,
  className,
}: {
  title: string;
  content: string;
  className?: string;
}) {
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [docxState, setDocxState] = useState<"idle" | "busy" | "done" | "fail">(
    "idle",
  );

  const save = async () => {
    if (!content.trim() || busy) return;
    setBusy(true);
    try {
      const res = await saveNoteFile(
        `调研：${(title || "深度调研").slice(0, 40)}`,
        content,
        "调研",
      );
      setSaved(!!res.ok);
    } finally {
      setBusy(false);
    }
  };

  const exportWord = async () => {
    if (!content.trim() || docxState === "busy") return;
    setDocxState("busy");
    try {
      const res = await exportMarkdownDocx(
        `调研：${(title || "深度调研").slice(0, 40)}`,
        content,
      );
      setDocxState(res.ok ? "done" : res.cancelled ? "idle" : "fail");
    } catch {
      setDocxState("fail");
    }
  };

  return (
    <div className={cn("mt-1.5 flex items-center gap-1.5", className)}>
      <button
        type="button"
        onClick={() => void save()}
        disabled={saved || busy}
        className={cn(
          "inline-flex items-center gap-1 rounded-pill px-2.5 py-0.5 text-[11px]",
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
      <button
        type="button"
        title="排版化 Word 文档（宽表自动横排）"
        onClick={() => void exportWord()}
        disabled={docxState === "busy"}
        className={cn(
          "inline-flex items-center gap-1 rounded-pill px-2.5 py-0.5 text-[11px]",
          docxState === "done"
            ? "bg-success/15 text-success"
            : docxState === "fail"
              ? "bg-error/10 text-error"
              : "border border-border text-secondary hover:bg-surface-elevated",
        )}
      >
        <FileDown className="h-3 w-3" />
        {docxState === "busy"
          ? "生成中…"
          : docxState === "done"
            ? "已导出 Word"
            : docxState === "fail"
              ? "导出失败，重试"
              : "导出 Word"}
      </button>
    </div>
  );
}
