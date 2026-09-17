import { useState } from "react";
import {
  Plus,
  Search,
  MessageSquare,
  Boxes,
  GraduationCap,
  Settings,
  Pin,
  Trash2,
  UserRoundPlus,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { actions, useStore } from "@/lib/store";
import {
  newConversation,
  switchConversation,
  deleteConversation,
  pinConversation,
  renameConversation,
  refreshConversations,
} from "@/lib/conversations";
import { ContextMenu } from "@/components/ui/ContextMenu";
import { StudyProfileDialog } from "@/components/StudyProfileDialog";
import type { Page } from "@/types";

const NAV: { page: Page; label: string; icon: typeof MessageSquare }[] = [
  { page: "chat", label: "对话", icon: MessageSquare },
  { page: "scenes", label: "场景", icon: Boxes },
  { page: "study", label: "学习库", icon: GraduationCap },
  { page: "settings", label: "设置", icon: Settings },
];

export function Sidebar() {
  const page = useStore((s) => s.page);
  const collapsed = page !== "chat";
  const [query, setQuery] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  // 画像已填 → 入口从引导文案变为摘要展示（专业 · 目标）。
  const profileMajor = useStore((s) => (s.settings.profile_major || "").trim());
  const profileGoal = useStore((s) => (s.settings.profile_goal || "").trim());
  const hasProfile = !!(profileMajor || profileGoal);
  const profileSummary = [profileMajor, profileGoal].filter(Boolean).join(" · ");

  return (
    <aside
      className={cn(
        "glass-sidebar relative z-[2] flex shrink-0 flex-col gap-2 border-r border-border p-3",
        "transition-[width,padding] duration-[var(--duration-slow)] ease-[var(--ease-standard)]",
        collapsed ? "w-14 p-2" : "w-60",
      )}
    >
      {!collapsed && (
        <>
          <button
            onClick={newConversation}
            className="interactive-morph flex h-9 items-center gap-2 rounded-md bg-accent-soft px-3 text-sm font-medium text-accent-strong hover:brightness-105"
          >
            <Plus className="h-4 w-4" />
            <span className="morph-label">新建会话</span>
          </button>
          <button
            onClick={() => setProfileOpen(true)}
            title={
              hasProfile
                ? "更新你的学习画像"
                : "补充一下你自己，让我更好地认识你"
            }
            className={cn(
              "interactive-morph flex h-8 items-center gap-2 rounded-md border px-3 text-xs hover:border-accent/60 hover:text-accent-strong",
              hasProfile
                ? "border-solid border-border text-primary"
                : "border-dashed border-border text-secondary",
            )}
          >
            <UserRoundPlus className={cn("h-3.5 w-3.5 shrink-0", hasProfile && "text-accent")} />
            <span className="truncate">
              {hasProfile ? profileSummary : "补充一下你自己，让我更好地认识你"}
            </span>
          </button>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tertiary" />
            <input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                refreshConversations(e.target.value.trim());
              }}
              placeholder="搜索会话"
              className="h-[30px] w-full rounded-md border border-border bg-surface-sunken pl-8 pr-2 text-sm text-primary outline-none transition-[border-color,box-shadow] duration-[var(--duration-fast)] focus:border-accent focus:shadow-[var(--shadow-focus)]"
            />
          </div>
          <ConversationList />
        </>
      )}
      <nav
        className={cn(
          "flex shrink-0 border-t border-border",
          collapsed ? "flex-col flex-1 gap-1 border-t-0 pt-0" : "justify-around pt-2",
        )}
      >
        {NAV.map(({ page: p, label, icon: Icon }) => (
          <button
            key={p}
            onClick={() => actions.setPage(p)}
            title={label}
            aria-label={label}
            className={cn(
              "interactive-morph-nav flex min-w-0 items-center justify-center whitespace-nowrap rounded-md",
              collapsed ? "flex-col gap-0.5 px-1 py-2 text-[11px]" : "flex-1 px-2 py-2",
              page === p
                ? "bg-accent-soft text-accent-strong font-semibold shadow-[var(--shadow-ambient)]"
                : "text-secondary hover:bg-surface-elevated hover:text-primary",
            )}
          >
            <Icon className={cn("shrink-0", collapsed ? "h-[18px] w-[18px]" : "h-5 w-5")} strokeWidth={page === p ? 2.2 : 1.8} />
            <span className={cn("morph-label truncate", !collapsed && "sr-only")}>{label}</span>
          </button>
        ))}
      </nav>
      <StudyProfileDialog open={profileOpen} onOpenChange={setProfileOpen} />
    </aside>
  );
}

function ConversationList() {
  const conversations = useStore((s) => s.conversations);
  const activeConv = useStore((s) => s.activeConv);

  return (
    <ul className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
      {conversations.map((c) => {
        const items = [
          {
            label: c.pinned ? "取消固定" : "固定",
            onSelect: () => pinConversation(c.id, !c.pinned),
          },
          {
            label: "重命名",
            onSelect: () => {
              const t = prompt("重命名会话", c.title || "");
              if (t !== null) renameConversation(c.id, t.trim());
            },
          },
          { label: "删除", danger: true, onSelect: () => deleteConversation(c.id) },
        ];
        return (
          <ContextMenu key={c.id} items={items}>
            <li
              onClick={() => switchConversation(c.id)}
              className={cn(
                "interactive-morph group relative flex cursor-pointer items-center rounded-md px-2 py-1.5 text-sm",
                c.id === activeConv
                  ? "bg-accent-soft font-medium text-accent-strong"
                  : "text-secondary hover:bg-surface-elevated hover:text-primary",
              )}
            >
              {c.pinned && <Pin className="mr-1.5 h-3 w-3 shrink-0 text-accent" />}
              <span className="morph-label truncate">{c.title || "新会话"}</span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  deleteConversation(c.id);
                }}
                className="ml-auto hidden rounded-sm p-0.5 text-tertiary hover:bg-surface-sunken hover:text-error group-hover:block"
                title="删除"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          </ContextMenu>
        );
      })}
    </ul>
  );
}
