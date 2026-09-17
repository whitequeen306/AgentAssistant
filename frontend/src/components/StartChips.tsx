import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import { useStore } from "@/lib/store";
import type { ChatItem } from "@/types";

export interface StartChip {
  /** Sent as-is when clicked. */
  message?: string;
  /** Prefilled into the input instead of sending (user completes the topic). */
  draft?: string;
  /** Jump into the practice room with this material preselected. */
  practice?: { kind: "note" | "kb"; sourceId: string };
  label: string;
}

/**
 * Composition (4 max): ① 深度调研 draft（画像→具体主题，模型生成）
 * ② 考考我《最新材料》（学习库，本地即时） ③ 画像个性化请求（模型生成）
 * ④ 画像×材料关联（仅当模型判断有关联时生成）。精读 chip 已移除——
 * 笔记已存进资料库，无需再读。
 */
export function useStartChips(chat: ChatItem[]): StartChip[] {
  const [chips, setChips] = useState<StartChip[]>([]);
  // 学习画像：本地兜底计划 chip 用 goal/major；模型建议在任何画像字段存在时触发。
  const profileMajor = useStore((s) => (s.settings.profile_major || "").trim());
  const profileGoal = useStore((s) => (s.settings.profile_goal || "").trim());
  const profileGrade = useStore((s) => (s.settings.profile_grade || "").trim());
  const profileNote = useStore((s) => (s.settings.profile_note || "").trim());
  const hasProfile = !!(profileMajor || profileGoal || profileGrade || profileNote);

  useEffect(() => {
    if (chat.length > 0) {
      setChips([]);
      return;
    }
    let alive = true;
    // 本地兜底：画像目标/专业的模板计划 chip（模型建议可用时会被替换掉）。
    const short = profileGoal || profileMajor;
    const planChip: StartChip | null = short
      ? {
          label: `按「${short.length > 14 ? short.slice(0, 14) + "…" : short}」定制学习计划`,
          message:
            "请结合我的专业背景和学习目标，帮我制定一份可执行的阶段性学习计划：" +
            "先问我 2-3 个关键问题（现有基础、每天可投入时间等），再给出分阶段方案。",
        }
      : null;
    void (async () => {
      // 本地：考考我（最新笔记，退而求其次知识库文件）——确定性强，即时显示。
      let quizChip: StartChip | null = null;
      try {
        const notes = await listNotes();
        if (alive && notes.length) {
          quizChip = {
            label: `考考我《${notes[0].title}》`,
            practice: { kind: "note", sourceId: notes[0].filename },
          };
        }
        if (alive && !notes.length) {
          const kb = (await getApi()?.list_knowledge_files?.()) || [];
          if (alive && kb.length) {
            quizChip = {
              label: `考考我《${kb[0].title || kb[0].filename}》`,
              practice: { kind: "kb", sourceId: kb[0].file_id },
            };
          }
        }
      } catch {
        // bridge not ready — quiz chip 不可用，其余照常
      }
      if (alive) setChips(quizChip ? [quizChip] : []);

      // 模型生成：①深度调研 ②画像请求 ③画像×材料关联（后端缓存，失败静默）。
      try {
        if (!alive || !hasProfile) return;
        const res = await getApi()?.generate_start_chips?.();
        if (!alive || !res?.ok) return;
        const modelChips: StartChip[] = (res.chips || [])
          .filter((c) => c.label && (c.draft || c.message))
          .map((c) =>
            c.draft
              ? { label: c.label, draft: c.draft }
              : { label: c.label, message: c.message! },
          );
        if (!modelChips.length) {
          if (planChip) {
            setChips(quizChip ? [quizChip, planChip] : [planChip]);
          }
          return;
        }
        // 组装：调研 chip 打头，考考我插第二位，其余模型 chips 跟后。
        setChips(() => {
          const [first, ...rest] = modelChips;
          const merged = quizChip ? [first, quizChip, ...rest] : [first, ...rest];
          const seen = new Set<string>();
          return merged
            .filter((c) =>
              seen.has(c.label) ? false : (seen.add(c.label), true),
            )
            .slice(0, 4);
        });
      } catch {
        if (alive && planChip) {
          setChips(quizChip ? [quizChip, planChip] : [planChip]);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [chat.length, hasProfile, profileMajor, profileGoal, profileGrade, profileNote]);

  return chips;
}

export function StartChips({
  chips,
  onSend,
  onDraft,
  onPractice,
}: {
  chips: StartChip[];
  onSend: (text: string) => void;
  onDraft: (text: string) => void;
  onPractice?: (kind: "note" | "kb", sourceId: string) => void;
}) {
  if (chips.length === 0) return null;
  return (
    <div className="mx-auto mb-1 flex max-w-xl flex-wrap justify-center gap-1.5">
      {chips.map((c) => (
        <button
          key={c.label}
          onClick={() => {
            if (c.practice) onPractice?.(c.practice.kind, c.practice.sourceId);
            else if (c.message) onSend(c.message);
            else onDraft(c.draft || "");
          }}
          className="interactive-morph inline-flex items-center gap-1.5 rounded-pill border border-border bg-surface-elevated px-3 py-1.5 text-xs text-secondary hover:border-accent/60 hover:text-accent-strong"
        >
          <Sparkles className="h-3 w-3 text-accent" />
          {c.label}
        </button>
      ))}
    </div>
  );
}
