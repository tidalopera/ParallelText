"use strict";
// Exercise the real reader event handlers in a minimal DOM, without a browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { hasSpeech, nextSpeakable, advanceRotation, rotationPlan, voiceSequence, playbackPlan } = require("./Reader.js");
const paragraphs = ["First.", "...", "\u2026\u2014", "Second.", "***", "Third.", "!!!"].map(english => ({ english, chinese: english, chapter: "Test" }));
assert.equal(hasSpeech("\u2026\u2026\u2014 * () \ud83d\ude42"), false);
for (const text of ["0", "No...", "caf\u00e9", "\u6d4b\u8bd5"]) assert.equal(hasSpeech(text), true);
assert.equal(nextSpeakable(paragraphs, 1), 3);
assert.equal(nextSpeakable(paragraphs, 4, -1), 3);
assert.equal(nextSpeakable(paragraphs, 6), -1);
assert.deepEqual(rotationPlan(paragraphs,0,"a",["a","b"],2,0), [{index:3,voice:"a"},{index:5,voice:"b"}]);
assert.deepEqual(rotationPlan(paragraphs,0,"a",["a","b"],2,1), [{index:3,voice:"b"},{index:5,voice:"b"}]);
assert.deepEqual(rotationPlan(paragraphs,0,"c",["a","b","c"],1,0), [{index:3,voice:"a"},{index:5,voice:"b"}]);
assert.deepEqual(advanceRotation("a",[],1,0),{voice:"a",completed:0});
assert.deepEqual(advanceRotation("a",["b"],1,0),{voice:"b",completed:0});
assert.deepEqual(advanceRotation("a",["b"],0,0),{voice:"a",completed:0});
assert.deepEqual(voiceSequence("c",["a","b","c","d"],3),["c","d","a"]);
assert.deepEqual(voiceSequence("x",["a","b"],99),["x","a","b"]);
assert.deepEqual(voiceSequence("a",[],3),["a"]);
assert.deepEqual(playbackPlan(paragraphs,0,"a",["a","b","c","d"],2,0,3,0),[
  {index:0,voice:"b"},{index:0,voice:"c"},{index:3,voice:"a"},
  {index:3,voice:"b"},{index:3,voice:"c"},{index:5,voice:"b"},
]);
assert.deepEqual(playbackPlan(paragraphs,5,"a",["a","b","c"],0,0,3,0),[{index:5,voice:"b"},{index:5,voice:"c"}]);
assert.deepEqual(playbackPlan(paragraphs,0,"a",["a","b"],2,0,1,0),rotationPlan(paragraphs,0,"a",["a","b"],2,0));
assert.deepEqual(voiceSequence("b",["f","a","b","c"],3,"f"),["f","b","c"]);
assert.deepEqual(voiceSequence("f",["f","a"],99,"f"),["f","a"]);
assert.deepEqual(voiceSequence("f",["f"],3,"f"),["f"]);
assert.deepEqual(voiceSequence("a",["a","b"],1,"f"),["f"]);
assert.deepEqual(playbackPlan(paragraphs,0,"a",["f","a","b","c"],2,0,3,0,6,"f"),[
  {index:0,voice:"a"},{index:0,voice:"b"},{index:3,voice:"f"},
  {index:3,voice:"a"},{index:3,voice:"b"},{index:5,voice:"f"},
]);
assert.deepEqual(playbackPlan(paragraphs,0,"a",["a","b"],1,0,1,0,6,"f"),[{index:3,voice:"f"},{index:5,voice:"f"}]);

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
  get("voiceEvery").value = "3";
  get("repeatEvery").value = "3";
  const doc = new Element();
  Object.assign(doc, { getElementById: get, createElement: () => new Element(), createDocumentFragment: () => new Element(), createTextNode: text => ({ textContent: text }) });
  const win = new Element();
  Object.assign(win, { innerHeight: 900, scrollY: 0, getSelection: () => null });
  let failure = false;
  let expireOnce = false;
  const requests = [];
  const requestedVoices = [], plans = [];
  const context = {
    document: doc, window: win, console, AbortController, DOMException, setTimeout, clearTimeout, queueMicrotask,
    requestAnimationFrame: () => 1,
    fetch: async (url, options = {}) => {
      if (url === "/api/book") return { ok: true, json: async () => ({ title: "Test", paragraphs, defaultVoice:"en-US-AriaNeural", voices: [
        {id:"furina-local-en",name:"Furina"},
        {id:"en-US-AriaNeural",name:"Aria",locale:"en-US",gender:"Female"},
        {id:"en-CA-ClaraNeural",name:"Clara",locale:"en-CA",gender:"Female"},
        {id:"de-DE-SeraphinaMultilingualNeural",name:"SeraphinaMultilingual",locale:"de-DE",gender:"Female"},
      ] }) };
      if (url.startsWith("/api/speech?key=")) return { ok: false, status: 404, json: async () => ({ message:"expired" }) };
      if (url === "/api/prefetch") {
        const { index, count } = JSON.parse(options.body);
        const { plan } = JSON.parse(options.body);
        if (plan) {
          plans.push(plan);
          assert.ok(plan.length <= count);
          assert.ok(plan.every(item => item.index >= index && hasSpeech(paragraphs[item.index].english)));
        }
        assert.equal(count, 6);
        assert.equal(hasSpeech(paragraphs[index].english), true);
        return { ok: true, json: async () => ({ state: "scheduled" }) };
      }
      assert.equal(url, "/api/speech");
      const { index, voice } = JSON.parse(options.body); requests.push(index); requestedVoices.push(voice);
      assert.equal(hasSpeech(paragraphs[index].english), true, "must never request punctuation-only speech");
      if (expireOnce) { expireOnce = false; return { ok:true, json:async()=>({state:"pending",key:"stale"}) }; }
      return { ok: true, json: async () => failure ? { state: "error", message: "network test" } : { state: "ready", words: [], audio: `/audio/${index}.mp3` } };
    },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("./Reader.js"), "utf8"), context);
  await flush();
  const audio = get("audio");
  assert.equal(get("voice").value, "en-US-AriaNeural", "catalogue preserves the configured default");
  assert.equal(get("voice").children.length, 4, "all voices are available initially");
  get("voiceLanguage").value = "cloud"; get("voiceLanguage").emit("change");
  assert.equal(get("voice").children.length,3,"cloud rotation excludes the local model");
  get("voiceLanguage").value = "multilingual"; get("voiceLanguage").emit("change");
  assert.equal(get("voice").children.length, 2, "nonmatching current voice remains pinned");
  assert.match(get("voice").children[1].textContent, /支持英文/);
  assert.equal(get("voice").value, "en-US-AriaNeural", "filtering never silently changes the voice");
  get("voiceSearch").value = "nonexistent"; get("voiceSearch").emit("input");
  assert.equal(get("voice").children.length, 1, "empty search retains the current voice");
  assert.match(get("voiceCount").textContent, /^0 \/ 4/);
  assert.deepEqual(requests, [], "filtering does not generate speech");
  get("voiceLanguage").value = "en"; get("voiceLanguage").emit("change");
  get("voiceSearch").value = "加拿大"; get("voiceSearch").emit("input");
  assert.equal(get("voice").children[1].value, "en-CA-ClaraNeural", "search matches localized locale names");
  get("voiceLanguage").value = "all"; get("voiceSearch").value = ""; get("voiceSearch").emit("input");
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
  get("voiceEvery").value = "1";
  get("voiceRotation").checked = true; get("voiceRotation").emit("change"); await flush();
  assert.deepEqual(requests,[3],"enabling rotation does not interrupt the current paragraph");
  assert.equal(plans.at(-1)[0].index,5,"rotation prefetch skips symbols");
  assert.equal(plans.at(-1)[0].voice,"en-CA-ClaraNeural","prefetch uses the next voice before the current paragraph ends");
  expireOnce = true;
  audio.paused = true; audio.emit("ended");
  await new Promise(resolve=>setTimeout(resolve,1100)); await flush();
  assert.equal(audio.src, "/audio/5.mp3", "autoplay crosses punctuation");
  assert.equal(audio.playbackRate, 1.37, "autoplay preserves the selected fractional speed");
  assert.equal(get("voice").value,"en-CA-ClaraNeural","one completed paragraph advances the voice exactly once");
  assert.equal(requestedVoices.at(-1),"en-CA-ClaraNeural","expired jobs retry the planned voice");
  get("voiceEvery").value = "2"; get("voiceEvery").emit("change");
  audio.pause();
  get("speed").value = "0.93"; get("speed").emit("input");
  assert.equal(audio.playbackRate, 0.93);
  assert.equal(audio.paused, true, "speed changes preserve paused playback");
  get("previous").emit("click"); await flush();
  assert.equal(audio.src, "/audio/3.mp3", "previous travels backward across punctuation");
  assert.equal(audio.playbackRate, 0.93, "manual navigation preserves the selected speed");
  assert.match(get("rotationStatus").textContent,/本轮 0\/2/,"manual navigation resets rotation count");
  get("voiceRotation").checked = false; get("voiceRotation").emit("change");
  assert.match(get("rotationStatus").textContent,/已关闭/);
  get("voice").value = "en-CA-ClaraNeural"; get("voice").emit("change"); await flush();
  assert.equal(audio.paused, true, "switching voices preserves paused playback");
  assert.equal(requests.at(-1), 3, "a newly added voice regenerates the current paragraph");
  audio.currentTime = 2;
  get("rewind").emit("click"); await flush();
  assert.equal(audio.src, "/audio/0.mp3", "rewind crosses consecutive punctuation");
  assert.equal(audio.currentTime, 0, "rewind clamps at start");
  get("forward").emit("click"); await flush();
  assert.equal(audio.src, "/audio/5.mp3", "forward seek carries seconds over symbols");
  audio.paused = true; audio.emit("ended"); await flush();
  assert.equal(audio.src, "");
  assert.equal(get("status").textContent, "\u5168\u4e66\u6717\u8bfb\u5b8c\u6210", "trailing symbols finish normally");
  // Both controls operate together: 3 voices per paragraph, base shifts after 2 paragraphs.
  get("voiceLanguage").value="cloud"; get("voiceLanguage").emit("change");
  get("voice").value="en-US-AriaNeural"; get("voice").emit("change"); await flush();
  get("paragraphRepeat").checked=true; get("repeatEvery").value="99"; get("paragraphRepeat").emit("change");
  assert.equal(get("repeatEvery").value,"3","repeat count is capped to distinct available voices");
  get("voiceRotation").checked=true; get("voiceEvery").value="2"; get("voiceRotation").emit("change");
  get("rewind").emit("click"); await flush();
  const a="en-US-AriaNeural", c="en-CA-ClaraNeural", s="de-DE-SeraphinaMultilingualNeural";
  const steps=[[0,a],[0,c],[0,s],[3,a],[3,c],[3,s],[5,c],[5,s],[5,a]];
  assert.equal(plans.at(-1)[0].index,0,"prefetch includes remaining voices of the current paragraph");
  assert.equal(plans.at(-1)[0].voice,c);
  for (let step=0;step<steps.length;step++) {
    const [paragraph,voice]=steps[step];
    assert.equal(audio.src,`/audio/${paragraph}.mp3`,"advance only after all repetitions finish");
    assert.equal(get("voice").value,voice,"fixed voice group starts at the paragraph base");
    assert.match(get("repeatStatus").textContent,new RegExp(`第 ${step%3+1}/3`));
    assert.equal(audio.playbackRate,0.93,"repeated audio keeps the selected speed");
    if (step===0) {
      audio.pause(); get("speed").emit("input");
      assert.match(get("repeatStatus").textContent,/第 1\/3/,"pause and speed do not advance repetitions");
      expireOnce=true;
    }
    audio.paused=true; audio.emit("ended");
    if (step===0) await new Promise(resolve=>setTimeout(resolve,1100));
    await flush();
    if (step<2) assert.match(get("rotationStatus").textContent,/本轮 0\/2/,"repetitions do not count as separate paragraphs");
  }
  assert.equal(audio.src,"","finish only after the final paragraph's last repetition");
  // Pin Furina to the first pass even when the filter only lists cloud voices.
  get("voice").value=a; get("voice").emit("change"); await flush();
  get("furinaFirst").checked=true; get("furinaFirst").emit("change"); await flush();
  assert.equal(get("paragraphRepeat").checked,true,"pinning Furina enables paragraph repetition");
  get("rewind").emit("click"); await flush();
  const f="furina-local-en";
  const pinnedSteps=[[0,f],[0,a],[0,c],[3,f],[3,a],[3,c],[5,f],[5,c],[5,s]];
  assert.equal(plans.at(-1)[0].voice,a,"prefetch schedules the first rotating voice after Furina");
  assert.equal(plans.at(-1)[2].voice,f,"next paragraph is also prefetched with Furina first");
  for(let step=0;step<pinnedSteps.length;step++) {
    const [paragraph,voice]=pinnedSteps[step];
    assert.equal(audio.src,`/audio/${paragraph}.mp3`);
    assert.equal(get("voice").value,voice,"Furina stays fixed while only the other voices rotate");
    assert.match(get("repeatStatus").textContent,/芙宁娜首遍/);
    if(step===1) {
      audio.pause();
      get("furinaFirst").checked=false; get("furinaFirst").emit("change"); await flush();
      get("furinaFirst").checked=true; get("furinaFirst").emit("change"); await flush();
      assert.equal(audio.paused,true,"enabling the mode preserves paused playback");
      assert.equal(get("voice").value,f,"enabling midway restarts this paragraph with Furina");
      assert.match(get("repeatStatus").textContent,/第 1\/3/);
      audio.paused=true; audio.emit("ended"); await flush();
      assert.equal(get("voice").value,a,"the rotating group resumes after the pinned first pass");
    }
    audio.paused=true; audio.emit("ended"); await flush();
  }
  assert.equal(audio.src,"");
  get("repeatEvery").value="1"; get("repeatEvery").emit("change"); await flush();
  get("rewind").emit("click"); await flush();
  for(const paragraph of [0,3,5]) {
    assert.equal(audio.src,`/audio/${paragraph}.mp3`);
    assert.equal(get("voice").value,f,"one total pass means Furina only");
    audio.paused=true; audio.emit("ended"); await flush();
  }
  get("furinaFirst").checked=false; get("furinaFirst").emit("change");
  get("paragraphRepeat").checked=false; get("paragraphRepeat").emit("change");
  get("voiceRotation").checked=false; get("voiceRotation").emit("change");
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
  console.log("Reader checks passed: Furina-first mode and paused activation, combined repetitions and rotation, planned prefetch, filters, speed, symbols, seek, retry, errors");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
