import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  BookOpenCheck,
  ChevronRight,
  History,
  Loader2,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import { cn } from "@/lib/cn";
import { actions, useStore } from "@/lib/store";
import type {
  PracticeQuizResult,
  PracticeQuestion,
  PracticeSessionSummary,
  PracticeSubmitResult,
} from "@/types";
import { PracticeQuiz, type PracticeAnswer, type QuizState } from "@/components/study/PracticeQuiz";
import { PracticeResult } from "@/components/study/PracticeResult";
import {
  PracticeActivityCard,
  PracticeMasteryCard,
  PracticeStatStrip,
  PracticeWeakSpots,
  hasDashboardData,
  usePracticeDashboard,
} from "@/components/study/PracticeDashboard";

type View = "home" | "quiz" | "result";

/** 学习库 · 练习室：出题 → 作答（整卷/逐题）→ AI 判分 → 复习调度。 */
export function PracticeRoom() {
  const jump = useStore((s) => s.practiceJump);
  const [view, setView] = useState<View>("home");
  const [quiz, setQuiz] = useState<QuizState | null>(null);
  const [submitResult, setSubmitResult] = useState<PracticeSubmitResult | null>(null);
  const [lastAnswers, setLastAnswers] = useState<PracticeAnswer[]>([]);
  const [pendingJump, setPendingJump] = useState(jump);

  useEffect(() => {
    if (jump) {
      setPendingJump(jump);
      actions.clearPracticeJump();
    }
  }, [jump]);

  const startQuiz = useCallback((res: PracticeQuizResult) => {
    if (!res.ok || !res.questions?.length || !res.session_id) return false;
    setQuiz({
      sessionId: res.session_id,
      title: res.title || "练习",
      sourceTitle: res.source_title || "",
      sourceKind: (res.source_kind as "note" | "kb" | "review" | "wrong") || "note",
      sourceId: "",
      mode: res.mode === "card" ? "card" : "paper",
      questions: res.questions as Required<PracticeQuestion>[],
    });
    setSubmitResult(null);
    setView("quiz");
    return true;
  }, []);

  const onSubmitDone = useCallback(
    (r: PracticeSubmitResult, answers: PracticeAnswer[]) => {
      setSubmitResult(r);
      setLastAnswers(answers);
      setView("result");
    },
    [],
  );

  const backHome = useCallback(() => {
    setView("home");
    setQuiz(null);
    setSubmitResult(null);
  }, []);

  const retryWrong = useCallback(
    async (sessionId: string) => {
      const res = (await getApi()?.practice_start_wrong?.(sessionId)) || {
        ok: false,
        error: "桥接不可用",
      };
      if (!startQuiz(res)) {
        // surface why (no wrong questions etc.) — stay on the result view
        return res.error || "没有可重练的错题";
      }
      return null;
    },
    [startQuiz],
  );

  /** 历史回看：拉取已提交会话的完整载荷，复用结果视图渲染。 */
  const openHistory = useCallback(
    async (sessionId: string): Promise<string | null> => {
      const res =
        (await getApi()?.practice_session_detail?.(sessionId)) || {
          ok: false,
          error: "桥接不可用",
        };
      if (!res.ok || !res.session || !res.questions?.length) {
        return res.error || "加载练习详情失败";
      }
      const s = res.session;
      const qs = res.questions as Required<PracticeQuestion>[];
      const attemptByQid = new Map(
        (res.attempts || []).map((a) => [a.question_id, a]),
      );
      setQuiz({
        sessionId,
        title: s.title,
        sourceTitle: s.source_title,
        sourceKind: (s.source_kind as QuizState["sourceKind"]) || "note",
        sourceId: s.source_id || "",
        mode: s.mode === "card" ? "card" : "paper",
        questions: qs,
      });
      setSubmitResult({
        ok: true,
        score_pct: s.score_pct ?? 0,
        correct_count: s.correct_count ?? 0,
        total: s.question_count ?? qs.length,
        per_question: qs.map((q) => {
          const a = attemptByQid.get(q.id);
          const score = a?.score ?? 0;
          return {
            question_id: q.id,
            score,
            is_correct: !!a?.is_correct,
            verdict:
              score >= 0.8 ? "right" : score >= 0.4 ? "partial" : "wrong",
            feedback: a?.feedback || "",
          };
        }),
      });
      setLastAnswers(
        qs.map((q) => {
          const a = attemptByQid.get(q.id);
          if (q.type === "choice") {
            const raw = a?.user_answer ?? "";
            const n = raw === "" ? NaN : Number(raw);
            return {
              choice: Number.isInteger(n) && n >= 0 ? n : null,
              short: "",
            } as PracticeAnswer;
          }
          return {
            choice: null,
            short: a?.user_answer || "",
          } as PracticeAnswer;
        }),
      );
      setView("result");
      return null;
    },
    [],
  );

  if (view === "quiz" && quiz) {
    return <PracticeQuiz quiz={quiz} onDone={onSubmitDone} onCancel={backHome} />;
  }
  if (view === "result" && quiz && submitResult) {
    return (
      <PracticeResult
        quiz={quiz}
        result={submitResult}
        answers={lastAnswers}
        onHome={backHome}
        onRetryWrong={retryWrong}
      />
    );
  }
  return (
    <PracticeHome
      onStart={startQuiz}
      onOpenHistory={openHistory}
      pendingJump={pendingJump}
      onJumpConsumed={() => setPendingJump(null)}
    />
  );
}

