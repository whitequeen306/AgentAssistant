import { useEffect, useState } from "react";
import { Activity, Flame, Layers, TrendingDown } from "lucide-react";
import { getApi } from "@/lib/bridge";
import { cn } from "@/lib/cn";

interface DashboardInfo {
  ok: boolean;
  error?: string;
  cards_total?: number;
  mastered?: number;
  due_count?: number;
  boxes?: Record<string, number>;
  attempts_total?: number;
  correct_rate?: number;
  streak_days?: number;
  last_14_days?: { day: string; count: number; correct: number }[];
  weak_spots?: {
    question_id: string;
    question: string;
    category: string;
    attempts: number;
    avg_score: number;
  }[];
}

/** Leitner 盒子 → 复习间隔（与后端 scheduler 一致，仅用于展示）。 */
const BOX_LABELS: Record<string, string> = {
  "1": "每天",
  "2": "隔 2 天",
  "3": "隔 4 天",
  "4": "隔 7 天",
  "5": "隔 15 天",
};

/** 拉取练习仪表盘聚合数据（一次调用，供各卡片共享）。 */
export function usePracticeDashboard() {
  const [info, setInfo] = useState<DashboardInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const res = await getApi()?.practice_dashboard?.();
      if (!alive) return;
      setInfo(res || { ok: false });
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, []);

  return { info, loading };
}

/** 仪表盘是否有可展示的数据。 */
export function hasDashboardData(info: DashboardInfo | null): boolean {
  return (
    !!info?.ok &&
    ((info.cards_total ?? 0) > 0 || (info.attempts_total ?? 0) > 0)
  );
}

/** 数字总览：单卡四栏，内部竖线分隔。 */
export function PracticeStatStrip({ info }: { info: DashboardInfo }) {
  return (
    <div className="grid grid-cols-4 divide-x divide-border rounded-lg border border-border bg-surface-elevated py-1 shadow-[var(--shadow-ambient)]">
      <StatCell label="知识卡片" value={info.cards_total ?? 0} />
      <StatCell label="已掌握" value={info.mastered ?? 0} tone="text-success" />
      <StatCell label="总正确率" value={`${info.correct_rate ?? 0}%`} />
      <StatCell
        label="连续打卡"
        value={`${info.streak_days ?? 0} 天`}
        icon={<Flame className="h-3.5 w-3.5 text-warning" />}
      />
    </div>
  );
}

