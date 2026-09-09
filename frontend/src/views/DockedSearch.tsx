import { useEffect, useState } from "react";
import { ArrowUp, Maximize2, Mic } from "lucide-react";
import { StatusDot } from "@/components/ui/StatusDot";
import { actions, useStore } from "@/lib/store";
import { getApi, goState } from "@/lib/bridge";
import { useWindowDrag } from "@/lib/drag";
import { togglePtt } from "@/lib/voice";

/** Pure dark capsule — matches dark main chrome. */
const BAR_BG = "#0c0c0e";
const TIP_BG = "#1c3a5f";

/**
 * docked-search: stadium pill. Confirm tip is a contrasting strip *below*
 * the bar (window grows via set_docked_confirm_banner). Incoming confirms
 * also auto-expand to main so the Allow/Deny modal is never missed.
 */
export function DockedSearch() {
  const [value, setValue] = useState("");
  const confirmTip = useStore((s) => s.confirmTip);
  const confirmRequest = useStore((s) => s.confirmRequest);
  const { onPointerDown } = useWindowDrag();
  const showConfirmTip = !!confirmTip;

  useEffect(() => {
    void getApi()?.set_docked_confirm_banner?.(showConfirmTip).catch(() => {});
  }, [showConfirmTip]);

  useEffect(() => {
    return () => {
      void getApi()?.set_docked_confirm_banner?.(false).catch(() => {});
    };
  }, []);

  const send = async () => {
    const t = value.trim();
    if (!t) return;
    setValue("");
    const api = getApi();
    if (api) {
      try {
        const conv = await api.new_conversation();
        actions.loadMessages([]);
        const list = await api.list_conversations();
        actions.setConversations(list, conv.id);
      } catch {
        /* fall through */
      }
    }
    actions.sendMessage(t);
    api?.send_message(t).catch(() => {});
    goState("popup-chat");
  };

  const openConfirmInMain = () => {
    actions.clearConfirmTip();
    actions.setPage("chat");
    goState("main");
    if (!confirmRequest) {
      actions.addSystemLine("确认请求已结束或超时，可在对话里重试该操作", "info");
    }
  };

  return (
    <div
      className="flex h-full w-full flex-col overflow-hidden"
      style={{ background: showConfirmTip ? TIP_BG : BAR_BG }}
    >
      <div
        onPointerDown={onPointerDown}
        className="relative flex h-12 w-full shrink-0 cursor-grab items-center gap-1.5 px-3.5 active:cursor-grabbing"
        style={{
          background: BAR_BG,
          borderRadius: showConfirmTip ? "16px 16px 0 0" : undefined,
        }}
      >
        <StatusDot />
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="让我来帮你全网搜索一些事情？"
          autoComplete="off"
          className="min-w-0 flex-1 cursor-text border-none bg-transparent text-md text-[#f2f2f7] placeholder:text-[#636366] outline-none"
        />
        <button
          data-no-drag
          onClick={() => togglePtt()}
          aria-label="语音输入"
          className="interactive-morph inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-pill text-[#aeaeb2] hover:text-[#f2f2f7]"
        >
          <Mic className="h-4 w-4" />
        </button>
        <button
          data-no-drag
          onClick={() => void send()}
          aria-label="发送"
          className="interactive-morph inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-pill bg-[#0a84ff] text-white"
        >
          <ArrowUp className="h-4 w-4" />
        </button>
        <button
          data-no-drag
          onClick={() => {
            if (confirmTip) actions.clearConfirmTip();
            goState("main");
          }}
          aria-label="展开为主窗口"
          className="interactive-morph inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-pill text-[#636366] hover:text-[#f2f2f7]"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {showConfirmTip && (
        <button
          type="button"
          data-no-drag
          onClick={openConfirmInMain}
          className="flex h-11 w-full shrink-0 items-center justify-between gap-2 px-4 text-left"
          style={{ background: TIP_BG }}
        >
          <span className="truncate text-sm font-medium text-[#f2f2f7]">
            需要确认
            {confirmTip?.tool ? (
              <span className="font-normal text-[#8eb7e8]">
                {" · "}
                {confirmTip.tool}
              </span>
            ) : null}
            {!confirmRequest ? (
              <span className="font-normal text-[#8eb7e8]">（已超时）</span>
            ) : null}
          </span>
          <span className="shrink-0 rounded-sm bg-[#0a84ff] px-2 py-0.5 text-xs text-white">
            点击打开
          </span>
        </button>
      )}
    </div>
  );
}
