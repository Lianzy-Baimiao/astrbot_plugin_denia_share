/* Real Chromium + frontend modules + ParserConfig; isolated API transport only.
 * Run: PLAYWRIGHT_MODULE_PATH=<playwright-core path> node tests/test_config_ui.cjs
 * Optional PYTHON and CHROME_EXECUTABLE environment variables override executables.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright-core");
const root = path.resolve(__dirname, "..");
let state = { "媒体发送": { SEND_MODE: "文本+富媒体" } };
let lastPayload;
function configAPI(values = {}) {
  const result = JSON.parse(execFileSync(process.env.PYTHON || "python", [path.join(__dirname, "config_ui_fixture.py")], {
    input: JSON.stringify({ state, values }), encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  }));
  state = result.state;
  return result;
}
const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/pages/denia/style.css">
<main id="app"></main><script type="module">
import { createConfigView } from '/pages/denia/views/config.js';
window.view = createConfigView({ setHead() {}, api: {
  async get() { return (await fetch('/api/config')).json(); },
  async post(route, body) { return (await fetch('/api/config', { method:'POST', body:JSON.stringify(body) })).json(); }
}});
await window.view.mount(document.querySelector('#app'));
window.ready = true;
</script>`;
const server = http.createServer(async (req, res) => {
  try {
    if (req.url === "/api/config") {
      let body = "";
      for await (const chunk of req) body += chunk;
      const values = body ? JSON.parse(body).values : {};
      if (req.method === "POST") lastPayload = values;
      res.setHeader("Content-Type", "application/json; charset=utf-8");
      return res.end(JSON.stringify(configAPI(values)));
    }
    if (req.url === "/") {
      res.setHeader("Content-Type", "text/html; charset=utf-8");
      return res.end(html);
    }
    const file = path.resolve(root, "." + req.url);
    if (!file.startsWith(root + path.sep) || !file.startsWith(path.join(root, "pages") + path.sep)) {
      res.writeHead(404); return res.end();
    }
    res.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/css; charset=utf-8");
    res.end(fs.readFileSync(file));
  } catch (error) {
    res.writeHead(500); res.end(String(error.stack));
  }
});
(async () => {
  let browser;
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => window.ready);
    const group = page.getByRole("group", { name: "发送模式", exact: true });
    const box = name => group.getByRole("checkbox", { name, exact: true });
    const save = page.locator("[data-save-submit]");
    async function checkSelection(expected) {
      for (const name of ["文本", "卡片", "富媒体"]) assert.equal(await box(name).isChecked(), expected.includes(name), name);
    }
    async function saveAndReload(expected) {
      await save.click();
      await page.waitForFunction(() => document.querySelector('[data-save-submit]').disabled);
      assert.deepEqual(lastPayload.SEND_MODE, expected);
      assert.deepEqual(configAPI().values.SEND_MODE, expected);
      await page.reload();
      await page.waitForFunction(() => window.ready);
      await checkSelection(expected);
      assert.equal(await save.isDisabled(), true);
    }
    await checkSelection(["文本", "富媒体"]);
    assert.equal(await save.isDisabled(), true);
    // Choosing a card must not deselect text; reverting must clear dirty state.
    await box("卡片").check();
    await checkSelection(["文本", "卡片", "富媒体"]);
    assert.equal(await save.isEnabled(), true);
    await box("卡片").uncheck();
    assert.equal(await save.isDisabled(), true);
    await box("卡片").check();
    await box("富媒体").uncheck();
    await saveAndReload(["文本", "卡片"]);
    // All eight selections round-trip through the actual Python config boundary.
    for (let mask = 0; mask < 8; mask++) {
      const selected = ["文本", "卡片", "富媒体"].filter((_, i) => mask & (1 << i));
      for (const name of ["文本", "卡片", "富媒体"]) await box(name).setChecked(selected.includes(name));
      if (await save.isEnabled()) await saveAndReload(selected);
      else await checkSelection(selected);
    }
    const renderField = page.locator('.field').filter({ has: page.locator('code', { hasText: /^RENDER_ENABLED$/ }) });
    await renderField.locator('label.switch').click();
    await save.click();
    await page.waitForFunction(() => document.querySelector('[data-save-submit]').disabled);
    assert.equal(lastPayload.RENDER_ENABLED, false);
    assert.deepEqual(configAPI().flags, [false, true, true]);
    await page.reload();
    await page.waitForFunction(() => window.ready);
    await checkSelection(["文本", "卡片", "富媒体"]);
    assert.equal(await renderField.locator('input[type="checkbox"]').isChecked(), false);
    // Also verify the independently mounted Appearance view uses the same switch.
    await page.evaluate(async () => {
      const { createAppearanceView } = await import('/pages/denia/views/appearance.js');
      window.appearance = createAppearanceView({
        setHead() {}, prefs: {}, accentPresets: {}, savePrefs() {},
        api: {
          async get() { return (await fetch('/api/config')).json(); },
          async post(route, body) {
            if (route === 'preview/sample') return {error: 'Preview intentionally stubbed in offline UI test'};
            return (await fetch('/api/config', {method:'POST', body:JSON.stringify(body)})).json();
          }
        }
      });
      await window.appearance.mount(document.querySelector('#app'));
    });
    const appearanceField = page.locator('.field').filter({ hasText: '卡片渲染' });
    assert.equal(await appearanceField.locator('input[type="checkbox"]').isChecked(), false);
    await appearanceField.locator('label.switch').click();
    const saveAppearance = page.getByRole('button', { name: /保存 1 项修改/ });
    await saveAppearance.click();
    await page.waitForFunction(() => document.body.textContent.includes('没有未保存') || !document.querySelector('.savebar'));
    assert.equal(lastPayload.RENDER_ENABLED, true);
    assert.deepEqual(configAPI().flags, [true, true, true]);
    assert.deepEqual(errors, []);
    console.log("PASS: real Chromium checkbox independence, all 8 combinations, dirty reset, save/reload, card master switch and Appearance switch; no JS errors.");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
