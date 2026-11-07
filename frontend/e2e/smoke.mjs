// Browser smoke test of the whole app (ARCHITECTURE.md §12), driving a local Chrome with puppeteer-core.
//
//   npm run e2e -- <baseUrl> [dwgFile]           e.g. npm run e2e -- http://localhost:5173 ../examples/call-center-offices.dwg
//
// Env: CHROME_PATH (default: macOS Google Chrome), E2E_SHOTS=<dir> to save screenshots,
//      E2E_USER / E2E_PASSWORD to log in instead of registering a throwaway e2e_* account.
import puppeteer from "puppeteer-core";

const [base, dwgFile] = process.argv.slice(2);
if (!base) {
  console.error("usage: npm run e2e -- <baseUrl> [dwgFile]");
  process.exit(2);
}
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SHOTS = process.env.E2E_SHOTS;
const log = (...a) => console.log("•", ...a);
const problems = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1400,900"],
  defaultViewport: { width: 1400, height: 900 },
});
const page = await browser.newPage();
page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("response", (r) => {
  if (r.status() >= 500 || r.status() === 404) problems.push(`HTTP ${r.status()} ${r.url()}`);
});
const shot = (name) => (SHOTS ? page.screenshot({ path: `${SHOTS}/${name}.png` }) : Promise.resolve());
const text = () => page.evaluate(() => document.body.innerText);
const waitText = (fragment, timeout = 20000) =>
  page.waitForFunction((f) => document.body.innerText.includes(f), { timeout }, fragment);
async function clickButton(label) {
  const handle = await page.waitForSelector(`::-p-xpath(//button[normalize-space(.)="${label}"] | //a[normalize-space(.)="${label}"])`);
  await handle.click();
}
const waitForScene = () => page.waitForFunction(() => !!window.__viewer?.scene?.meshes?.length, { timeout: 60000 });

/** Screen position of the first window (sill or glass) in the current camera. */
const windowOnScreen = () =>
  page.evaluate(() => {
    const scene = window.__viewer.scene;
    const glass = scene.meshes.find((m) => m.metadata?.kind === "glass");
    const sill = scene.meshes.find((m) => m.metadata?.kind === "sill" && m.metadata.openingId === glass.metadata.openingId);
    const c = (sill ?? glass).getBoundingInfo().boundingBox.centerWorld;
    const engine = scene.getEngine();
    const vp = scene.activeCamera.viewport.toGlobal(engine.getRenderWidth(), engine.getRenderHeight());
    const transform = scene.getTransformMatrix();
    const p = c.constructor.Project(c, transform.constructor.Identity(), transform, vp);
    return { x: p.x, y: p.y, opening: glass.metadata.openingId };
  });

