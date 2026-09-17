import { useMemo, useState } from "react";
import { AlertCircle, Check, ChevronRight, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui";
import { getApi } from "@/lib/bridge";
import { cn } from "@/lib/cn";
import type { PracticeGradeResult, PracticeQuestion, PracticeSubmitResult } from "@/types";

export interface QuizState {
  sessionId: string;
  title: string;
  sourceTitle: string;
  sourceKind: "note" | "kb" | "review" | "wrong";
  sourceId: string;
  mode: "paper" | "card";
  questions: Required<PracticeQuestion>[];
}

type Answer = {
  choice: number | null;
  short: string;
  /** card mode: filled as soon as the question is graded */
  score?: number;
  feedback?: string;
  verdict?: "right" | "partial" | "wrong";
};

export type { Answer as PracticeAnswer };

const DIFF_LABEL: Record<string, string> = {
  basic: "基础",
  understand: "理解",
  apply: "应用",
};

const VERDICT_META: Record<string, { label: string; cls: string }> = {
  right: { label: "回答正确", cls: "text-success" },
  partial: { label: "部分正确", cls: "text-warning" },
  wrong: { label: "未答对", cls: "text-error" },
};

/** 练习作答视图：paper = 整卷一次判分；card = 逐题即时反馈（AI 判简答）。 */
export function PracticeQuiz({
  quiz,
  onDone,
  onCancel,
}: {
  quiz: QuizState;
  onDone: (result: PracticeSubmitResult, answers: Answer[]) => void;
  onCancel: () => void;
}) {
  const [answers, setAnswers] = useState<Answer[]>(() =>
    quiz.questions.map(() => ({ choice: null, short: "" }) as Answer),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  // card mode
  const [idx, setIdx] = useState(0);
  const [grading, setGrading] = useState(false);
  const [gradeResult, setGradeResult] = useState<PracticeGradeResult | null>(null);

  const q = quiz.questions[idx];
  const a = answers[idx];
  const isCard = quiz.mode === "card";
  const isLast = idx === quiz.questions.length - 1;

  const setAnswer = (i: number, patch: Partial<Answer>) =>
    setAnswers((prev) => prev.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const canSubmitPaper = useMemo(
    () =>
      quiz.questions.every((qq, i) =>
        qq.type === "choice"
          ? answers[i].choice !== null
          : answers[i].short.trim().length > 0,
      ),
    [quiz.questions, answers],
  );
  const answeredPaperCount = useMemo(
    () =>
      quiz.questions.filter((qq, i) =>
        qq.type === "choice"
          ? answers[i].choice !== null
          : answers[i].short.trim().length > 0,
      ).length,
    [quiz.questions, answers],
  );

  const submit = async () => {
    setSubmitting(true);
    setError("");
    try {
      const payload = quiz.questions.map((qq, i) => {
        const ans = answers[i];
        const item: Record<string, unknown> = {
          question_id: qq.id,
          user_answer:
            qq.type === "choice" ? String(ans.choice ?? "") : ans.short,
        };
        if (isCard) {
          // card mode already graded every question — pass scores through
          item.score = ans.score ?? 0;
          item.feedback = ans.feedback ?? "";
        }
        return item;
      });
      const res = (await getApi()?.practice_submit?.(quiz.sessionId, payload as never)) || {
        ok: false,
        error: "桥接不可用",
      };
      if (!res.ok) setError(res.error || "提交失败");
      else onDone(res, answers);
    } finally {
      setSubmitting(false);
    }
  };

  /* ─── Card mode: confirm one question ─── */
  const confirmCard = async () => {
    if (!q || !a) return;
    if (q.type === "choice") {
      const correct = a.choice !== null && a.choice === Number(q.answer);
      setAnswer(idx, {
        score: correct ? 1 : 0,
        feedback: correct ? "" : `正确答案是 ${q.options?.[Number(q.answer)] ?? ""}`,
        verdict: correct ? "right" : "wrong",
      });
      return; // show inline result; user clicks 下一题
    }
    // short → AI grade
    setGrading(true);
    setError("");
    try {
      const res =
        (await getApi()?.practice_grade_short?.(q.id, a.short)) || {
          ok: false,
          error: "桥接不可用",
        };
      if (res.ok) {
        setAnswer(idx, {
          score: res.score,
          feedback: res.feedback,
          verdict: res.verdict,
        });
        setGradeResult(null);
      } else {
        setGradeResult({ ok: false, error: res.error || "判分失败" });
      }
    } finally {
      setGrading(false);
    }
  };

  const nextCard = () => {
    setGradeResult(null);
    if (isLast) void submit();
    else setIdx((i) => i + 1);
  };

  const cardAnswered = isCard && a?.verdict !== undefined;
  const cardGradingFailed = isCard && gradeResult && !gradeResult.ok;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto flex max-w-2xl flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="min-w-0">
            <h3 className="truncate text-md font-semibold text-primary">{quiz.title}</h3>
            <p className="text-[11px] text-tertiary">
              {isCard ? `逐题模式 · 第 ${idx + 1} / ${quiz.questions.length} 题` : `整卷模式 · 共 ${quiz.questions.length} 题`}
            </p>
          </div>
          <Button variant="ghost" className="h-7 py-1" onClick={onCancel} disabled={submitting}>
            退出练习
          </Button>
        </div>

        {/* card mode progress dots */}
        {isCard && (
          <div className="flex gap-1">
            {quiz.questions.map((_, i) => {
              const v = answers[i].verdict;
              return (
                <div
                  key={i}
                  className={cn(
                    "h-1 flex-1 rounded-pill",
                    i === idx
                      ? "bg-accent"
                      : v === "right"
                        ? "bg-success/60"
                        : v === "partial"
                          ? "bg-warning/60"
                          : v === "wrong"
                            ? "bg-error/60"
                            : "bg-surface-sunken",
                  )}
                />
              );
            })}
          </div>
        )}

        {/* paper: all questions on one page; card: current question only */}
        {(isCard
          ? q
            ? [{ qq: q, i: idx }]
            : []
          : quiz.questions.map((qq, i) => ({ qq, i }))
        ).map(({ qq, i }) => {
          const ans = answers[i];
          const answered = isCard && ans?.verdict !== undefined;
          return (
            <div
              key={qq.id}
              className="rounded-lg border border-border bg-surface-elevated p-4"
            >
              <div className="mb-2 flex items-start gap-2 text-sm text-primary">
                <span className="shrink-0 font-semibold text-accent-strong">{i + 1}.</span>
                <span className="leading-relaxed">{qq.question}</span>
                <span className="ml-auto flex shrink-0 gap-1">
                  {qq.category && (
                    <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent-strong">
                      {qq.category}
                    </span>
                  )}
                  {qq.difficulty && (
                    <span className="rounded bg-surface-sunken px-1.5 py-0.5 text-[10px] text-tertiary">
                      {DIFF_LABEL[qq.difficulty] || qq.difficulty}
                    </span>
                  )}
                </span>
              </div>

              {qq.type === "choice" ? (
                <ChoiceOptions
                  q={qq}
                  picked={ans?.choice ?? null}
                  disabled={answered || submitting}
                  showResult={answered}
                  onPick={(oi) => setAnswer(i, { choice: oi })}
                />
              ) : (
                <div className="flex flex-col gap-2">
                  <textarea
                    value={ans?.short || ""}
                    disabled={answered || submitting}
                    onChange={(e) => setAnswer(i, { short: e.target.value })}
                    rows={3}
                    placeholder="写下你的答案…"
                    className="resize-y rounded-md border border-border bg-surface-sunken px-2.5 py-1.5 text-sm text-primary outline-none focus:border-accent"
                  />
                  {answered && (
                    <CardShortFeedback
                      verdict={ans.verdict!}
                      score={ans.score ?? 0}
                      feedback={ans.feedback || ""}
                    />
                  )}
                </div>
              )}
            </div>
          );
        })}

        {error && (
          <p className="flex items-center gap-1 text-xs text-error">
            <AlertCircle className="h-3 w-3" />
            {error}
          </p>
        )}

        {/* ─── Controls ─── */}
        {isCard ? (
          <div className="flex flex-col gap-2">
            {!cardAnswered && (
              <Button
                onClick={() => void confirmCard()}
                disabled={
                  grading ||
                  (q?.type === "choice" ? a?.choice === null : a?.short.trim().length === 0)
                }
                className="w-full"
              >
                {grading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    AI 判分中…
                  </>
                ) : q?.type === "choice" ? (
                  "确认答案"
                ) : (
                  "提交判分"
                )}
              </Button>
            )}
            {cardGradingFailed && (
              <p className="text-center text-xs text-tertiary">
                判分失败也可点「下一题」跳过——交卷时会自动重新判分
              </p>
            )}
            {cardAnswered && (
              <Button onClick={nextCard} className="w-full">
                {isLast ? (
                  submitting ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      生成练习记录…
                    </>
                  ) : (
                    "查看结果"
                  )
                ) : (
                  <>
                    下一题
                    <ChevronRight className="h-4 w-4" />
                  </>
                )}
              </Button>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <p className="text-center text-xs text-tertiary">
              已答 {answeredPaperCount} / {quiz.questions.length} 题
              {answeredPaperCount < quiz.questions.length && "（答完全部题目后可交卷）"}
            </p>
            <Button
              onClick={() => void submit()}
              disabled={!canSubmitPaper || submitting}
              className="w-full"
            >
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  AI 判分中…
                </>
              ) : (
                "交卷"
              )}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Choice options (shared by both modes) ─────────────────────── */

function ChoiceOptions({
  q,
  picked,
  disabled,
  showResult,
  onPick,
}: {
  q: Required<PracticeQuestion>;
  picked: number | null;
  disabled: boolean;
  showResult: boolean;
  onPick: (index: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      {(q.options || []).map((opt, oi) => {
        const isPicked = picked === oi;
        const isAnswer = Number(q.answer) === oi;
        return (
          <button
            key={oi}
            disabled={disabled}
            onClick={() => onPick(oi)}
            className={cn(
              "flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-sm",
              showResult
                ? isAnswer
                  ? "border-success/60 bg-success/10 text-primary"
                  : isPicked
                    ? "border-error/60 bg-error/10 text-primary"
                    : "border-border text-secondary"
                : isPicked
                  ? "border-accent bg-accent-soft text-primary"
                  : "border-border text-secondary hover:border-border-strong hover:text-primary",
            )}
          >
            {showResult && isAnswer && <Check className="h-3.5 w-3.5 shrink-0 text-success" />}
            {showResult && isPicked && !isAnswer && <X className="h-3.5 w-3.5 shrink-0 text-error" />}
            {opt}
          </button>
        );
      })}
    </div>
  );
}

/* ─── Card-mode short answer feedback ───────────────────────────── */

function CardShortFeedback({
  verdict,
  score,
  feedback,
}: {
  verdict: "right" | "partial" | "wrong";
  score: number;
  feedback: string;
}) {
  const meta = VERDICT_META[verdict] || VERDICT_META.wrong;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className={cn("text-sm font-semibold", meta.cls)}>{meta.label}</span>
        <span className="text-xs text-tertiary">得分 {Math.round(score * 100)}%</span>
      </div>
      {feedback && (
        <div className="rounded-md bg-surface-sunken p-2 text-xs leading-relaxed text-secondary">
          {feedback}
        </div>
      )}
    </div>
  );
}
