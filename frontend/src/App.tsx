import { useEffect } from "react";
import { bootstrap, goState } from "@/lib/bridge";
import { applyTheme, useThemeToggle } from "@/lib/theme";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { OperatorControlBar } from "@/components/OperatorControlBar";
import { Toast } from "@/components/Toast";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { ViewRouter } from "@/components/ViewRouter";
import { actions, getState, useStore } from "@/lib/store";
import { switchConversation } from "@/lib/conversations";

export default function App() {
  const ready = useStore((s) => s.ready);
  const settings = useStore((s) => s.settings);
  const pendingInvoke = useStore((s) => s.pendingInvoke);

  // Bootstrap the bridge + hydrate the store once.
  useEffect(() => {
    void bootstrap();
  }, []);

  // Apply theme whenever settings land / change.
  useEffect(() => {
    applyTheme();
  }, [settings.theme, settings.accent]);

  // keep useThemeToggle's system listener attached
  useThemeToggle();

  // Consume a queued right-click / browser-text / scene-test invoke.
  useEffect(() => {
    if (!pendingInvoke) return;
    const { kind, convId } = pendingInvoke;
    actions.clearPendingInvoke();
    void (async () => {
      await switchConversation(convId);
      const line =
        kind === "file"
          ? "正在读取文件中..."
          : kind === "text"
            ? "已收到选中文本，正在理解…"
            : "正在测试场景（新会话）…";
      actions.addSystemLine(line, "info");
      actions.setThinking(true);
      if (getState().state !== "main") goState("main");
    })();
  }, [pendingInvoke]);

  // Opaque fallback while pywebview injects the bridge — transparent null
  // reads as a white WebView surface on some Windows setups.
  if (!ready) {
    return (
      <div
        className="flex h-full w-full items-center justify-center rounded-xl"
        style={{
          background: "rgba(12, 12, 14, 0.96)",
          color: "#aeaeb2",
          fontFamily: "system-ui, sans-serif",
          fontSize: 13,
        }}
      >
        正在启动…
      </div>
    );
  }

  return (
    <TooltipProvider>
      <ViewRouter />
      <ConfirmDialog />
      <Toast />
      <OperatorControlBar />
    </TooltipProvider>
  );
}