/** 掌握度分布（Leitner 盒子）。 */
export function PracticeMasteryCard({ info }: { info: DashboardInfo }) {
  return (
    <div className="rounded-lg border border-border bg-surface-elevated p-4 shadow-[var(--shadow-ambient)]">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-semibold text-primary">
          <Layers className="h-3.5 w-3.5 text-accent-strong" />
          掌握度分布
        </p>
        <span className="text-[10px] text-tertiary">盒子越深 · 记得越牢</span>
      </div>
      <div className="flex flex-col gap-2">
        {["1", "2", "3", "4", "5"].map((box) => {
          const n = info.boxes?.[box] ?? 0;
          const total = Math.max(1, info.cards_total ?? 1);
          return (
            <div key={box} className="flex items-center gap-2.5">
              <span className="w-20 shrink-0 whitespace-nowrap text-[10px] text-tertiary">
                盒 {box} · {BOX_LABELS[box]}
              </span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-pill bg-surface-sunken">
                <div
                  className={cn(
                    "h-full rounded-pill",
                    Number(box) >= 4 ? "bg-success/80" : "bg-accent/80",
                  )}
                  style={{ width: `${(n / total) * 100}%` }}
                />
              </div>
              <span className="w-6 shrink-0 text-right text-[10px] font-medium tabular-nums text-secondary">
                {n}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 近 14 天练习：GitHub 风格热力条——颜色=正确率，深浅=题量。 */
export function PracticeActivityCard({ info }: { info: DashboardInfo }) {
  const byDay = new Map(
    (info.last_14_days || []).map((d) => [d.day, d]),
  );
  const maxCount = Math.max(
    1,
    ...(info.last_14_days || []).map((d) => d.count),
  );
  // 后端只回有作答的日期；补零成完整 14 天，今天固定在最后。
  const series = Array.from({ length: 14 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - (13 - i));
    const key =
      `${d.getFullYear()}-` +
      `${String(d.getMonth() + 1).padStart(2, "0")}-` +
      `${String(d.getDate()).padStart(2, "0")}`;
    return byDay.get(key) || { day: key, count: 0, correct: 0 };
  });

  return (
    <div className="rounded-lg border border-border bg-surface-elevated p-4 shadow-[var(--shadow-ambient)]">
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-semibold text-primary">
          <Activity className="h-3.5 w-3.5 text-accent-strong" />
          近 14 天练习
        </p>
        <span className="flex items-center gap-1 text-[10px] text-tertiary">
          颜色 = 正确率
          <i className="inline-block h-1.5 w-1.5 rounded-[2px] bg-success/80" />
          <i className="inline-block h-1.5 w-1.5 rounded-[2px] bg-warning/80" />
          <i className="inline-block h-1.5 w-1.5 rounded-[2px] bg-error/80" />
        </span>
      </div>
      <div className="flex items-center gap-[3px]">
        {series.map((d) => {
          const rate = d.count > 0 ? d.correct / d.count : 0;
          const cellClass = !d.count
            ? "bg-surface-sunken"
            : rate >= 0.8
              ? "bg-success"
              : rate >= 0.5
                ? "bg-warning"
                : "bg-error";
          return (
            <div
              key={d.day}
              title={
                d.count > 0
                  ? `${d.day}：${d.count} 题（对 ${d.correct} / 错 ${d.count - d.correct}）`
                  : `${d.day}：未练习`
              }
              className={cn("h-2.5 flex-1 rounded-[3px]", cellClass)}
              style={
                d.count > 0
                  ? { opacity: 0.35 + 0.65 * (d.count / maxCount) }
                  : undefined
              }
            />
          );
        })}
      </div>
      <div className="mt-1.5 flex items-center justify-between text-[10px] text-tertiary">
        <span>14 天前</span>
        <span>今天</span>
      </div>
    </div>
  );
}

/** 薄弱知识点（无数据时整块隐藏）。 */
export function PracticeWeakSpots({ info }: { info: DashboardInfo | null }) {
  if (!info?.ok || !(info.weak_spots?.length ?? 0)) return null;
  return (
    <div className="rounded-lg border border-border bg-surface-elevated p-4 shadow-[var(--shadow-ambient)]">
      <div className="mb-2.5 flex items-baseline justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-semibold text-primary">
          <TrendingDown className="h-3.5 w-3.5 text-error" />
          薄弱知识点
        </p>
        <span className="text-[10px] text-tertiary">多次作答平均分最低</span>
      </div>
      <div className="grid gap-1.5 md:grid-cols-2">
        {info.weak_spots!.map((w) => (
          <div
            key={w.question_id}
            className="flex items-center justify-between gap-2 rounded-md bg-surface-sunken px-2.5 py-2"
          >
            <span className="min-w-0 truncate text-xs text-primary">
              {w.category && (
                <span className="mr-1.5 rounded bg-accent-soft px-1 py-0.5 text-[10px] text-accent-strong">
                  {w.category}
                </span>
              )}
              {w.question}
            </span>
            <span
              className={cn(
                "shrink-0 rounded-pill bg-surface-elevated px-1.5 py-0.5 text-[10px] font-semibold tabular-nums",
                w.avg_score >= 0.6 ? "text-warning" : "text-error",
              )}
            >
              {Math.round(w.avg_score * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function StatCell({
  label,
  value,
  icon,
  tone = "text-primary",
}: {
  label: string;
  value: number | string;
  icon?: React.ReactNode;
  tone?: string;
}) {
  return (
    <div className="flex flex-col items-center gap-1 px-2 py-3">
      <p
        className={cn(
          "flex items-center gap-1 font-display text-xl font-semibold leading-none tabular-nums",
          tone,
        )}
      >
        {icon}
        {value}
      </p>
      <p className="text-[10px] text-tertiary">{label}</p>
    </div>
  );
}
