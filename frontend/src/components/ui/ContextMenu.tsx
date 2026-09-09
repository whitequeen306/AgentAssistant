import { type ReactNode } from "react";
import * as CtxMenu from "@radix-ui/react-context-menu";
import { cn } from "@/lib/cn";

export interface ContextMenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
}

/** Right-click menu built on Radix ContextMenu. Wrap any element as the trigger. */
export function ContextMenu({
  items,
  children,
}: {
  items: ContextMenuItem[];
  children: ReactNode;
}) {
  return (
    <CtxMenu.Root>
      <CtxMenu.Trigger asChild>{children}</CtxMenu.Trigger>
      <CtxMenu.Portal>
        <CtxMenu.Content
          className="z-[100] min-w-[120px] rounded-md border border-border bg-surface p-1 shadow-[var(--shadow-drop)] data-[state=open]:animate-[fade_0.12s_ease-out]"
        >
          {items.map((it) => (
            <CtxMenu.Item
              key={it.label}
              onSelect={(e) => {
                e.preventDefault();
                it.onSelect();
              }}
              className={cn(
                "flex cursor-pointer select-none items-center rounded-sm px-3 py-1.5 text-sm outline-none data-[highlighted]:bg-surface-elevated",
                it.danger ? "text-error" : "text-primary",
              )}
            >
              {it.label}
            </CtxMenu.Item>
          ))}
        </CtxMenu.Content>
      </CtxMenu.Portal>
    </CtxMenu.Root>
  );
}
