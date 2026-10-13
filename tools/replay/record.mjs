#!/usr/bin/env node
// Records the replay player into a video, with headless Chromium and ffmpeg.
//
//   node tools/replay/record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif --gif-width 960]
//
// Extra options: --width N --height N (default 1280x720); --gif-fps N --gif-colors N
// --gif-speed N (GIF only: frames per second, palette size, time multiplier; 2 plays twice as
// fast, 0.5 half as fast).
//
// PAGE.html is a player with its replay embedded (python -m tools.replay.export ... --html).
// SCRIPT.json is a list of steps run in order against `window.replay`:
//   {"caption": "text" | null}          show or hide the caption
//   {"seek": EVENT_ID}                  jump to an event
//   {"focus": EVENT_ID}                 highlight an event
//   {"speed": N}                        set the playback speed
//   {"play": true} / {"pause": true}
//   {"wait": MS}                        let the page run for MS milliseconds
//   {"until": EVENT_ID, "timeout": MS}  run until the player has reached EVENT_ID (60 s by
//                                       default); not reaching it in time stops the recording
// Frames come from the DevTools screencast with their timestamps, so the video keeps the
// page's real pace. An error of the page (an exception, or an event the player could not show)
// stops the recording: the video would not show the run. Needs Node 22 (global WebSocket),
// chromium and ffmpeg on the PATH; CHROMIUM names another browser.

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const USAGE = "usage: record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif --gif-width 960]";
const NUMBERS = ["--width", "--height", "--gif-width", "--gif-fps", "--gif-colors", "--gif-speed"];

const [page, scriptPath, output, ...rest] = process.argv.slice(2);
if (!page || !scriptPath || !output) {
  console.error(USAGE);
  process.exit(2);
}
const options = new Map();
for (let i = 0; i < rest.length; i += 2) {
  const [name, value] = [rest[i], rest[i + 1]];
  if (name !== "--gif" && !NUMBERS.includes(name)) fail(`unknown option ${name}\n${USAGE}`);
  if (value === undefined) fail(`${name} needs a value`);
  if (NUMBERS.includes(name) && !(Number(value) > 0)) fail(`${name} must be a positive number`);
  options.set(name, value);
}
const number = (name, fallback) => Number(options.get(name) ?? fallback);
const gif = options.get("--gif") ?? null;
const gifWidth = number("--gif-width", 960);
const gifFps = number("--gif-fps", 12);
const gifColors = number("--gif-colors", 256);
const gifSpeed = number("--gif-speed", 1);
const width = number("--width", 1280);
const height = number("--height", 720);
const steps = JSON.parse(readFileSync(scriptPath, "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(message) {
  console.error(`record.mjs: ${message}`);
  process.exit(2);
}

const profile = mkdtempSync(join(tmpdir(), "zephyros-record-"));
let browser = null;
let ws = null;
try {
  const frames = await record(profile);
  await stopBrowser();
  encode(profile, frames);
  console.log(`recorded ${frames.length} frames -> ${output}${gif ? ` and ${gif}` : ""}`);
} catch (error) {
  console.error(`record.mjs: ${error.message}`);
  process.exitCode = 1;
} finally {
  await stopBrowser();
  rmSync(profile, { recursive: true, force: true });
}

// The browser writes to its profile until it exits: wait for it before the profile goes.
async function stopBrowser() {
  if (ws) ws.close();
  if (!browser || browser.exitCode !== null || browser.signalCode !== null) return;
  const exited = new Promise((r) => browser.once("exit", r));
  browser.kill();
  if (await Promise.race([exited.then(() => true), sleep(5000).then(() => false)])) return;
  browser.kill("SIGKILL");
  await exited;
}

// The browser, the page and the steps: the frames, each with the time it was painted.
async function record(profile) {
  const framesDir = join(profile, "frames");
  mkdirSync(framesDir, { recursive: true });
  browser = spawn(
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
  const started = new Promise((_, reject) => browser.once("error", reject));
  started.catch(() => {}); // awaited below while the browser starts; ignored afterwards

  const portFile = join(profile, "DevToolsActivePort");
  let port = null;
  for (let i = 0; i < 100 && port === null; i++) {
    if (existsSync(portFile)) port = readFileSync(portFile, "utf8").split("\n")[0];
    else await Promise.race([sleep(100), started]);
  }
  if (port === null) throw new Error("chromium did not start");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r, { once: true }));

  let nextId = 1;
  const pending = new Map();
  const shots = [];
  const pageErrors = [];
  let lastWall = 0;
  const send = (method, params = {}) => {
    const id = nextId++;
    ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, reject) => pending.set(id, { ok, reject }));
  };
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { ok, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else ok(message.result);
    } else if (message.method === "Page.screencastFrame") {
      const { data, metadata, sessionId } = message.params;
      const name = join(framesDir, `f${String(shots.length).padStart(6, "0")}.jpg`);
      writeFileSync(name, Buffer.from(data, "base64"));
      shots.push({ name, t: metadata.timestamp });
      lastWall = Date.now();
      send("Page.screencastFrameAck", { sessionId });
    } else if (message.method === "Runtime.exceptionThrown") {
      const details = message.params.exceptionDetails;
      pageErrors.push(details.exception?.description ?? details.text);
    } else if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
      pageErrors.push(message.params.args.map((arg) => arg.value ?? arg.description).join(" "));
    }
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(`page error: ${JSON.stringify(result.exceptionDetails)}`);
    return result.result.value;
  };
  const checkPage = () => {
    if (pageErrors.length) throw new Error(`the page failed: ${pageErrors.join("; ")}`);
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: pathToFileURL(resolve(page)).href });
  for (let i = 0; i < 100 && !(await evaluate("typeof window.replay === 'object'")); i++) await sleep(100);
  checkPage();
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
      for (;;) {
        const state = await evaluate("window.replay.state()");
        if (state.done || state.eventId >= Number(step.until)) break;
        checkPage();
        if (Date.now() >= limit) {
          throw new Error(`the player did not reach event ${step.until} in time (it is at ${state.eventId})`);
        }
        await sleep(50);
      }
    }
    checkPage();
  }
  await sleep(300);
  // A static page emits no screencast frame, so the trailing hold (the last `wait`) would be
  // lost. Capture one last frame now and date it from the wall clock, so the concat keeps it.
  try {
    const shot = await send("Page.captureScreenshot", { format: "jpeg", quality: 92 });
    const name = join(framesDir, `f${String(shots.length).padStart(6, "0")}.jpg`);
    writeFileSync(name, Buffer.from(shot.data, "base64"));
    const t = shots.length && lastWall ? shots.at(-1).t + Math.max((Date.now() - lastWall) / 1000, 0) : 0;
    shots.push({ name, t });
  } catch { /* keep whatever frames we have */ }
  await send("Page.stopScreencast");
  checkPage();
  if (shots.length === 0) throw new Error("no frame was captured");
  return shots;
}

// The frames as a video, at their own pace, and the GIF when one is asked for.
function encode(profile, shots) {
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
    const filters = `${gifSpeed !== 1 ? `setpts=PTS/${gifSpeed},` : ""}fps=${gifFps},scale=${gifWidth}:-1:flags=lanczos`;
    run(["-i", output, "-vf", `${filters},palettegen=max_colors=${gifColors}:stats_mode=diff`, palette]);
    run(["-i", output, "-i", palette, "-lavfi", `${filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`, gif]);
  }
}
