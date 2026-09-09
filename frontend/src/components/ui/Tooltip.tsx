import { type ReactNode, useState } from "react";
import * as Tip from "@radix-ui/react-tooltip";
import { cn } from "@/lib/cn";

export function TooltipProvider({ children }: { children: ReactNode }) {
  return <Tip.Provider delayDuration={300} skipDelayDuration={120}>{children}</Tip.Provider>;
}

/** Hover hint. Wraps a trigger element; label is plain text. */
export function Tooltip({
  label,
  children,
  side = "bottom",
  className,
}: {
  label: string;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Tip.Root open={open} onOpenChange={setOpen} delayDuration={300}>
      <Tip.Trigger asChild
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className={cn("outline-none", className)}
      >
        {children}
      </Tip.Trigger>
      <Tip.Portal>
        <Tip.Content
          side={side}
          sideOffset={6}
          className="z-[120] rounded-sm border border-border bg-surface px-2 py-1 text-xs text-secondary shadow-[var(--shadow-ambient)] data-[state=delayed-open]:animate-[fade_0.12s_ease-out]"
        >
          {label}
          <Tip.Arrow className="fill-surface" />
        </Tip.Content>
      </Tip.Portal>
    </Tip.Root>
  );
}
