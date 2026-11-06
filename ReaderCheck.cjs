"use strict";
// Exercise the real reader event handlers in a minimal DOM, without a browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { hasSpeech, nextSpeakable } = require("./Reader.js");
const paragraphs = ["First.", "...", "\u2026\u2014", "Second.", "***", "Third.", "!!!"].map(english => ({ english, chinese: english, chapter: "Test" }));
assert.equal(hasSpeech("\u2026\u2026\u2014 * () \ud83d\ude42"), false);
for (const text of ["0", "No...", "caf\u00e9", "\u6d4b\u8bd5"]) assert.equal(hasSpeech(text), true);
assert.equal(nextSpeakable(paragraphs, 1), 3);
assert.equal(nextSpeakable(paragraphs, 4, -1), 3);
assert.equal(nextSpeakable(paragraphs, 6), -1);

class Element {
  constructor() {
    this.listeners = {}; this.children = []; this.dataset = {}; this.style = {};
    this.classList = { toggle() {}, contains() { return false; }, add() {}, remove() {} };
    this.value = ""; this.checked = false; this.textContent = "";
  }
  addEventListener(type, fn, options) { (this.listeners[type] ||= []).push({ fn, once: options?.once }); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(x => x.fn !== fn); }
  emit(type, event = {}) {
    for (const entry of [...(this.listeners[type] || [])]) {
      if (entry.once) this.removeEventListener(type, entry.fn);
      entry.fn(event);
    }
  }
  setAttribute() {}
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(fragment) { this.children = fragment.children; }
  querySelector() { return null; }
  contains() { return false; }
}
class Audio extends Element {
  constructor() { super(); this.paused = true; this.src = ""; this.duration = 5; this.currentTime = 0; this.playCount = 0; }
  pause() { if (!this.paused) { this.paused = true; this.emit("pause"); } }
  removeAttribute(name) { if (name === "src") this.src = ""; }
  load() { this.currentTime = 0; if (this.src) queueMicrotask(() => this.emit("loadedmetadata")); }
  async play() { this.paused = false; ++this.playCount; this.emit("play"); }
}
const flush = async () => { for (let i = 0; i < 6; ++i) await new Promise(setImmediate); };

async function run() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, id === "audio" ? new Audio() : new Element());
    return elements.get(id);
  };
  get("voice").value = "en-US-AriaNeural";
  get("speed").value = "1";
  const doc = new Element();
  Object.assign(doc, { getElementById: get, createElement: () => new Element(), createDocumentFragment: () => new Element(), createTextNode: text => ({ textContent: text }) });
  const win = new Element();
  Object.assign(win, { innerHeight: 900, scrollY: 0, getSelection: () => null });
  let failure = false;
  let expireOnce = false;
  const requests = [];
  const context = {
    document: doc, window: win, console, AbortController, DOMException, setTimeout, clearTimeout, queueMicrotask,
    requestAnimationFrame: () => 1,
    fetch: async (url, options = {}) => {
      if (url === "/api/book") return { ok: true, json: async () => ({ title: "Test", paragraphs, voices: [] }) };
      if (url.startsWith("/api/speech?key=")) return { ok: false, status: 404, json: async () => ({ message:"expired" }) };
      if (url === "/api/prefetch") {
        const { index, count } = JSON.parse(options.body);
        assert.equal(count, 6);
        assert.equal(hasSpeech(paragraphs[index].english), true);
        return { ok: true, json: async () => ({ state: "scheduled" }) };
      }
      assert.equal(url, "/api/speech");
      const { index } = JSON.parse(options.body); requests.push(index);
      assert.equal(hasSpeech(paragraphs[index].english), true, "must never request punctuation-only speech");
      if (expireOnce) { expireOnce = false; return { ok:true, json:async()=>({state:"pending",key:"stale"}) }; }
      return { ok: true, json: async () => failure ? { state: "error", message: "network test" } : { state: "ready", words: [], audio: `/audio/${index}.mp3` } };
    },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("./Reader.js"), "utf8"), context);
  await flush();
  const audio = get("audio");
  get("play").emit("click"); await flush();
  assert.equal(audio.src, "/audio/3.mp3", "start at consecutive punctuation then continue");
  assert.equal(audio.paused, false);
  assert.deepEqual(requests, [3], "foreground requests only the current paragraph");
  audio.currentTime = 2;
  const playCount = audio.playCount;
  for (const speed of ["0.5", "0.93", "1.37", "2"]) {
    get("speed").value = speed; get("speed").emit("input");
    assert.equal(audio.playbackRate, Number(speed), "slider applies immediately while dragging");
    assert.equal(get("speedValue").textContent, `${Number(speed).toFixed(2)}×`);
  }
  assert.equal(audio.currentTime, 2, "speed changes preserve playback position");
  assert.equal(audio.paused, false, "speed changes preserve active playback");
  assert.equal(audio.playCount, playCount, "speed changes do not restart playback");
  assert.deepEqual(requests, [3], "speed changes reuse existing audio");
  get("speed").value = "1.37"; get("speed").emit("input");
  expireOnce = true;
  audio.paused = true; audio.emit("ended");
  await new Promise(resolve=>setTimeout(resolve,1100)); await flush();
  assert.equal(audio.src, "/audio/5.mp3", "autoplay crosses punctuation");
  assert.equal(audio.playbackRate, 1.37, "autoplay preserves the selected fractional speed");
  audio.pause();
  get("speed").value = "0.93"; get("speed").emit("input");
  assert.equal(audio.playbackRate, 0.93);
  assert.equal(audio.paused, true, "speed changes preserve paused playback");
  get("previous").emit("click"); await flush();
  assert.equal(audio.src, "/audio/3.mp3", "previous travels backward across punctuation");
  assert.equal(audio.playbackRate, 0.93, "manual navigation preserves the selected speed");
  audio.currentTime = 2;
  get("rewind").emit("click"); await flush();
  assert.equal(audio.src, "/audio/0.mp3", "rewind crosses consecutive punctuation");
  assert.equal(audio.currentTime, 0, "rewind clamps at start");
  get("forward").emit("click"); await flush();
  assert.equal(audio.src, "/audio/5.mp3", "forward seek carries seconds over symbols");
  audio.paused = true; audio.emit("ended"); await flush();
  assert.equal(audio.src, "");
  assert.equal(get("status").textContent, "\u5168\u4e66\u6717\u8bfb\u5b8c\u6210", "trailing symbols finish normally");
  failure = true;
  get("previous").emit("click"); await flush();
  assert.match(get("status").textContent, /network test/, "real service errors must not be skipped");
  assert.equal(audio.src, "");
  // A book containing no spoken content must finish without a request or loop.
  for (const paragraph of paragraphs) paragraph.english = "...";
  const count = requests.length;
  get("play").emit("click"); await flush();
  assert.equal(requests.length, count);
  assert.equal(audio.src, "");
  console.log("Reader checks passed: continuous speed, symbol-only start/autoplay/previous/seek/end, prefetch, real errors, all-symbol book");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
