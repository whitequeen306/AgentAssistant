import { Sidebar } from "@/components/Sidebar";
import { PageTransition } from "@/components/PageTransition";
import { ResizeHandles } from "@/components/ResizeHandles";
import { ChatPage } from "@/pages/ChatPage";
import { ScenesPage } from "@/pages/ScenesPage";
import { StudyPage } from "@/pages/StudyPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { AboutPage } from "@/pages/AboutPage";
import { useStore } from "@/lib/store";

/** main: frosted glass card — sidebar + content with smooth page morph. */
export function MainView() {
  const page = useStore((s) => s.page);

  return (
    <div className="glass-panel relative flex h-full w-full overflow-hidden rounded-xl">
      <ResizeHandles />
      {/* subtle inner highlight + brand tint for depth */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-xl opacity-70"
        style={{
          background:
            "radial-gradient(120% 80% at 0% 0%, rgba(255,255,255,0.28), transparent 55%), radial-gradient(90% 70% at 100% 100%, rgba(0,122,255,0.05), transparent 60%)",
        }}
      />
      <Sidebar />
      <main
        className="relative z-[2] flex min-w-0 flex-1 flex-col"
        style={{
          background: "color-mix(in srgb, var(--color-surface-sunken) 38%, transparent)",
        }}
      >
        <PageTransition page={page}>
          {page === "chat" && <ChatPage />}
          {page === "scenes" && <ScenesPage />}
          {page === "study" && <StudyPage />}
          {page === "settings" && <SettingsPage />}
          {page === "about" && <AboutPage />}
        </PageTransition>
      </main>
    </div>
  );
}
