import { useEffect } from "react";
import { X, BellRing } from "lucide-react";
import { actions, useStore } from "@/lib/store";
import { switchConversation } from "@/lib/conversations";

const AUTO_DISMISS_MS = 8000;

/**
 * Top slide-down toast for background reports (perf anomaly, morning briefing).
 * Lives outside the chat list on purpose — daemon deliveries must not inject
 * lines into whatever conversation the user happens to be viewing.
 */
export function Toast() {
  const toast = useStore((s) => s.toast);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => actions.dismissToast(), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [toast]);

  if (!toast) return null;

  const open = () => {
    actions.dismissToast();
    if (toast.convId) void switchConversation(toast.convId);
  };

  return (
    <div className="pointer-events-none fixed inset-x-0 top-2 z-50 flex justify-center">
      <div
        role="status"
        className="pointer-events-auto flex max-w-[85%] items-center gap-2 rounded-md border border-accent/50 bg-surface-elevated px-3 py-2 shadow-lg animate-[toast-in_0.25s_ease-out]"
      >
        <BellRing className="h-3.5 w-3.5 shrink-0 text-accent" />
        <button
          type="button"
          onClick={open}
          title={toast.convId ? "点击查看" : undefined}
          className={
            "truncate text-xs " +
            (toast.convId ? "cursor-pointer text-accent hover:underline" : "text-secondary")
          }
        >
          {toast.text}
        </button>
        <button
          type="button"
          onClick={() => actions.dismissToast()}
          aria-label="关闭通知"
          className="shrink-0 cursor-pointer text-tertiary hover:text-primary"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
