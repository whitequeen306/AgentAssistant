import { useEffect, useState } from "react";
import { Eye, EyeOff, Info, Loader2, RefreshCw } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { PageHeader } from "@/components/PageHeader";
import { Toggle } from "@/components/ui/Toggle";
import { Select } from "@/components/ui/Select";
import { cn } from "@/lib/cn";
import { actions, useStore } from "@/lib/store";
import { getApi } from "@/lib/bridge";
import { applyTheme } from "@/lib/theme";
import type { SelectOption } from "@/components/ui/Select";
import type { ToolPermission } from "@/types";

interface Item {
  key: string;
  label: string;
  type: "toggle" | "select" | "text" | "password" | "color";
  options?: string[];
  labels?: string[];
  hint?: string;
  placeholder?: string;
}
interface Group {
  group: string;
  items: Item[];
}

const SCHEMA: Group[] = [
  {
    group: "外观",
    items: [
      {
        key: "theme",
        label: "主题",
        type: "select",
        options: ["system", "light", "dark"],
        labels: ["跟随系统", "浅色", "深色"],
      },
      { key: "accent", label: "主题色", type: "color" },
    ],
  },
  {
    group: "窗口",
    items: [
      {
        key: "startup_state",
        label: "启动形态",
        type: "select",
        options: ["main", "docked-search"],
        labels: ["主页面", "搜索栏"],
      },
      { key: "edge_snap", label: "边缘吸附", type: "toggle" },
    ],
  },
  {
    group: "功能开关",
    items: [{ key: "feature_right_click", label: "右键快捷菜单", type: "toggle" }],
  },
  {
    group: "安全",
    items: [
      {
        key: "file_jail_roots",
        label: "文件操作工作区",
        type: "text",
        hint: "分号分隔多个目录；留空 = 默认（桌面/文档/下载/图片/音乐/视频 + 数据目录）；填 off 关闭围栏。围栏外的文件操作会先询问",
      },
    ],
  },
];

