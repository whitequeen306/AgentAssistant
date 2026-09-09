import { useState } from "react";
import { NotebookPen, Database, GraduationCap } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { NotesPanel } from "@/components/study/NotesPanel";
import { KnowledgePanel } from "@/components/study/KnowledgePanel";
import { PracticeRoom } from "@/components/study/PracticeRoom";
import { cn } from "@/lib/cn";

type Tab = "notes" | "kb" | "practice";

const TABS: { id: Tab; label: string; icon: typeof NotebookPen }[] = [
  { id: "notes", label: "资料", icon: NotebookPen },
  { id: "kb", label: "知识库", icon: Database },
  { id: "practice", label: "练习室", icon: GraduationCap },
];

/** 学习库：资料笔记 + 向量知识库 + 练习室，三合一学习工作台。 */
export function StudyPage() {
  const [tab, setTab] = useState<Tab>("notes");

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
    </section>
  );
}
