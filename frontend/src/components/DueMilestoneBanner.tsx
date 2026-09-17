import { CalendarClock, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { actions, useStore } from "@/lib/store";

/**
 * 目标节点提醒（考研报名 / 初试 / 出分…）。
 *
 * 后端 `due_milestones()` 早就算出这些数据，但以前没有任何消费者——
 * 用户不主动打开「规划」tab 就永远看不到「预报名还有 3 天」。
 * 这条横幅是它的第一个消费入口，挂在启动即加载的 init_data 上。
 */
export function DueMilestoneBanner() {
  const milestones = useStore((s) => s.dueMilestones);

  if (milestones.length === 0) return null;

  return (
    <div className="flex flex-col gap-1 border-b border-border px-4 py-2">
      {milestones.slice(0, 3).map((m) => {
        const overdue = m.days < 0;
        const today = m.days === 0;
        const text = overdue
          ? `${m.label}已逾期 ${-m.days} 天`
          : today
            ? `${m.label}就是今天`
            : `${m.label}还有 ${m.days} 天`;
        return (
          <div
            key={`${m.track_id}:${m.key}`}
            className={cn(
              "flex items-center gap-2 rounded-sm px-2 py-1 text-xs",
              overdue
                ? "bg-error/10 text-error"
                : today
                  ? "bg-warning/15 text-warning"
                  : "bg-accent-soft text-accent-strong",
            )}
          >
            <CalendarClock className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {text}
              <span className="text-tertiary"> · {m.track_title}</span>
            </span>
            {m.note && (
              <span className="hidden shrink-0 text-tertiary sm:inline">
                {m.note}
              </span>
            )}
            <button
              type="button"
              aria-label="忽略该提醒"
              onClick={() => actions.dismissDueMilestone(m.key)}
              className="shrink-0 rounded-sm p-0.5 text-tertiary hover:text-primary"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
