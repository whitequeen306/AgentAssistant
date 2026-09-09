import { type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

/** Centered frosted modal on a soft scrim. */
export function Modal({
  open,
  onOpenChange,
  title,
  children,
  footer,
  className,
  /** When true, overlay / Esc / X cannot dismiss — only caller controls close. */
  dismissible = true,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
  dismissible?: boolean;
}) {
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        // Block Radix auto-dismiss (overlay / Esc) when not dismissible.
        if (!next && !dismissible) return;
        onOpenChange(next);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-[var(--color-overlay)] backdrop-blur-[6px] data-[state=open]:animate-[fade_0.2s_ease-out]" />
        <Dialog.Content
          className={cn(
            "glass-panel-strong fixed left-1/2 top-1/2 z-50 flex max-h-[88%] w-[min(420px,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col gap-3 overflow-y-auto rounded-xl p-5 data-[state=open]:animate-[pop_0.22s_var(--ease-standard)]",
            className,
          )}
          onPointerDownOutside={(e) => {
            if (!dismissible) e.preventDefault();
          }}
          onInteractOutside={(e) => {
            if (!dismissible) e.preventDefault();
          }}
          onEscapeKeyDown={(e) => {
            if (!dismissible) e.preventDefault();
          }}
        >
          {title !== undefined && (
            <div className="flex items-center justify-between">
              <Dialog.Title className="font-[family-name:var(--font-display)] text-lg font-semibold text-primary">
                {title}
              </Dialog.Title>
              {dismissible ? (
                <Dialog.Close asChild>
                  <button
                    className="interactive-morph inline-flex h-7 w-7 items-center justify-center rounded-sm text-tertiary hover:text-primary"
                    aria-label="关闭"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </Dialog.Close>
              ) : (
                <span className="inline-block h-7 w-7" aria-hidden />
              )}
            </div>
          )}
          {children}
          {footer && <div className="flex justify-end gap-2">{footer}</div>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export const modalKeyframes = `
@keyframes fade { from{opacity:0} to{opacity:1} }
@keyframes pop { from{opacity:0;transform:translate(-50%,-48%) scale(.97)} to{opacity:1;transform:translate(-50%,-50%) scale(1)} }
`;
