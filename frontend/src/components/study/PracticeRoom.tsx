import { useEffect, useMemo, useState } from "react";
import { Check, GraduationCap, Loader2, RotateCcw, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui";
import { EmptyState } from "@/components/ui/EmptyState";
import { getApi } from "@/lib/bridge";
import { listNotes } from "@/lib/library";
import { cn } from "@/lib/cn";
import type { PracticeQuestion } from "@/types";

type Answer = { choice: number | null; short: string; self: "right" | "wrong" | null };

/** 学习库 · 练习室：选材料 → AI 出题 → 本地判分/自评 → 解析。 */
export function PracticeRoom() {
  const [notes, setNotes] = useState<{ filename: string; title: string }[]>([]);
  const [kbFiles, setKbFiles] = useState<{ file_id: string; title: string }[]>([]);
  const [kind, setKind] = useState<"note" | "kb">("note");
  const [sourceId, setSourceId] = useState("");
  const [count, setCount] = useState(5);
  const [qtype, setQtype] = useState<"mixed" | "choice" | "short">("mixed");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [quiz, setQuiz] = useState<{
    title: string;
    questions: PracticeQuestion[];
  } | null>(null);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [graded, setGraded] = useState(false);

  useEffect(() => {
    void (async () => {
      setNotes(await listNotes());
      const list = (await getApi()?.list_knowledge_files?.()) || [];
      setKbFiles(list.map((f) => ({ file_id: f.file_id, title: f.title || f.filename })));
    })();
  }, []);

  const materials = kind === "note" ? notes : kbFiles;
  useEffect(() => {
    // keep selection valid when the kind switches
    const ids = materials.map((m: { filename?: string; file_id?: string }) =>
      kind === "note" ? m.filename : m.file_id,
    );
    if (ids.length && !ids.includes(sourceId)) {
      setSourceId(ids[0] || "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, notes.length, kbFiles.length]);

  const score = useMemo(() => {
    if (!quiz || !graded) return null;
    let right = 0;
    quiz.questions.forEach((q, i) => {
      const a = answers[i];
      if (q.type === "choice") {
        if (a?.choice !== null && a?.choice === Number(q.answer)) right += 1;
      } else if (a?.self === "right") right += 1;
    });
    return { right, total: quiz.questions.length };
  }, [quiz, graded, answers]);

  const generate = async () => {
    const api = getApi();
    if (!api?.practice_generate || !sourceId) return;
    setBusy(true);
    setError("");
    setQuiz(null);
    setGraded(false);
    try {
      const res = await api.practice_generate(kind, sourceId, count, qtype);
      if (!res.ok || !res.questions?.length) {
        setError(res.error || "出题失败，请重试");
        return;
      }
      setQuiz({ title: res.title || "练习", questions: res.questions });
      setAnswers(
        res.questions.map(() => ({ choice: null, short: "", self: null }) as Answer),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const setAnswer = (i: number, patch: Partial<Answer>) =>
    setAnswers((prev) =>
      prev.map((a, j) => (j === i ? { ...a, ...patch } : a)),
    );

  const canSubmit =
    !!quiz &&
    answers.every(
      (a, i) =>
        quiz.questions[i].type === "choice"
          ? a.choice !== null
          : a.short.trim().length > 0 || a.self !== null,
    );

  const reset = () => {
    setQuiz(null);
    setGraded(false);
    setAnswers([]);
    setError("");
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      {notes.length === 0 && kbFiles.length === 0 ? (
        <EmptyState
          icon={GraduationCap}
          title="练习室还没有材料"
          hint="先在「资料」或「知识库」标签里准备学习内容，然后就能在这里让助手出题检验学习效果。"
        />
      ) : !quiz ? (
        <div className="mx-auto flex max-w-xl flex-col gap-3">
          <div className="rounded-lg border border-border bg-surface-elevated p-4">
            <div className="mb-2 text-sm font-semibold text-primary">选择材料</div>
            <div className="mb-2 flex gap-1.5">
              {(
                [
                  ["note", `笔记 (${notes.length})`],
                  ["kb", `知识库 (${kbFiles.length})`],
                ] as const
              ).map(([k, label]) => (
                <button
                  key={k}
                  onClick={() => {
                    setKind(k);
                    setSourceId("");
                  }}
                  className={cn(
                    "rounded-md px-3 py-1 text-xs",
                    kind === k
                      ? "bg-accent-soft font-medium text-accent-strong"
                      : "bg-surface-sunken text-secondary hover:text-primary",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
            <select
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
              className="h-9 w-full rounded-md border border-border bg-surface-sunken px-2 text-sm text-primary outline-none focus:border-accent"
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
            <div className="mt-3 flex gap-4">
              <label className="flex items-center gap-1.5 text-xs text-secondary">
                题量
                <select
                  value={count}
                  onChange={(e) => setCount(Number(e.target.value))}
                  className="h-7 rounded-md border border-border bg-surface-sunken px-1.5 text-xs text-primary outline-none"
                >
                  {[3, 5, 8, 10].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1.5 text-xs text-secondary">
                题型
                <select
                  value={qtype}
                  onChange={(e) => setQtype(e.target.value as typeof qtype)}
                  className="h-7 rounded-md border border-border bg-surface-sunken px-1.5 text-xs text-primary outline-none"
                >
                  <option value="mixed">混合</option>
                  <option value="choice">选择题</option>
                  <option value="short">简答题</option>
                </select>
              </label>
            </div>
            <Button
              onClick={() => void generate()}
              disabled={busy || !sourceId}
              className="mt-3 w-full"
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
              <p className="mt-2 text-center text-xs text-error">{error}</p>
            )}
          </div>
        </div>
      ) : (
        <div className="mx-auto flex max-w-2xl flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="text-md font-semibold text-primary">{quiz.title}</h3>
            <Button variant="ghost" className="h-7 py-1" onClick={reset}>
              <RotateCcw className="h-3.5 w-3.5" />
              重新选材料
            </Button>
          </div>
          {quiz.questions.map((q, i) => {
            const a = answers[i];
            const isChoice = q.type === "choice";
            const correct = isChoice && a?.choice === Number(q.answer);
            return (
              <div
                key={i}
                className={cn(
                  "rounded-lg border p-3.5",
                  graded && isChoice
                    ? correct
                      ? "border-success/50 bg-success/5"
                      : "border-error/50 bg-error/5"
                    : "border-border bg-surface-elevated",
                )}
              >
                <div className="mb-2 flex items-start gap-2 text-sm text-primary">
                  <span className="shrink-0 font-semibold text-accent-strong">
                    {i + 1}.
                  </span>
                  <span className="leading-relaxed">{q.question}</span>
                </div>
                {isChoice ? (
                  <div className="flex flex-col gap-1">
                    {(q.options || []).map((opt, oi) => {
                      const picked = a?.choice === oi;
                      const isAnswer = Number(q.answer) === oi;
                      return (
                        <button
                          key={oi}
                          disabled={graded}
                          onClick={() => setAnswer(i, { choice: oi })}
                          className={cn(
                            "flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-sm",
                            graded
                              ? isAnswer
                                ? "border-success/60 bg-success/10 text-primary"
                                : picked
                                  ? "border-error/60 bg-error/10 text-primary"
                                  : "border-border text-secondary"
                              : picked
                                ? "border-accent bg-accent-soft text-primary"
                                : "border-border text-secondary hover:border-border-strong hover:text-primary",
                          )}
                        >
                          {graded && isAnswer && (
                            <Check className="h-3.5 w-3.5 shrink-0 text-success" />
                          )}
                          {graded && picked && !isAnswer && (
                            <X className="h-3.5 w-3.5 shrink-0 text-error" />
                          )}
                          {opt}
                        </button>
                      );
                    })}
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">
                    <textarea
                      value={a?.short || ""}
                      disabled={graded}
                      onChange={(e) => setAnswer(i, { short: e.target.value })}
                      rows={2}
                      placeholder="写下你的答案…"
                      className="resize-y rounded-md border border-border bg-surface-sunken px-2.5 py-1.5 text-sm text-primary outline-none focus:border-accent"
                    />
                    {graded && (
                      <div className="rounded-md bg-surface-sunken p-2 text-xs leading-relaxed text-secondary">
                        <span className="font-medium text-primary">参考答案：</span>
                        {String(q.answer)}
                      </div>
                    )}
                    {graded && (
                      <div className="flex gap-1.5">
                        <button
                          onClick={() => setAnswer(i, { self: "right" })}
                          className={cn(
                            "rounded-pill px-3 py-0.5 text-xs",
                            a?.self === "right"
                              ? "bg-success/15 font-medium text-success"
                              : "bg-surface-sunken text-tertiary hover:text-primary",
                          )}
                        >
                          我答对了
                        </button>
                        <button
                          onClick={() => setAnswer(i, { self: "wrong" })}
                          className={cn(
                            "rounded-pill px-3 py-0.5 text-xs",
                            a?.self === "wrong"
                              ? "bg-error/15 font-medium text-error"
                              : "bg-surface-sunken text-tertiary hover:text-primary",
                          )}
                        >
                          没答上
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {graded && q.explain && (
                  <p className="mt-2 border-t border-border pt-2 text-xs leading-relaxed text-tertiary">
                    {q.explain}
                  </p>
                )}
              </div>
            );
          })}
          {graded ? (
            <div className="rounded-lg border border-border bg-surface-elevated p-4 text-center">
              <p className="text-2xl font-bold text-accent-strong">
                {score?.right} / {score?.total}
              </p>
              <p className="mt-1 text-sm text-secondary">
                {score && score.right === score.total
                  ? "全对！这个知识点已经拿下了"
                  : "错题看上面的解析，回材料里再巩固一下"}
              </p>
            </div>
          ) : (
            <Button onClick={() => setGraded(true)} disabled={!canSubmit} className="w-full">
              交卷
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
