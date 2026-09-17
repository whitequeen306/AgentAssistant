import { DueMilestoneBanner } from "@/components/DueMilestoneBanner";
import { useEffect, useRef } from "react";
import { Moon, Sun, Mic, X, Maximize2, Shrink } from "lucide-react";
import { ChatMessage } from "@/components/ChatMessage";
import { ChatInput } from "@/components/ChatInput";
import { StartChips, useStartChips } from "@/components/StartChips";
import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { VoiceWave } from "@/components/VoiceWave";
import { PageHeader } from "@/components/PageHeader";
import { IconButton } from "@/components/ui";
import { Tooltip } from "@/components/ui/Tooltip";
import { actions, useStore } from "@/lib/store";
import { getApi } from "@/lib/bridge";
import { useThemeToggle } from "@/lib/theme";
import { togglePtt } from "@/lib/voice";
import type { ContextAttachment } from "@/types";

export function ChatPage() {
  const chat = useStore((s) => s.chat);
  const activeConv = useStore((s) => s.activeConv);
  const conversations = useStore((s) => s.conversations);
  const state = useStore((s) => s.state);
  const maximized = useStore((s) => s.maximized);
  const { isDark, toggle } = useThemeToggle();
  const scrollRef = useRef<HTMLDivElement>(null);

  const conv = conversations.find((c) => c.id === activeConv);
  const title = conv?.title || "新会话";
  const inputActive = state === "main";
  const chips = useStartChips(chat);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat]);

  useEffect(() => {
    void getApi()
      ?.is_maximized?.()
      .then((v) => actions.setMaximized(!!v))
      .catch(() => {});
  }, [state]);

  const send = (
    text: string,
    contextAttachment?: ContextAttachment,
    research?: boolean,
  ) => {
    actions.sendMessage(text);
    getApi()
      ?.send_message(text, undefined, undefined, contextAttachment, research)
      .catch(() => {
        actions.endThought();
        actions.setThinking(false);
        actions.addSystemLine("发送失败，请重试", "error");
      });
  };

  const toggleMax = () => {
    getApi()
      ?.toggle_maximize?.()
      .then((v) => actions.setMaximized(!!v))
      .catch(() => {});
  };

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader title={title}>
        <Tooltip label="切换主题">
          <IconButton onClick={toggle} aria-label="切换主题">
            {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </IconButton>
        </Tooltip>
        <Tooltip label="语音模式">
          <IconButton onClick={() => togglePtt()} aria-label="语音">
            <Mic className="h-4 w-4" />
          </IconButton>
        </Tooltip>
        <DockMinimizeButton />
        <Tooltip label={maximized ? "缩小主窗口" : "最大化"}>
          <IconButton
            onClick={toggleMax}
            aria-label={maximized ? "缩小主窗口" : "最大化"}
          >
            {maximized ? (
              <Shrink className="h-4 w-4" />
            ) : (
              <Maximize2 className="h-4 w-4" />
            )}
          </IconButton>
        </Tooltip>
        <Tooltip label="关闭">
          <IconButton
            onClick={() => getApi()?.quit_app().catch(() => {})}
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </Tooltip>
      </PageHeader>

      <div
        ref={scrollRef}
        className="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto px-4 py-4"
      >
        <DueMilestoneBanner />
        {chat.map((item) => (
          <ChatMessage key={item.id} item={item} />
        ))}
        {chat.length === 0 && (
          <StartChips
            chips={chips}
            onSend={(text) => send(text)}
            onDraft={() => {
              /* draft chips fill via ChatInput below */
            }}
            onPractice={(kind, sourceId) => actions.startPractice({ kind, sourceId })}
          />
        )}
      </div>

      <VoiceWave />

      <ChatInput
        active={inputActive}
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
        onSend={send}
        autoFocus
        enableContextSources
        startChips={chips.filter((c) => !!c.draft)}
      />
    </section>
  );
}
