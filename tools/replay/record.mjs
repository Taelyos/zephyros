#!/usr/bin/env node
// Records the replay player into a video, with headless Chromium and ffmpeg.
//
//   node tools/replay/record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif --gif-width 960]
//
// PAGE.html is a player with its replay embedded (python -m tools.replay.export ... --html).
// SCRIPT.json is a list of steps run in order against `window.replay`:
//   {"caption": "text" | null}          show or hide the caption
//   {"seek": EVENT_ID}                  jump to an event
//   {"focus": EVENT_ID}                 highlight an event
//   {"speed": N}                        set the playback speed
//   {"play": true} / {"pause": true}
//   {"wait": MS}                        let the page run for MS milliseconds
//   {"until": EVENT_ID, "timeout": MS}  run until the player has reached EVENT_ID
// Frames come from the DevTools screencast with their timestamps, so the video keeps the
// page's real pace. Needs Node 22 (global WebSocket), chromium and ffmpeg on the PATH.

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [page, scriptPath, output, ...rest] = process.argv.slice(2);
if (!page || !scriptPath || !output) {
  console.error("usage: record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif --gif-width 960]");
  process.exit(2);
}
const option = (name, fallback) => {
  const i = rest.indexOf(name);
  return i >= 0 ? rest[i + 1] : fallback;
};
const gif = option("--gif", null);
const gifWidth = Number(option("--gif-width", "960"));
const width = Number(option("--width", "1280"));
const height = Number(option("--height", "720"));
const steps = JSON.parse(readFileSync(scriptPath, "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const profile = mkdtempSync(join(tmpdir(), "zephyros-record-"));
const frames = join(profile, "frames");
spawnSync("mkdir", ["-p", frames]);
const browser = spawn(
  process.env.CHROMIUM || "chromium",
  [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--hide-scrollbars",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

async function devtoolsPort() {
  const file = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100; i++) {
    if (existsSync(file)) return readFileSync(file, "utf8").split("\n")[0];
    await sleep(100);
  }
  throw new Error("chromium did not start");
}

const port = await devtoolsPort();
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));

let nextId = 1;
const pending = new Map();
const shots = [];
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve: ok, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else ok(message.result);
  } else if (message.method === "Page.screencastFrame") {
    const { data, metadata, sessionId } = message.params;
    const name = join(frames, `f${String(shots.length).padStart(6, "0")}.jpg`);
    writeFileSync(name, Buffer.from(data, "base64"));
    shots.push({ name, t: metadata.timestamp });
    send("Page.screencastFrameAck", { sessionId });
  }
});
function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((ok, reject) => pending.set(id, { resolve: ok, reject }));
}
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(`page error: ${JSON.stringify(result.exceptionDetails)}`);
  return result.result.value;
}

await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: pathToFileURL(resolve(page)).href });
for (let i = 0; i < 100 && !(await evaluate("typeof window.replay === 'object'")); i++) await sleep(100);
await send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: width, maxHeight: height });

for (const step of steps) {
  if ("caption" in step) await evaluate(`window.replay.caption(${JSON.stringify(step.caption)})`);
  if ("speed" in step) await evaluate(`window.replay.setSpeed(${Number(step.speed)})`);
  if ("seek" in step) await evaluate(`window.replay.seek(${Number(step.seek)})`);
  if ("focus" in step) await evaluate(`window.replay.focus(${Number(step.focus)})`);
  if (step.play) await evaluate("window.replay.play()");
  if (step.pause) await evaluate("window.replay.pause()");
  if ("wait" in step) await sleep(Number(step.wait));
  if ("until" in step) {
    const limit = Date.now() + Number(step.timeout ?? 60000);
    while (Date.now() < limit) {
      const state = await evaluate("window.replay.state()");
      if (state.done || state.eventId >= Number(step.until)) break;
      await sleep(50);
    }
  }
}
await sleep(300);
await send("Page.stopScreencast");
ws.close();
browser.kill();

if (shots.length === 0) throw new Error("no frame was captured");
// One concat entry per frame, lasting until the next one.
const lines = [];
for (let i = 0; i < shots.length; i++) {
  const next = i + 1 < shots.length ? shots[i + 1].t : shots[i].t + 0.5;
  lines.push(`file '${shots[i].name}'`, `duration ${Math.max(next - shots[i].t, 0.001).toFixed(4)}`);
}
lines.push(`file '${shots.at(-1).name}'`);
const list = join(profile, "frames.txt");
writeFileSync(list, lines.join("\n") + "\n");
const run = (args) => {
  const r = spawnSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", ...args], { stdio: "inherit" });
  if (r.status !== 0) throw new Error(`ffmpeg failed: ${args.join(" ")}`);
};
run(["-f", "concat", "-safe", "0", "-i", list, "-vf", `fps=30,scale=${width}:${height}:flags=lanczos,format=yuv420p`,
  "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", output]);
if (gif) {
  const palette = join(profile, "palette.png");
  const filters = `fps=12,scale=${gifWidth}:-1:flags=lanczos`;
  run(["-i", output, "-vf", `${filters},palettegen=stats_mode=diff`, palette]);
  run(["-i", output, "-i", palette, "-lavfi", `${filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`, gif]);
}
rmSync(profile, { recursive: true, force: true });
console.log(`recorded ${shots.length} frames -> ${output}${gif ? ` and ${gif}` : ""}`);
