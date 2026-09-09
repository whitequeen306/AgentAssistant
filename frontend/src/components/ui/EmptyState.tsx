import type { LucideIcon } from "lucide-react";

/** Shared empty-state: soft icon tile + title + hint, centered. */
export function EmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: LucideIcon;
  title: string;
  hint?: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <span className="mb-1 flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-surface-elevated text-tertiary shadow-[var(--shadow-ambient)]">
        <Icon className="h-5 w-5" />
      </span>
      <p className="text-md font-semibold text-primary">{title}</p>
      {hint && <p className="max-w-sm text-sm leading-relaxed text-tertiary">{hint}</p>}
    </div>
  );
}
