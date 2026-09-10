// One-off: screenshot the redesigned ContextSourcesModal (paperclip picker).
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const OUT = resolve(import.meta.dirname, "../shots");
mkdirSync(OUT, { recursive: true });

function installMock() {
  const api = {
    async get_init_data() {
      return {
        settings: { theme: "light" },
        conversations: [{ id: "c1", title: "t", pinned: false }],
        active_conversation: "c1",
        messages: [],
        scenes: [],
        tools: [],
        state: "main",
        model: "m",
        version: "0.2.0",
        drag_params: { maxDrag: 200, settleThreshold: 100, mainHeight: 885, dockedHeight: 48 },
      };
    },
    async list_conversations() { return [{ id: "c1", title: "t", pinned: false }]; },
    async list_notes() {
      return [
        { filename: "a.md", title: "Rust 所有权速记", mtime: Date.now() / 1000, size: 1 },
        { filename: "b.md", title: "K8s 排障清单", mtime: Date.now() / 1000, size: 2 },
        { filename: "c.md", title: "考研调研报告", mtime: Date.now() / 1000, size: 3 },
      ];
    },
    async read_note() { return { ok: true, content: "" }; },
    async pick_local_files() { return ["C:/demo/report.pdf"]; },
    async list_knowledge_files() { return []; },
    async send_message() {},
    async switch_conversation() { return []; },
    async is_maximized() { return false; },
  };
  window.pywebview = { api };
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 880, height: 640 },
  deviceScaleFactor: 2,
});
const page = await context.newPage();
await context.addInitScript(installMock);
await page.goto("http://localhost:5199/", { waitUntil: "networkidle" });
await page.waitForSelector("textarea", { timeout: 10000 });
await page.waitForTimeout(400);

// open the picker
await page.locator('button[title*="资料来源"]').click();
await page.waitForTimeout(400);
await page.screenshot({ path: resolve(OUT, "context-modal-main.png") });
console.log("shot: context-modal-main");

// notes step
await page.getByText("资料库", { exact: false }).first().click();
await page.waitForTimeout(400);
await page.screenshot({ path: resolve(OUT, "context-modal-notes.png") });
console.log("shot: context-modal-notes");

// select two notes → back → main with chips
await page.getByRole("button", { name: "Rust 所有权速记" }).click();
await page.getByRole("button", { name: "K8s 排障清单" }).click();
await page.getByRole("button", { name: /完成选择/ }).click();
await page.getByRole("button", { name: "确认" }).click();
await page.waitForTimeout(400);
await page.screenshot({ path: resolve(OUT, "context-attached-chips.png") });
console.log("shot: context-attached-chips");

await browser.close();