try {
  // Enable the viewer test hook before any app code runs.
  await page.goto(`${base}/login`, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => localStorage.setItem("walkitecture.e2e", "1"));

  // 1. Unauthenticated -> login page
  await page.goto(`${base}/plans`, { waitUntil: "networkidle0" });
  if (!page.url().endsWith("/login")) throw new Error(`expected redirect to /login, got ${page.url()}`);
  await shot("01-login");
  log("redirected to login");

  // 2. Register (or log in)
  const username = process.env.E2E_USER ?? `e2e_${Date.now()}`;
  const password = process.env.E2E_PASSWORD ?? "e2e-password-123";
  if (!process.env.E2E_USER) {
    await clickButton("Create one");
    await page.waitForFunction(() => location.pathname === "/register");
  }
  await page.type('input[autocomplete="username"]', username);
  await page.type('input[type="password"]', password);
  await page.click("button.primary");
  await page.waitForFunction(() => location.pathname === "/plans", { timeout: 15000 });
  await waitText("Your plans");
  await shot("02-plans");
  log(`${process.env.E2E_USER ? "logged in" : "registered"} as ${username}`);

  // 3. Sample plan -> viewer
  await clickButton("Add sample apartment");
  await page.waitForFunction(() => /^\/plans\/[0-9a-f-]+$/.test(location.pathname), { timeout: 30000 });
  await waitText("Ask for a change");
  await waitForScene();
  await sleep(1500);
  await shot("03-plan-first-person");
  log("sample plan opened:", page.url());

  // 4. Top-down, click a window to select it
  await page.keyboard.press("t");
  await sleep(1000);
  const target = await windowOnScreen();
  await page.mouse.click(target.x, target.y);
  await waitText("Window ·", 5000);
  const before = (await text()).match(/Window · ([\d.]+) m/)[1];
  await shot("04-window-selected");
  log(`selected ${target.opening} (${before} m) by clicking it in 3D`);

  // 5. +50% via the edit panel, 6. undo
  await clickButton("+50%");
  await page.waitForFunction(
    (b) => {
      const m = document.body.innerText.match(/Window · ([\d.]+) m/);
      return m && m[1] !== b;
    },
    { timeout: 15000 },
    before,
  );
  const after = (await text()).match(/Window · ([\d.]+) m/)[1];
  await shot("05-window-wider");
  log(`slider edit: ${before} m -> ${after} m`);
  await clickButton("Undo");
  await page.waitForFunction((b) => document.body.innerText.includes(`Window · ${b} m`), { timeout: 15000 }, before);
  log(`undo restored ${before} m`);

  // 7. Chat edit
  await page.type(".chat-form input", "Raise the ceiling to 3.5 meters");
  await page.click(".chat-form button");
  await page.waitForFunction(() => document.body.innerText.includes("3.50 m"), { timeout: 30000 });
  await shot("06-after-chat");
  const msgs = await page.$$eval(".msg", (els) => els.map((e) => `[${e.className.replace("msg ", "")}] ${e.textContent}`));
  log("activity:", JSON.stringify(msgs));
  log(`AI buttons ${(await text()).includes("AI tools need an OpenAI key") ? "disabled (no OpenAI key)" : "enabled"}`);

  // 8. Optional: upload a real DWG and walk it
  if (dwgFile) {
    await page.click(".hud-links a");
    await page.waitForSelector(".plan-name", { timeout: 15000 });
    const input = await page.$('input[type="file"]');
    await input.uploadFile(dwgFile);
    log(`uploading ${dwgFile}…`);
    await page.waitForFunction(() => /^\/plans\/[0-9a-f-]+$/.test(location.pathname) || document.querySelector(".error"), {
      timeout: 180000,
    });
    const err = await page.$eval(".error", (e) => e.textContent).catch(() => null);
    if (err) throw new Error(`upload failed: ${err}`);
    await waitForScene();
    await sleep(4000);
    await shot("07-uploaded-first-person");
    await page.keyboard.press("t");
    await sleep(1500);
    await shot("08-uploaded-top-down");
    const furniture = await page.evaluate(() => window.__viewer.scene.transformNodes.filter((n) => n.metadata?.kind === "furniture").length);
    log(`uploaded plan walkable: ${(await text()).match(/\d+ walls · \d+ rooms · \d+ furniture/)?.[0]}, ${furniture} GLB models placed`);
  }

  // 9. Plan list, log out
  await page.click(".hud-links a");
  await page.waitForSelector(".plan-name", { timeout: 15000 });
  log("plan list:", (await page.$$eval(".plan-name", (els) => els.map((e) => e.textContent))).join(", "));
  await clickButton("Log out");
  await page.waitForFunction(() => location.pathname === "/login", { timeout: 10000 });
  log("logged out");

  // 10. Public sample viewer
  await page.goto(`${base}/viewer?fixture=call-center-offices&view=top`, { waitUntil: "networkidle0" });
  await waitForScene();
  await sleep(2500);
  await shot("09-public-sample");
  log("public sample viewer ok");
} catch (error) {
  problems.push(`FAILED: ${error.message}`);
  await shot("zz-failure").catch(() => {});
} finally {
  await browser.close();
}
console.log(problems.length ? `PROBLEMS:\n${problems.join("\n")}` : "ALL STEPS PASSED, no console errors or failed requests");
process.exit(problems.length ? 1 : 0);
