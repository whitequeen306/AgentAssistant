import { useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Check,
  ChevronDown,
  CircleDot,
  Plus,
  Target,
  Trash2,
} from "lucide-react";
import { Button, IconButton, Input } from "@/components/ui";
import { EmptyState } from "@/components/ui/EmptyState";
import { Tooltip } from "@/components/ui/Tooltip";
import { getApi, prefetchStartChips } from "@/lib/bridge";
import type { GoalMilestone, GoalSpec, GoalTrack } from "@/types";

/** 距今天的天数（按自然日算，不按 24h 取整）。 */
function daysFromToday(ts: number): number {
  const a = new Date();
  a.setHours(0, 0, 0, 0);
  const b = new Date(ts * 1000);
  b.setHours(0, 0, 0, 0);
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

function toDateInput(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function countdownLabel(days: number): { text: string; tone: string } {
  if (days < 0) return { text: `已过去 ${-days} 天`, tone: "text-tertiary" };
  if (days === 0) return { text: "就是今天", tone: "text-warning" };
  if (days <= 7) return { text: `还有 ${days} 天`, tone: "text-warning" };
  return { text: `还有 ${days} 天`, tone: "text-secondary" };
}

/** 学习库 · 规划：目标轨道 + 关键节点倒计时。 */
export function PlanningPanel() {
  const [specs, setSpecs] = useState<GoalSpec[]>([]);
  const [tracks, setTracks] = useState<GoalTrack[]>([]);
  const [msMap, setMsMap] = useState<Record<string, GoalMilestone[]>>({});
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  const refresh = async () => {
    const api = getApi();
    const [s, t] = await Promise.all([
      api?.list_goal_specs?.(),
      api?.list_goal_tracks?.(),
    ]);
    setSpecs(s?.specs || []);
    setTracks(t?.tracks || []);
  };

  useEffect(() => {
    void refresh();
  }, []);

  const loadMilestones = async (trackId: string) => {
    const api = getApi();
    const res = await api?.list_goal_milestones?.(trackId);
    setMsMap((m) => ({ ...m, [trackId]: res?.milestones || [] }));
  };

  const toggle = async (trackId: string) => {
    const next = openId === trackId ? null : trackId;
    setOpenId(next);
    if (next && !msMap[next]) await loadMilestones(next);
  };

  const create = async (spec: GoalSpec) => {
    const api = getApi();
    if (!api?.create_goal_track) return;
    setBusy(true);
    setStatus("");
    try {
      const res = await api.create_goal_track(spec.kind, spec.label);
      if (!res.ok) {
        setStatus(res.error || "创建失败");
        return;
      }
      if (res.track_id) {
        await loadMilestones(res.track_id);
        setOpenId(res.track_id);
      }
      await refresh();
      // 目标变了 → 开场 chips 与画像注入都随之变化
      prefetchStartChips();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (track: GoalTrack) => {
    if (!confirm(`删除「${track.title}」及其全部节点？`)) return;
    setBusy(true);
    try {
      await getApi()?.delete_goal_track?.(track.track_id);
      setOpenId(null);
      await refresh();
      prefetchStartChips();
    } finally {
      setBusy(false);
    }
  };

  const setMilestone = async (
    trackId: string,
    key: string,
    patch: { due_date?: string; done?: boolean },
  ) => {
    const res = await getApi()?.set_goal_milestone?.(
      trackId,
      key,
      patch.due_date,
      patch.done,
    );
    if (res && !res.ok) setStatus(res.error || "保存失败");
    await loadMilestones(trackId);
  };

  /** 下一个未完成节点（用于卡片摘要）。 */
  const nextByTrack = useMemo(() => {
    const out: Record<string, GoalMilestone | undefined> = {};
    for (const [tid, list] of Object.entries(msMap)) {
      out[tid] = list.find((m) => !m.done);
    }
    return out;
  }, [msMap]);

  const activeTracks = tracks.filter((t) => t.status === "active");

  return (
    <>
      <div className="px-4 pt-3">
        <div className="mx-auto flex max-w-2xl flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-surface-elevated px-3.5 py-3">
          <p className="text-sm leading-relaxed text-secondary">
            建一个目标，助手会按它的场景出调研报告、校准建议，并盯住关键时间节点。
          </p>
          <div className="flex shrink-0 gap-2">
            {specs.map((s) => (
              <Tooltip
                key={s.kind}
                label={
                  s.draft
                    ? "该轨道仍在完善，可建但不建议正式使用"
                    : `含 ${s.output_fields.length} 列对比字段、${s.milestone_count} 个节点`
                }
              >
                <Button
                  onClick={() => void create(s)}
                  disabled={busy}
                  className={s.draft ? "h-8 px-3 py-1.5 opacity-50" : "h-8 px-3 py-1.5"}
                >
                  <Plus className="h-3.5 w-3.5" />
                  {s.label}
                  {s.draft && <span className="text-xs">（敬请期待）</span>}
                </Button>
              </Tooltip>
            ))}
          </div>
        </div>
        {status && (
          <p className="mx-auto mt-1.5 max-w-2xl text-xs text-error">{status}</p>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {activeTracks.length === 0 ? (
          <EmptyState
            icon={Target}
            title="还没有目标"
            hint="点上方按钮建一个。建好后，调研会自动按该场景的结构输出，节点到期前也会提醒你。"
          />
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-2">
            {activeTracks.map((t) => {
              const open = openId === t.track_id;
              const next = nextByTrack[t.track_id];
              const nextDays = next ? daysFromToday(next.due_at) : null;
              return (
                <div
                  key={t.track_id}
                  className="rounded-lg border border-border bg-surface-elevated"
                >
                  <button
                    onClick={() => void toggle(t.track_id)}
                    className="flex w-full items-center gap-3 px-3.5 py-3 text-left"
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent-strong">
                      <Target className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-base text-primary">{t.title}</div>
                      <div className="truncate text-xs text-tertiary">
                        {t.kind_label}
                        {t.cycle_year ? ` · ${t.cycle_year} 届` : ""}
                        {next && nextDays !== null
                          ? ` · ${next.label} ${countdownLabel(nextDays).text}`
                          : ""}
                      </div>
                    </div>
                    <ChevronDown
                      className={`h-4 w-4 shrink-0 text-tertiary transition-transform ${open ? "rotate-180" : ""}`}
                    />
                  </button>

                  {open && (
                    <div className="border-t border-border px-3.5 py-2">
                      {(msMap[t.track_id] || []).length === 0 ? (
                        <p className="py-2 text-xs text-tertiary">暂无节点</p>
                      ) : (
                        <div className="flex flex-col">
                          {(msMap[t.track_id] || []).map((m) => {
                            const days = daysFromToday(m.due_at);
                            const label = countdownLabel(days);
                            return (
                              <div
                                key={m.key}
                                className="flex items-center gap-2.5 border-b border-border py-2 last:border-b-0"
                              >
                                <button
                                  onClick={() =>
                                    void setMilestone(t.track_id, m.key, {
                                      done: !m.done,
                                    })
                                  }
                                  aria-label={m.done ? "标记未完成" : "标记完成"}
                                  className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border ${
                                    m.done
                                      ? "border-success bg-success text-white"
                                      : "border-border-strong text-transparent hover:border-accent"
                                  }`}
                                >
                                  <Check className="h-3 w-3" />
                                </button>
                                <CircleDot
                                  className={`h-3.5 w-3.5 shrink-0 ${
                                    m.done ? "text-tertiary" : "text-accent"
                                  }`}
                                />
                                <span
                                  className={`w-24 shrink-0 truncate text-sm ${
                                    m.done
                                      ? "text-tertiary line-through"
                                      : "text-primary"
                                  }`}
                                >
                                  {m.label}
                                </span>
                                <Input
                                  type="date"
                                  value={toDateInput(m.due_at)}
                                  onChange={(e) => {
                                    const v = e.target.value;
                                    if (v)
                                      void setMilestone(t.track_id, m.key, {
                                        due_date: v,
                                      });
                                  }}
                                  className="h-7 w-36 shrink-0 px-2 text-xs"
                                />
                                <span className={`shrink-0 text-xs ${label.tone}`}>
                                  {label.text}
                                </span>
                                {m.note && (
                                  <Tooltip label={m.note}>
                                    <CalendarClock className="h-3.5 w-3.5 shrink-0 text-tertiary" />
                                  </Tooltip>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      <div className="mt-1 flex items-center justify-between">
                        <span className="text-xs text-tertiary">
                          改过日期的节点会被钉住，调整届数时不会被覆盖
                        </span>
                        <IconButton
                          onClick={() => void remove(t)}
                          aria-label="删除目标"
                          disabled={busy}
                        >
                          <Trash2 className="h-4 w-4" />
                        </IconButton>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}
