// Screenshot harness: loads the Vite dev server with a mocked pywebview
// bridge, rich fake data, and captures light/dark shots of every page.
// Usage: node scripts/ui-shots.mjs [outDir]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const OUT = resolve(import.meta.dirname, process.argv[2] || "../shots");
mkdirSync(OUT, { recursive: true });

const URL = process.env.SHOT_URL || "http://localhost:5199/";

function installMock() {
  const md = `## 调研摘要\n\n核心结论如下：\n\n1. **性能** — 冷启动优化至 \`120ms\`\n2. **成本** — 每千次调用约 ¥0.8\n\n| 指标 | 优化前 | 优化后 |\n|---|---|---|\n| P95 | 320ms | 118ms |\n| 成本 | ¥2.1 | ¥0.8 |\n\n> 详细数据见附录。`;
  const api = {
    async get_init_data() {
      // theme via query param so each shot can pin light/dark
      const theme = new URLSearchParams(location.search).get("theme") || "light";
      return {
        settings: { theme },
        conversations: [
          { id: "c1", title: "深度调研：边缘计算趋势", pinned: true },
          { id: "c2", title: "重构 storefront 组件库", pinned: false },
          { id: "c3", title: "周报整理与待办同步", pinned: false },
          { id: "c4", title: "Translate README to English", pinned: false },
        ],
        active_conversation: "c1",
        messages: [
          { id: "m1", role: "user", content: "帮我调研一下 2026 年边缘计算的发展趋势，重点看国内市场。" },
          { id: "m2", role: "assistant", content: md },
        ],
        scenes: [
          { id: "s1", name: "晨间简报", trigger_phrases: ["早报", "晨间简报"], actions: [{ tool: "web_search" }, { tool: "speak" }] },
          { id: "s2", name: "整理下载目录", trigger_phrases: ["清理下载"], actions: [{ tool: "run_command" }] },
        ],
        tools: [],
        state: "main",
        model: "deepseek-v3.2",
        version: "0.2.0",
        drag_params: { maxDrag: 200, settleThreshold: 100, mainHeight: 720, dockedHeight: 48 },
      };
    },
    async list_conversations() {
      return [
        { id: "c1", title: "深度调研：边缘计算趋势", pinned: true },
        { id: "c2", title: "重构 storefront 组件库", pinned: false },
        { id: "c3", title: "周报整理与待办同步", pinned: false },
        { id: "c4", title: "Translate README to English", pinned: false },
      ];
    },
    async list_notes() {
      return [
        { filename: "n1.md", title: "Rust 所有权速记", mtime: Date.now() / 1000 - 3600, size: 1200 },
        { filename: "n2.md", title: "K8s 排障清单", mtime: Date.now() / 1000 - 86400, size: 3400 },
        { filename: "n3.md", title: "采购比价（Q3）", mtime: Date.now() / 1000 - 172800, size: 800 },
      ];
    },
    async read_note() { return { ok: true, content: "# 笔记\n\n示例内容。" }; },
    async list_knowledge_files() {
      return [
        { file_id: "k1", filename: "handbook-v3.txt", title: "员工手册 v3", chunk_count: 128, created_at: Date.now() / 1000 - 7200 },
        { file_id: "k2", filename: "faq.md", title: "FAQ 汇总", chunk_count: 32, created_at: Date.now() / 1000 - 172800 },
      ];
    },
    async get_tool_permissions() {
      return [
        { name: "web_search", label: "网页搜索", permission: "auto" },
        { name: "run_command", label: "运行命令", permission: "ask" },
        { name: "read_file", label: "读取文件", permission: "auto" },
      ];
    },
    async is_maximized() { return false; },
    async send_message() {},
    async change_state() {},
    async begin_drag() { return { x: 0, y: 0, w: 760, h: 720 }; },
    async live_resize() {},
    async live_move() {},
    async check_edge_snap() {},
    async grow_to_main() {},
    async decide_drag_settle() { return "main"; },
    async quit_app() {},
    async switch_conversation() { return []; },
    async new_conversation() { return { id: "c9", title: "新会话" }; },
    async rename_conversation() {},
    async delete_conversation() {},
    async pin_conversation() {},
    async list_scenes() { return []; },
    async save_scene(s) { return s; },
    async delete_scene() {},
    async test_scene() {},
    async get_settings() { return { theme: "light" }; },
    async save_setting() {},
    async update_note() { return { ok: true }; },
    async delete_note() { return { ok: true }; },
    async start_dictation() { return true; },
    async stop_dictation() {},
    async dock_minimize() {},
    async toggle_maximize() { return false; },
    async get_init() { return {}; },
    async set_docked_confirm_banner() {},
    async ingest_knowledge_files() { return { cancelled: true }; },
    async delete_knowledge_file() {},
    async set_tool_permission() {},
    async open_file_picker() {},
  };
  window.pywebview = { api };
}

const browser = await chromium.launch({ headless: true });

async function shoot(name, { width = 880, height = 640, theme = "light", actions = null } = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    colorScheme: theme === "dark" ? "dark" : "light",
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  await context.addInitScript(installMock);
  await page.goto(`${URL}?theme=${theme}`, { waitUntil: "networkidle" });
  await page.waitForSelector("textarea", { timeout: 10000 });
  await page.waitForTimeout(400);
  if (actions) await actions(page);
  await page.waitForTimeout(350);
  await page.screenshot({ path: resolve(OUT, `${name}.png`) });
  await context.close();
  console.log("shot:", name);
}

// helper: navigate pages via sidebar buttons
const gotoPage = (label) => async (page) => {
  await page.getByRole("button", { name: label, exact: true }).first().click();
};

await shoot("main-chat-light");
await shoot("main-chat-dark", { theme: "dark" });
await shoot("empty-chat-chips", { actions: async (page) => {
  await page.getByRole("button", { name: "新建会话" }).first().click();
} });
await shoot("scenes-light", { actions: gotoPage("场景") });
await shoot("study-light", { actions: gotoPage("学习库") });
await shoot("study-practice", { actions: async (page) => {
  await gotoPage("学习库")(page);
  await page.getByRole("button", { name: "练习室" }).first().click();
} });
await shoot("settings-light", { actions: gotoPage("设置") });
await shoot("settings-dark", { theme: "dark", actions: gotoPage("设置") });
await shoot("about-light", { actions: async (page) => {
  await gotoPage("设置")(page);
  await page.getByText("关于 AgentAssistant").click();
} });

await browser.close();
console.log("done ->", OUT);
