// Measures how long the built single-file bundle takes to reach a usable
// DOM on this machine (where fonts.googleapis.com is unreachable) —
// quantifies the white-screen period caused by the render-blocking font CSS.
import { chromium } from "playwright";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const BUILT = resolve(import.meta.dirname, "../../src/agent_assistant/ui/web/index.html");
const URL = pathToFileURL(BUILT).href;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();

// inject the same bridge mock as e2e/verify.mjs so pywebview.api exists
// (without it the app legitimately waits 15s for the bridge → skews numbers)
await page.addInitScript(() => {
  window.pywebview = {
    api: {
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
      async is_maximized() { return false; },
      async send_message() {},
      async switch_conversation() { return []; },
    },
  };
});

const t0 = Date.now();
let firstPaint = null;
page.on("domcontentloaded", () => {
  firstPaint = Date.now() - t0;
  console.log(`domcontentloaded: +${firstPaint}ms`);
});
try {
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 45000 });
} catch (e) {
  console.log("goto timed out at", Date.now() - t0, "ms:", String(e).slice(0, 120));
}
const tLoad = Date.now() - t0;
console.log(`goto(domcontentloaded) returned after: ${tLoad}ms`);

// how long until the app is actually interactive (input mounted)?
try {
  await page.waitForSelector("textarea", { timeout: 60000 });
  console.log(`interactive (textarea mounted): +${Date.now() - t0}ms`);
} catch {
  console.log("interactive: NOT reached within 60s");
}
await browser.close();
