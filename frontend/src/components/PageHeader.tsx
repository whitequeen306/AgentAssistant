import type { ReactNode } from "react";
import { useWindowDrag } from "@/lib/drag";

/** Unified page top bar: drag region + display-font title + right slot. */
export function PageHeader({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  const { onPointerDown } = useWindowDrag();
  return (
    <header
      onPointerDown={onPointerDown}
      className="flex h-11 shrink-0 cursor-default items-center justify-between gap-2 border-b border-border px-4"
      style={{
        background: "color-mix(in srgb, var(--color-surface-elevated) 45%, transparent)",
      }}
    >
      <span className="truncate font-[family-name:var(--font-display)] text-md font-semibold tracking-tight text-primary">
        {title}
      </span>
      <div className="flex items-center gap-0.5">{children}</div>
    </header>
  );
}
