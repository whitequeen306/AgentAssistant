import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import type { ChatItem } from "@/types";

export interface StartChip {
  /** Sent as-is when clicked. */
  message?: string;
  /** Prefilled into the input instead of sending (user completes the topic). */
  draft?: string;
  label: string;
}

/**
 * Dynamic start chips for empty conversations — CONCRETE, generated from the
 * user's actual study library (latest note / KB file), never generic filler.
 * A chip that doesn't reference real content is not shown.
 */
export function useStartChips(chat: ChatItem[]): StartChip[] {
  const [chips, setChips] = useState<StartChip[]>([]);

  useEffect(() => {
    if (chat.length > 0) {
      setChips([]);
      return;
    }
    let alive = true;
    void (async () => {
      const built: StartChip[] = [];
      try {
        const notes = await listNotes();
        if (alive && notes.length) {
          const latest = notes[0];
          built.push({
            label: `精读《${latest.title}》`,
            message:
              `请帮我精读学习库笔记《${latest.title}》（文件名 ${latest.filename}，用 read_note 读取）：` +
              "先讲这份笔记在讲什么，再挑 2 个值得深挖的点展开，最后问我 2 个问题检查我的理解。",
          });
          built.push({
            label: `考考我《${latest.title}》`,
            message:
              `基于学习库笔记《${latest.title}》（文件名 ${latest.filename}，用 read_note 读取）` +
              "出 5 道题考我：先出题，我回答后你逐题判分讲解。",
          });
        }
        if (alive && !notes.length) {
          const kb = (await getApi()?.list_knowledge_files?.()) || [];
          if (alive && kb.length) {
            const t = kb[0].title || kb[0].filename;
            built.push({
              label: `复习《${t}》`,
              message:
                `用 search_knowledge 检索《${t}》的内容：先帮我划出重点（3-5 条），` +
                "再出 3 道简答题考我。",
            });
          }
        }
        if (alive) {
          built.push({
            label: "深度调研：…",
            draft: "深度调研：",
          });
        }
        if (alive) setChips(built.slice(0, 4));
      } catch {
        // bridge not ready — chips stay empty, no filler
      }
    })();
    return () => {
      alive = false;
    };
  }, [chat.length]);

  return chips;
}

export function StartChips({
  chips,
  onSend,
  onDraft,
}: {
  chips: StartChip[];
  onSend: (text: string) => void;
  onDraft: (text: string) => void;
}) {
  if (chips.length === 0) return null;
  return (
    <div className="mx-auto mb-1 flex max-w-xl flex-wrap justify-center gap-1.5">
      {chips.map((c) => (
        <button
          key={c.label}
          onClick={() => (c.message ? onSend(c.message) : onDraft(c.draft || ""))}
          className="interactive-morph inline-flex items-center gap-1.5 rounded-pill border border-border bg-surface-elevated px-3 py-1.5 text-xs text-secondary hover:border-accent/60 hover:text-accent-strong"
        >
          <Sparkles className="h-3 w-3 text-accent" />
          {c.label}
        </button>
      ))}
    </div>
  );
}