export function SettingsPage() {
  const settings = useStore((s) => s.settings);
  const [refreshKey, setRefreshKey] = useState(0);

  const save = (key: string, value: string) => {
    actions.setSetting(key, value);
    getApi()?.save_setting(key, value).catch(() => {});
    if (key === "theme" || key === "accent") applyTheme();
  };

  // Refresh once on mount to pick up any backend-side defaults.
  useEffect(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader title="设置">
        <DockMinimizeButton />
      </PageHeader>
      <div key={refreshKey} className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto flex max-w-2xl flex-col gap-5">
          {SCHEMA.map((grp) => (
            <div key={grp.group}>
              <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-tertiary">
                <span className="h-2.5 w-0.5 rounded-pill bg-accent" />
                {grp.group}
              </h4>
              <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface-elevated">
                {grp.items.map((item, i) => (
                  <div
                    key={item.key}
                    className={cn(
                      "flex items-center justify-between gap-3 px-3.5 py-2.5",
                      i > 0 && "border-t border-border",
                    )}
                  >
                    <div className="flex min-w-0 flex-col">
                      <span className="text-base text-primary">{item.label}</span>
                      {item.hint && (
                        <span className="mt-0.5 text-xs leading-relaxed text-tertiary">{item.hint}</span>
                      )}
                    </div>
                    <SettingControl item={item} value={settings[item.key] || ""} onSave={save} />
                  </div>
                ))}
              </div>
            </div>
          ))}
          <ProviderSection />
          <ToolPermissionSection />
          <div className="flex justify-center border-t border-border pt-4 pb-1">
            <Button variant="ghost" onClick={() => actions.setPage("about")}>
              <Info className="h-4 w-4" />
              关于 AgentAssistant
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ─── 模型（Provider）：Key 密文+小眼睛 / URL 明文 / 拉取模型下拉 ─── */

function ProviderSection() {
  const settings = useStore((s) => s.settings);
  const [showKey, setShowKey] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState("");

  const save = (key: string, value: string) => {
    actions.setSetting(key, value);
    getApi()?.save_setting(key, value).catch(() => {});
  };

  const storedKey = settings.deepseek_api_key || "";
  const storedUrl = settings.deepseek_base_url || "";

  const fetchModels = async () => {
    setFetching(true);
    setFetchError("");
    try {
      const res = await getApi()?.fetch_provider_models?.(storedKey, storedUrl);
      if (res?.ok && res.models?.length) {
        setModels(res.models);
      } else {
        setFetchError(res?.error || "未拉取到模型");
      }
    } finally {
      setFetching(false);
    }
  };

  return (
    <div>
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-tertiary">
        <span className="h-2.5 w-0.5 rounded-pill bg-accent" />
        模型（Provider）
      </h4>
      <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface-elevated">
        {/* API Key：密文 + 小眼睛 */}
        <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
          <span className="text-base text-primary">API Key</span>
          <div className="flex items-center gap-1">
            <Input
              type={showKey ? "text" : "password"}
              value={storedKey}
              placeholder={storedKey ? "" : "未配置（可在 .env 配置）"}
              onChange={(e) => {
                const v = e.target.value.trim();
                if (v) save("deepseek_api_key", v);
              }}
              className="w-48"
            />
            <button
              type="button"
              onClick={() => setShowKey((v) => !v)}
              disabled={!storedKey}
              aria-label={showKey ? "隐藏 Key" : "显示 Key"}
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-tertiary hover:bg-surface-elevated hover:text-primary disabled:opacity-40"
            >
              {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </button>
          </div>
        </div>
        {/* Base URL：明文 */}
        <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2.5">
          <span className="text-base text-primary">Base URL</span>
          <Input
            type="text"
            value={storedUrl}
            placeholder="https://api.deepseek.com"
            onChange={(e) => save("deepseek_base_url", e.target.value.trim())}
            className="w-56"
          />
        </div>
        {/* 模型：手填 + 拉取下拉 */}
        <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2.5">
          <div className="flex min-w-0 flex-col">
            <span className="text-base text-primary">模型名称</span>
            <span className="mt-0.5 text-xs leading-relaxed text-tertiary">
              可手填，或用右侧按钮按 Key 和 URL 拉取模型列表
            </span>
          </div>
          <div className="flex flex-col items-end gap-1.5">
            <div className="flex items-center gap-1">
              <Input
                type="text"
                value={settings.deepseek_model || ""}
                placeholder="deepseek-chat"
                onChange={(e) => save("deepseek_model", e.target.value.trim())}
                className="w-40"
              />
              <Button
                variant="ghost"
                className="h-8 shrink-0 px-2"
                onClick={() => void fetchModels()}
                disabled={fetching}
                aria-label="拉取模型列表"
              >
                {fetching ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
                拉取模型
              </Button>
            </div>
            {models.length > 0 && (
              <Select
                value={settings.deepseek_model || ""}
                onValueChange={(v) => save("deepseek_model", v)}
                options={models.map((m) => ({ value: m, label: m }))}
              />
            )}
            {fetchError && <p className="text-xs text-error">{fetchError}</p>}
          </div>
        </div>
        {/* 思考强度 */}
        <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2.5">
          <div className="flex min-w-0 flex-col">
            <span className="text-base text-primary">思考强度</span>
            <span className="mt-0.5 text-xs leading-relaxed text-tertiary">
              越低越快、越省额度。保存后立即生效
            </span>
          </div>
          <div className="w-28">
            <SettingControl
              item={{
                key: "reasoning_effort",
                label: "思考强度",
                type: "select",
                options: ["off", "low", "high", "max"],
                labels: ["关闭", "低", "高", "最高"],
              }}
              value={settings.reasoning_effort || ""}
              onSave={save}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function ToolPermissionSection() {
  const [tools, setTools] = useState<ToolPermission[]>([]);

  useEffect(() => {
    getApi()?.get_tool_permissions().then(setTools).catch(() => {});
  }, []);

  const change = (name: string, mode: "auto" | "ask") => {
    setTools((prev) => prev.map((t) => (t.name === name ? { ...t, permission: mode } : t)));
    getApi()?.set_tool_permission(name, mode).catch(() => {});
  };

  if (tools.length === 0) return null;

  const groups: { title: string; mode: "auto" | "ask" }[] = [
    { title: "自动运行", mode: "auto" },
    { title: "需要询问", mode: "ask" },
  ];

  return (
    <div>
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-tertiary">
        <span className="h-2.5 w-0.5 rounded-pill bg-accent" />
        工具权限
      </h4>
      <p className="mb-2 text-xs leading-relaxed text-tertiary">
        控制 Agent 调用每个工具前是否需要你确认；危险命令、系统目录操作仍会询问
      </p>
      {groups.map(({ title, mode }) => {
        const items = tools.filter((t) => t.permission === mode);
        if (items.length === 0) return null;
        return (
          <div key={mode} className="mb-3">
            <h5 className="mb-1.5 text-xs font-medium text-secondary">{title}</h5>
            <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface-elevated">
              {items.map((t) => (
                <div
                  key={t.name}
                  className="flex items-center justify-between gap-3 px-3.5 py-2.5 [&:not(:first-child)]:border-t [&:not(:first-child)]:border-border"
                >
                  <span className="text-base text-primary">
                    {t.label}
                    <span className="ml-1 text-xs text-tertiary">（{t.name}）</span>
                  </span>
                  <Toggle
                    checked={t.permission === "auto"}
                    onCheckedChange={(v) => change(t.name, v ? "auto" : "ask")}
                    aria-label={`${t.label} 权限`}
                  />
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SettingControl({
  item,
  value,
  onSave,
}: {
  item: Item;
  value: string;
  onSave: (key: string, value: string) => void;
}) {
  if (item.type === "toggle") {
    return (
      <Toggle
        checked={value === "on"}
        onCheckedChange={(v) => onSave(item.key, v ? "on" : "off")}
        aria-label={item.label}
      />
    );
  }
  if (item.type === "select" && item.options) {
    const options: SelectOption[] = item.options.map(
      (opt, i) => ({ value: opt, label: (item.labels && item.labels[i]) || opt }),
    );
    return (
      <Select value={value || item.options[0]} onValueChange={(v) => onSave(item.key, v)} options={options} />
    );
  }
  if (item.type === "color") {
    return (
      <input
        type="color"
        value={/^#[0-9A-Fa-f]{6}$/.test(value) ? value : "#007AFF"}
        onChange={(e) => onSave(item.key, e.target.value)}
        className="h-8 w-12 rounded-sm border border-border bg-surface-sunken p-0.5"
      />
    );
  }
  return (
    <Input
      type={item.type === "password" ? "password" : "text"}
      value={value}
      placeholder={item.placeholder}
      onChange={(e) => onSave(item.key, e.target.value.trim())}
      className="w-48"
    />
  );
}
