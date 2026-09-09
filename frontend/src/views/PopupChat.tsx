import { useEffect, useRef } from "react";
import { Maximize2, X } from "lucide-react";
import { ChatMessage } from "@/components/ChatMessage";
import { ChatInput } from "@/components/ChatInput";
import { IconButton } from "@/components/ui";
import { Tooltip } from "@/components/ui/Tooltip";
import { cn } from "@/lib/cn";
import { actions, useStore } from "@/lib/store";
import { getApi, goState } from "@/lib/bridge";
import { useWindowDrag } from "@/lib/drag";

/** popup-chat: compact conversation dialog (docked-search → send → here). */
export function PopupChat() {
  const chat = useStore((s) => s.chat);
  const conversations = useStore((s) => s.conversations);
  const activeConv = useStore((s) => s.activeConv);
  const { onPointerDown } = useWindowDrag();
  const scrollRef = useRef<HTMLDivElement>(null);

  const conv = conversations.find((c) => c.id === activeConv);
  const title = conv?.title || "Assistant";

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat]);

  const send = (text: string) => {
    actions.sendMessage(text);
    getApi()?.send_message(text).catch(() => {});
  };

  return (
    <div className="flex h-full flex-col rounded-b-2xl bg-surface shadow-[var(--shadow-drop)]">
      <header
        onPointerDown={onPointerDown}
        className={cn("flex h-9 shrink-0 cursor-default items-center gap-2 px-3")}
      >
        <span className="h-5 w-5 shrink-0 rounded-pill bg-gradient-to-br from-accent to-[#a855f7]" />
        <span className="flex-1 truncate text-sm font-semibold text-primary">{title}</span>
        <div className="flex items-center gap-0.5">
          <Tooltip label="展开为主窗口">
            <IconButton onClick={() => goState("main")} aria-label="展开">
              <Maximize2 className="h-4 w-4" />
            </IconButton>
          </Tooltip>
          <Tooltip label="结束对话">
            <IconButton onClick={() => goState("docked-search")} aria-label="关闭">
              <X className="h-4 w-4" />
            </IconButton>
          </Tooltip>
        </div>
      </header>
      <div ref={scrollRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3">
        {chat.map((item) => (
          <ChatMessage key={item.id} item={item} />
        ))}
      </div>
      <ChatInput
        active
        compact
        placeholder="继续追问…"
        onSend={send}
      />
    </div>
  );
}
