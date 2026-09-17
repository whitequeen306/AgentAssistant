import { useState } from "react";
import { Check, FileDown, Home, Loader2, RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui";
import { getApi } from "@/lib/bridge";
import { cn } from "@/lib/cn";
import type { PracticeSubmitResult } from "@/types";
import type { PracticeAnswer, QuizState } from "@/components/study/PracticeQuiz";

const VERDICT_LABEL: Record<string, string> = {
  right: "回答正确",
  partial: "部分正确",
  wrong: "未答对",
};

/** 练习结果页：成绩总览 + 逐题回看（AI 反馈/解析）+ 后续动作。 */
export function PracticeResult({
  quiz,
  result,
  answers,
  onHome,
  onRetryWrong,
}: {
  quiz: QuizState;
  result: PracticeSubmitResult;
  answers: PracticeAnswer[];
  onHome: () => void;
  onRetryWrong: (sessionId: string) => Promise<string | null>;
}) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");

  const byId = new Map(
    (result.per_question || []).map((p) => [p.question_id, p]),
  );
  const hasWrong = (result.correct_count ?? 0) < (result.total ?? 0);
  const pct = result.score_pct ?? 0;

  const saveReport = async () => {
    setSaving(true);
    setSaved("");
    try {
      const res =
        (await getApi()?.practice_save_report?.(quiz.sessionId)) || {
          ok: false,
          error: "桥接不可用",
        };
      setSaved(res.ok ? "已存入学习库「资料」" : res.error || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const retryWrong = async () => {
    setRetrying(true);
    setRetryError("");
    const err = await onRetryWrong(quiz.sessionId);
    setRetrying(false);
    if (err) setRetryError(err);
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto flex max-w-2xl flex-col gap-3">
        {/* 成绩卡 */}
        <div className="rounded-lg border border-border bg-surface-elevated p-5 text-center">
          <p className="text-3xl font-bold text-accent-strong">{pct}%</p>
          <p className="mt-1 text-sm text-secondary">
            {result.correct_count ?? 0} / {result.total ?? 0} 题达标
            · {quiz.title}
          </p>
          <p className="mt-1.5 text-xs text-tertiary">
            {pct === 100
              ? "全对！这些知识点已经拿下了"
              : pct >= 80
                ? "掌握得不错，错题已进入复习队列"
                : hasWrong
                  ? "错题已按记忆规律排入复习计划，记得回来看看"
                  : "继续保持"}
          </p>
        </div>

        {/* 操作区 */}
        <div className="flex flex-wrap justify-center gap-2">
          {hasWrong && (
            <Button onClick={() => void retryWrong()} disabled={retrying} className="h-8">
              {retrying ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              错题重练
            </Button>
          )}
          <Button variant="ghost" className="h-8" onClick={() => void saveReport()} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDown className="h-4 w-4" />}
            报告存入资料
          </Button>
          <Button variant="ghost" className="h-8" onClick={onHome}>
            <Home className="h-4 w-4" />
            返回练习室
          </Button>
        </div>
        {saved && <p className="text-center text-xs text-success">{saved}</p>}
        {retryError && <p className="text-center text-xs text-error">{retryError}</p>}

        {/* 逐题回看 */}
        {quiz.questions.map((q, i) => {
          const p = byId.get(q.id);
          if (!p) return null;
          const ans = answers[i];
          const isChoice = q.type === "choice";
          const userAnswerText = isChoice
            ? (ans?.choice !== null && ans?.choice !== undefined
                ? q.options?.[ans.choice] || `选项 ${ans.choice + 1}`
                : "（未作答）")
            : ans?.short?.trim() || "（未作答）";
          return (
            <div
              key={q.id}
              className={cn(
                "rounded-lg border p-3.5",
                p.is_correct
                  ? "border-success/40 bg-success/5"
                  : "border-error/40 bg-error/5",
              )}
            >
              <div className="mb-1.5 flex items-start gap-2 text-sm text-primary">
                <span className="shrink-0 font-semibold text-accent-strong">{i + 1}.</span>
                <span className="leading-relaxed">{q.question}</span>
                <span className="ml-auto shrink-0">
                  {p.is_correct ? (
                    <Check className="h-4 w-4 text-success" />
                  ) : (
                    <X className="h-4 w-4 text-error" />
                  )}
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-5 text-xs text-secondary">
                <span>
                  得分 <b className="text-primary">{Math.round(p.score * 100)}%</b>
                </span>
                <span className={cn(p.is_correct ? "text-success" : "text-warning")}>
                  {VERDICT_LABEL[p.verdict] || p.verdict}
                </span>
                <span>
                  你的答案：<span className="text-primary">{userAnswerText}</span>
                </span>
              </div>
              {p.feedback && (
                <p className="ml-5 mt-1.5 rounded-md bg-surface-sunken p-2 text-xs leading-relaxed text-secondary">
                  {p.feedback}
                </p>
              )}
              {q.explain && (
                <p className="ml-5 mt-1.5 border-t border-border pt-1.5 text-xs leading-relaxed text-tertiary">
                  解析：{q.explain}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