/* ─── Home: 今日复习 + 出题表单 + 历史 ─────────────────────────── */

const KIND_LABEL: Record<string, string> = {
  note: "笔记",
  kb: "知识库",
  review: "复习",
  wrong: "错题重练",
  custom: "自定义",
};

function PracticeHome({
  onStart,
  onOpenHistory,
  pendingJump,
  onJumpConsumed,
}: {
  onStart: (res: PracticeQuizResult) => boolean;
  onOpenHistory: (sessionId: string) => Promise<string | null>;
  pendingJump: { kind: "note" | "kb" | "file"; sourceId: string; autoStart: boolean } | null;
  onJumpConsumed: () => void;
}) {
  const [notes, setNotes] = useState<{ filename: string; title: string }[]>([]);
  const [kbFiles, setKbFiles] = useState<{ file_id: string; title: string }[]>([]);
  const [kind, setKind] = useState<"note" | "kb" | "custom">("note");
  const [sourceId, setSourceId] = useState("");
  const [topic, setTopic] = useState("");
  const [count, setCount] = useState(5);
  const [qtype, setQtype] = useState<"mixed" | "choice" | "short">("mixed");
  const [mode, setMode] = useState<"paper" | "card">("paper");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [due, setDue] = useState<{ due_count: number; boxes: Record<string, number> } | null>(null);
  const [history, setHistory] = useState<PracticeSessionSummary[]>([]);

  const materials = kind === "note" ? notes : kbFiles;

  useEffect(() => {
    void (async () => {
      setNotes(await listNotes());
      const list = (await getApi()?.list_knowledge_files?.()) || [];
      setKbFiles(list.map((f) => ({ file_id: f.file_id, title: f.title || f.filename })));
      const d = await getApi()?.practice_due_info?.();
      if (d?.ok) setDue({ due_count: d.due_count || 0, boxes: d.boxes || {} });
      const h = await getApi()?.practice_history?.();
      if (h?.ok) setHistory(h.sessions || []);
    })();
  }, []);

  // keep selection valid when the kind switches
  useEffect(() => {
    const ids = materials.map((m) => ("filename" in m ? m.filename : m.file_id));
    if (ids.length && !ids.includes(sourceId)) setSourceId(ids[0] || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, notes.length, kbFiles.length]);

  const generate = useCallback(
    async (genKind: "note" | "kb" | "custom" | "file", genSourceId: string) => {
      const api = getApi();
      if (!api?.practice_generate || !genSourceId) return false;
      setBusy(true);
      setError("");
      const res = await api.practice_generate(genKind, genSourceId, count, qtype, mode);
      setBusy(false);
      if (!res.ok || !res.questions?.length) {
        setError(res.error || "出题失败，请重试");
        return false;
      }
      return onStart(res);
    },
    [count, qtype, mode, onStart],
  );

  // Consume a start-chip jump: preselect material, auto-generate.
  useEffect(() => {
    if (!pendingJump || busy) return;
    const { kind: jk, sourceId: jsid } = pendingJump;
    onJumpConsumed();
    if (jk === "file") {
      // 本机文件来源（精读联动）：直接出题，来源 segmented 不切换。
      void generate("file", jsid);
      return;
    }
    setKind(jk);
    setSourceId(jsid);
    void generate(jk, jsid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingJump]);

  const startReview = async () => {
    setBusy(true);
    setError("");
    const res = (await getApi()?.practice_start_review?.()) || { ok: false };
    setBusy(false);
    if (!res.ok || !onStart(res)) setError(res.error || "开始复习失败");
  };

  const retryWrongFromHistory = async (sessionId: string) => {
    setBusy(true);
    setError("");
    const res = (await getApi()?.practice_start_wrong?.(sessionId)) || { ok: false };
    setBusy(false);
    if (!res.ok || !onStart(res)) setError(res.error || "错题重练失败");
  };

  const openHistoryDetail = async (sessionId: string) => {
    setHistoryError("");
    const err = await onOpenHistory(sessionId);
    if (err) setHistoryError(err);
  };

  const { info: dash, loading: dashLoading } = usePracticeDashboard();
  const hasDash = hasDashboardData(dash);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-5">
      <div className="mx-auto flex max-w-4xl flex-col gap-3.5">
        {/* 学习数据总览 */}
        {dashLoading ? (
          <div className="flex h-16 items-center justify-center gap-2 rounded-lg border border-border bg-surface-elevated text-xs text-tertiary shadow-[var(--shadow-ambient)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            加载学习数据…
          </div>
        ) : hasDash && dash ? (
          <PracticeStatStrip info={dash} />
        ) : null}

        {/* 今日复习 */}
        {due && due.due_count > 0 && (
          <div className="lift flex items-center justify-between gap-3 rounded-lg border border-accent/40 bg-accent-soft/60 p-4 shadow-[var(--shadow-ambient)]">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-accent-strong text-accent-contrast shadow-[var(--shadow-ambient)]">
                <BookOpenCheck className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-primary">
                  {due.due_count} 张卡片待复习
                </p>
                <p className="mt-0.5 truncate text-xs text-secondary">
                  间隔重复到期的知识卡片，答对升盒、答错回炉
                </p>
              </div>
            </div>
            <Button onClick={() => void startReview()} disabled={busy} className="shrink-0">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "开始复习"}
            </Button>
          </div>
        )}

        {/* 生成练习（主操作）+ 学习进度图表并排 */}
        <div className={cn("grid grid-cols-1 gap-3.5", hasDash && "md:grid-cols-5")}>
          <div className={cn(hasDash && "md:col-span-3")}>
            <div className="rounded-lg border border-border bg-surface-elevated p-4 shadow-[var(--shadow-ambient)]">
              <div className="mb-3 flex items-baseline justify-between gap-2">
                <p className="text-sm font-semibold text-primary">生成练习</p>
                <p className="text-[10px] text-tertiary">AI 围绕材料出题，作答后即时判分</p>
              </div>

              {/* 来源分段选择器 */}
              <div className="mb-2.5 flex rounded-md bg-surface-sunken p-0.5">
                {(
                  [
                    ["note", `笔记 (${notes.length})`],
                    ["kb", `知识库 (${kbFiles.length})`],
                    ["custom", "自定义主题"],
                  ] as const
                ).map(([k, label]) => (
                  <button
                    key={k}
                    onClick={() => setKind(k)}
                    className={cn(
                      "flex-1 rounded-[10px] px-2 py-1.5 text-xs",
                      kind === k
                        ? "bg-surface-elevated font-semibold text-accent-strong shadow-[var(--shadow-ambient)]"
                        : "text-secondary hover:text-primary",
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {kind === "custom" ? (
                <input
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  placeholder="想练什么？一句话主题，如：TCP 三次握手 / 行测言语理解 / Redis 持久化"
                  className="h-9 w-full rounded-md border border-border bg-surface-sunken px-3 text-sm text-primary outline-none placeholder:text-tertiary focus:border-accent"
                />
              ) : (
                <select
                  value={sourceId}
                  onChange={(e) => setSourceId(e.target.value)}
                  className="h-9 w-full rounded-md border border-border bg-surface-sunken px-3 text-sm text-primary outline-none focus:border-accent"
                >
                  <option value="" disabled>
                    {materials.length ? "请选择材料…" : "该类型暂无材料"}
                  </option>
                  {kind === "note"
                    ? notes.map((n) => (
                        <option key={n.filename} value={n.filename}>
                          {n.title}
                        </option>
                      ))
                    : kbFiles.map((f) => (
                        <option key={f.file_id} value={f.file_id}>
                          {f.title}
                        </option>
                      ))}
                </select>
              )}

              {/* 参数区：三列对齐的标签字段 */}
              <div className="mt-3 grid grid-cols-3 gap-2.5">
                <GenerateField label="题量">
                  <select
                    value={count}
                    onChange={(e) => setCount(Number(e.target.value))}
                    className="h-8 w-full rounded-md border border-border bg-surface-sunken px-2 text-xs text-primary outline-none focus:border-accent"
                  >
                    {[1, 2, 3, 5, 8, 10].map((n) => (
                      <option key={n} value={n}>
                        {n} 题
                      </option>
                    ))}
                  </select>
                </GenerateField>
                <GenerateField label="题型">
                  <select
                    value={qtype}
                    onChange={(e) => setQtype(e.target.value as typeof qtype)}
                    className="h-8 w-full rounded-md border border-border bg-surface-sunken px-2 text-xs text-primary outline-none focus:border-accent"
                  >
                    <option value="mixed">混合</option>
                    <option value="choice">选择题</option>
                    <option value="short">简答题</option>
                  </select>
                </GenerateField>
                <GenerateField label="模式">
                  <select
                    value={mode}
                    onChange={(e) => setMode(e.target.value as typeof mode)}
                    className="h-8 w-full rounded-md border border-border bg-surface-sunken px-2 text-xs text-primary outline-none focus:border-accent"
                  >
                    <option value="paper">整卷作答</option>
                    <option value="card">逐题反馈</option>
                  </select>
                </GenerateField>
              </div>

              <Button
                onClick={() => void generate(kind, kind === "custom" ? topic : sourceId)}
                disabled={busy || (kind === "custom" ? !topic.trim() : !sourceId)}
                className="mt-3.5 w-full"
              >
                {busy ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    出题中…
                  </>
                ) : (
                  <>
                    <Sparkles className="h-4 w-4" />
                    生成练习
                  </>
                )}
              </Button>
              {error && (
                <p className="mt-2 flex items-center justify-center gap-1 text-center text-xs text-error">
                  <AlertCircle className="h-3 w-3" />
                  {error}
                </p>
              )}
            </div>
          </div>

          {hasDash && dash && (
            <div className="flex flex-col gap-3.5 md:col-span-2">
              <PracticeMasteryCard info={dash} />
              <PracticeActivityCard info={dash} />
            </div>
          )}
        </div>

        {/* 薄弱知识点（有数据才显示） */}
        <PracticeWeakSpots info={dash} />

        {/* 最近练习：全宽，条目两列铺开 */}
        {history.length > 0 && (
          <div className="rounded-lg border border-border bg-surface-elevated p-4 shadow-[var(--shadow-ambient)]">
            <div className="mb-2.5 flex items-baseline justify-between gap-2">
              <p className="flex items-center gap-1.5 text-sm font-semibold text-primary">
                <History className="h-4 w-4 text-secondary" />
                最近练习
              </p>
              <p className="text-[10px] text-tertiary">点击回看试卷与判分详情</p>
            </div>
            <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
              {history.slice(0, 8).map((s) => {
                const hasWrong =
                  s.correct_count !== null && s.correct_count < s.question_count;
                const pct = s.score_pct ?? 0;
                return (
                  <div
                    key={s.session_id}
                    onClick={() => void openHistoryDetail(s.session_id)}
                    className="group flex cursor-pointer items-center justify-between gap-3 rounded-md bg-surface-sunken px-3 py-2.5 hover:bg-accent-soft/70"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-xs font-medium text-primary">
                        <span className="mr-1.5 inline-block rounded border border-border bg-surface-elevated px-1 py-px text-[10px] font-normal text-secondary">
                          {KIND_LABEL[s.source_kind] || "练习"}
                        </span>
                        {s.title}
                      </p>
                      <p className="mt-0.5 text-[11px] text-tertiary">
                        {new Date(s.created_at * 1000).toLocaleDateString()} ·{" "}
                        {s.question_count} 题
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {hasWrong && (
                        <Button
                          variant="ghost"
                          className="h-6 px-2 py-0 text-[11px]"
                          onClick={(e) => {
                            e.stopPropagation();
                            void retryWrongFromHistory(s.session_id);
                          }}
                          disabled={busy}
                        >
                          <RotateCcw className="h-3 w-3" />
                          重练
                        </Button>
                      )}
                      <span
                        className={cn(
                          "rounded-pill px-2 py-0.5 text-xs font-semibold tabular-nums",
                          pct >= 80
                            ? "bg-success/10 text-success"
                            : pct >= 50
                              ? "bg-warning/10 text-warning"
                              : "bg-error-soft text-error",
                        )}
                      >
                        {pct}%
                      </span>
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-tertiary group-hover:text-accent-strong" />
                    </div>
                  </div>
                );
              })}
            </div>
            {historyError && (
              <p className="mt-1.5 flex items-center justify-center gap-1 text-xs text-error">
                <AlertCircle className="h-3 w-3" />
                {historyError}
              </p>
            )}
          </div>
        )}

        {/* 空历史提示 */}
        {history.length === 0 && due && due.due_count === 0 && (
          <div className="rounded-lg border border-dashed border-border-strong bg-surface-elevated/40 px-4 py-6 text-center text-xs text-tertiary">
            做过的练习和错题都会记录在这里，形成你的复习计划
          </div>
        )}
      </div>
    </div>
  );
}

/** 出题参数的小标签字段：标签在上、控件在下，三列对齐。 */
function GenerateField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-medium text-tertiary">{label}</span>
      {children}
    </label>
  );
}
