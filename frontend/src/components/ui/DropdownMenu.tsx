import { type ReactNode } from "react";
import * as Drop from "@radix-ui/react-dropdown-menu";
import { cn } from "@/lib/cn";

export interface DropdownItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
}

export function DropdownMenu({
  trigger,
  items,
  align = "end",
}: {
  trigger: ReactNode;
  items: DropdownItem[];
  align?: "start" | "center" | "end";
}) {
  return (
    <Drop.Root>
      <Drop.Trigger asChild>{trigger}</Drop.Trigger>
      <Drop.Portal>
        <Drop.Content
          align={align}
          sideOffset={4}
          className="z-[100] min-w-[140px] rounded-md border border-border bg-surface p-1 shadow-[var(--shadow-drop)] data-[state=open]:animate-[fade_0.12s_ease-out]"
        >
          {items.map((it) => (
            <Drop.Item
              key={it.label}
              onSelect={(e) => {
                e.preventDefault();
                it.onSelect();
              }}
              className={cn(
                "flex cursor-pointer select-none items-center gap-2 rounded-sm px-3 py-1.5 text-sm outline-none data-[highlighted]:bg-surface-elevated",
                it.danger ? "text-error" : "text-primary",
              )}
            >
              {it.icon}
              {it.label}
            </Drop.Item>
          ))}
        </Drop.Content>
      </Drop.Portal>
    </Drop.Root>
  );
}
