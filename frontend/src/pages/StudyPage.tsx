import { useEffect, useState } from "react";
import { NotebookPen, Database, GraduationCap, Target } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { NotesPanel } from "@/components/study/NotesPanel";
import { KnowledgePanel } from "@/components/study/KnowledgePanel";
import { PracticeRoom } from "@/components/study/PracticeRoom";
import { PlanningPanel } from "@/components/study/PlanningPanel";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/cn";

type Tab = "notes" | "kb" | "practice" | "plan";

const TABS: { id: Tab; label: string; icon: typeof NotebookPen }[] = [
  { id: "notes", label: "资料", icon: NotebookPen },
  { id: "kb", label: "知识库", icon: Database },
  { id: "practice", label: "练习室", icon: GraduationCap },
  { id: "plan", label: "规划", icon: Target },
];

/** 学习库：资料笔记 + 向量知识库 + 练习室，三合一学习工作台。 */
export function StudyPage() {
  const [tab, setTab] = useState<Tab>("notes");
  const practiceJump = useStore((s) => s.practiceJump);

  // 「考考我」chip 跳转：切到练习室 tab（PracticeRoom 自行消费 payload）。
  useEffect(() => {
    if (practiceJump) setTab("practice");
  }, [practiceJump]);

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader title="学习库">
        <DockMinimizeButton />
      </PageHeader>
      <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-1.5">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={cn(
              "interactive-morph-nav flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm",
              tab === id
                ? "bg-accent-soft font-semibold text-accent-strong"
                : "text-secondary hover:bg-surface-elevated hover:text-primary",
            )}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>
      {tab === "notes" && <NotesPanel />}
      {tab === "kb" && <KnowledgePanel />}
      {tab === "practice" && <PracticeRoom />}
      {tab === "plan" && <PlanningPanel />}
    </section>
  );
}
