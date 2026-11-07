(function () {
  "use strict";
  const normalize = text => text.normalize("NFKC").replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[—–]/g, "-").toLowerCase().replace(/\s+/g, " ").trim();
  const visibleRange = (index, total) => [Math.max(0, index - 5), Math.min(total, index + 6)];
  const hasSpeech = text => /[\p{L}\p{N}]/u.test(text);
  const nextSpeakable = (paragraphs, start, step = 1) => {
    for (let i = start; i >= 0 && i < paragraphs.length; i += step) {
      if (hasSpeech(paragraphs[i].english)) return i;
    }
    return -1;
  };
  const advanceRotation = (voice, order, every, completed) => {
    if (!every) return { voice, completed: 0 };
    if (++completed >= every) return { voice: order.length ? order[(order.indexOf(voice) + 1) % order.length] : voice, completed: 0 };
    return { voice, completed };
  };
  const rotationPlan = (paragraphs, index, voice, order, every, completed, count = 6) => {
    const plan = [];
    for (let i = index + 1; i < paragraphs.length && plan.length < count; ++i) {
      if (!hasSpeech(paragraphs[i].english)) continue;
      ({ voice, completed } = advanceRotation(voice, order, every, completed));
      plan.push({ index: i, voice });
    }
    return plan;
  };
  const voiceSequence = (first, order, count, fixed = null) => {
    if (fixed) {
      const others = [...new Set([...(first !== fixed && !order.includes(first) ? [first] : []), ...order.filter(v => v !== fixed)])];
      if (count <= 1 || !others.length) return [fixed];
      return [fixed, ...voiceSequence(first === fixed ? others[0] : first, others, count-1)];
    }
    const pool = [...new Set([...(order.includes(first) ? [] : [first]), ...order])];
    const start = pool.indexOf(first);
    return Array.from({ length: Math.max(1, Math.min(count, pool.length)) }, (_, i) => pool[(start+i)%pool.length]);
  };
  const playbackPlan = (paragraphs, index, first, order, every, completed, repeats, pass, count = 6, fixed = null, paragraphsAhead = null) => {
    if (fixed) {
      order = order.filter(v => v !== fixed);
      if (first === fixed) first = order[0] || fixed;
    }
    const plan = [];
    let ahead = 0;
    while (plan.length < count) {
      const sequence = voiceSequence(first, order, repeats, fixed);
      if (++pass >= sequence.length) {
        if (paragraphsAhead !== null && ahead >= paragraphsAhead) break;
        index = nextSpeakable(paragraphs, index+1);
        if (index < 0) break;
        ++ahead;
        ({ voice:first, completed } = advanceRotation(first, order, every, completed));
        pass = 0;
      }
      plan.push({ index, voice: voiceSequence(first, order, repeats, fixed)[pass] });
    }
    return plan;
  };
  const playerShouldShow = (y, height, inside, focused, visible) => focused || (y >= height - 12 && y <= height) || (visible && inside);
  const centerPosition = (center, height) => ({ padding: Math.max(0, height / 2 - center), scroll: Math.max(0, center - height / 2) });
  const formatTime = seconds => {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const n = Math.floor(seconds);
    return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
  };
  const activeWord = (words, time) => {
    let low = 0, high = words.length - 1, result = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (words[middle].start <= time) { result = middle; low = middle + 1; }
      else high = middle - 1;
    }
    return result >= 0 && time <= words[result].end + 0.06 ? result : -1;
  };
  const findMatches = (indexed, query) => {
    const needle = normalize(query);
    if (!needle) return [];
    return indexed.reduce((matches, text, i) => { if (text.includes(needle)) matches.push(i); return matches; }, []);
  };
  // Pure functions can be checked in Node without controlling a user's browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { normalize, visibleRange, hasSpeech, nextSpeakable, formatTime, activeWord, findMatches, playerShouldShow, centerPosition, advanceRotation, rotationPlan, voiceSequence, playbackPlan };
    return;
  }

  const $ = id => document.getElementById(id);
  const audio = $("audio");
  let book = [], indexed = [], index = 2, words = [], wordElements = [];
  let highlighted = -1, revision = 0, controller = null, loading = false, desiredPlay = false;
  let frame = null, loadedVoice = null, stopped = false;
  let voices = [];
  let rotationCompleted = 0;
  let paragraphBaseVoice = "", repeatPass = 0;

  const languageNames = new Intl.DisplayNames(["zh-CN"], { type: "language" });
  function voiceLocale(voice) { return voice.locale || (voice.id === "furina-local-en" ? "local" : voice.id.split("-").slice(0, 2).join("-")); }
  function voiceGroup(voice) {
    const locale = voiceLocale(voice);
    return locale === "local" ? "local" : locale.startsWith("en-") ? "en" : "multilingual";
  }
  function voiceLabel(voice) {
    const locale = voiceLocale(voice);
    const language = locale === "local" ? "本机" : languageNames.of(locale);
    const gender = { Female:"女声", Male:"男声" }[voice.gender];
    return [voice.name, language, voiceGroup(voice) === "multilingual" ? "多语种 · 支持英文" : "", gender].filter(Boolean).join(" · ");
  }
  function matchingVoices() {
    const language = $("voiceLanguage").value || "all";
    const query = normalize($("voiceSearch").value);
    return voices.filter(voice => (language === "all" || (language === "cloud" && voiceGroup(voice) !== "local") || voiceGroup(voice) === language)
      && normalize(`${voiceLabel(voice)} ${voice.id}`).includes(query));
  }
  function rotationEvery() {
    const n = Number($("voiceEvery").value);
    return $("voiceRotation").checked && Number.isInteger(n) && n >= 1 && n <= 1000 ? n : 0;
  }
  function repeatEvery() {
    const n = Number($("repeatEvery").value);
    return $("paragraphRepeat").checked && Number.isInteger(n) && n >= 1 ? n : 1;
  }
  function pinnedVoice() { return $("furinaFirst").checked ? "furina-local-en" : null; }
  function rotationOrder() { return matchingVoices().map(v => v.id).filter(id => id !== pinnedVoice()); }
  function currentSequence() { return voiceSequence(paragraphBaseVoice || $("voice").value, rotationOrder(), repeatEvery(), pinnedVoice()); }
  function updateRepeats() {
    const sequence = currentSequence();
    const first = voices.find(v => v.id === paragraphBaseVoice);
    $("repeatStatus").textContent = $("paragraphRepeat").checked || pinnedVoice() ? `本段第 ${repeatPass+1}/${sequence.length} 遍 · ${pinnedVoice() ? "芙宁娜首遍 · 其他起始" : "起始"} ${first?.name || "当前声音"}` : "逐段复读已关闭";
  }
  function updateRotation() {
    const every = rotationEvery();
    const count = rotationOrder().length;
    $("rotationStatus").textContent = every ? `本轮 ${rotationCompleted}/${every} 段 · ${count ? `按筛选后的 ${count} 个声音循环` : "无匹配音色，保持当前声音"}` : "自动换声已关闭";
    updateRepeats();
  }
  function resetRotation(first = $("voice").value) {
    if (pinnedVoice() && first === pinnedVoice()) first = rotationOrder()[0] || first;
    rotationCompleted = 0; repeatPass = 0; paragraphBaseVoice = first;
    const maximum = voiceSequence(first, rotationOrder(), Math.max(1, voices.length), pinnedVoice()).length;
    $("repeatEvery").max = String(maximum);
    $("repeatEvery").value = String(Math.max(1,Math.min(maximum,Math.trunc(Number($("repeatEvery").value) || 1))));
    updateRotation();
  }
  function filterVoices(preferred) {
    const current = typeof preferred === "string" ? preferred : $("voice").value;
    const matches = matchingVoices();
    const fragment = document.createDocumentFragment();
    const addOption = (voice, prefix = "") => {
      const option = document.createElement("option"); option.value = voice.id;
      option.textContent = prefix + voiceLabel(voice); fragment.append(option);
    };
    const selected = voices.find(voice => voice.id === current);
    if (selected && !matches.includes(selected)) addOption(selected, "当前 · ");
    matches.forEach(voice => addOption(voice));
    $("voice").replaceChildren(fragment);
    if (selected) $("voice").value = current;
    else if (matches.length) $("voice").value = matches[0].id;
    $("voiceCount").textContent = `${matches.length} / ${voices.length} 个声音${selected && !matches.includes(selected) ? " · 当前声音已保留" : ""}`;
    updateRotation();
  }

  function initializeVoices(data) {
    voices = data.voices;
    const fragment = document.createDocumentFragment();
    const languages = [...new Set(voices.map(voiceGroup))].sort();
    const labels = { en:"英语地区", multilingual:"多语种", local:"本机" };
    const options = [["all", "全部音色"], ["cloud", "云端全部"], ...languages.map(language => [language, labels[language]])];
    for (const [value, label] of options) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; fragment.append(option);
    }
    $("voiceLanguage").replaceChildren(fragment); $("voiceLanguage").value = "all";
    filterVoices(data.defaultVoice || "en-US-AriaNeural");
    resetRotation();
  }

  function setupPlayerVisibility() {
    const player = $("player");
    let pointerY = -1, inside = false, keyboard = false;
    const refresh = () => {
      const focused = (keyboard || ["voiceSearch", "voiceEvery", "repeatEvery"].includes(document.activeElement?.id)) && player.contains(document.activeElement);
      player.classList.toggle("is-visible", playerShouldShow(pointerY, window.innerHeight, inside, focused, player.classList.contains("is-visible")));
    };
    const move = event => { pointerY = event.clientY; inside = player.contains(event.target); refresh(); };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerdown", event => { keyboard = false; move(event); });
    document.addEventListener("keydown", event => { if (event.key === "Tab") keyboard = true; });
    document.addEventListener("pointerleave", () => { pointerY = -1; inside = false; refresh(); });
    player.addEventListener("focusin", refresh);
    player.addEventListener("focusout", () => queueMicrotask(refresh));
    window.addEventListener("blur", () => { pointerY = -1; inside = false; keyboard = false; refresh(); });
    window.addEventListener("resize", () => { refresh(); if (book.length) followCurrent(); });
  }

  function status(text) { $("status").textContent = text; }
  async function pollCache() {
    try {
      const data = await jsonRequest("/api/cache");
      const job = data.preparation || {};
      const labels = { "preparing":"预生成中", "paused-resources":"资源忙，已暂停", "cache-full":"缓存已满", "complete":"已到全文末尾", "error":"预生成出错", "idle":"待机" };
      $("cacheStatus").textContent = `音频缓存 ${(data.bytes / 1e6).toFixed(1)} / ${(data.limitBytes / 1e6).toFixed(0)} MB · ${labels[job.state] || job.state}${Number.isInteger(job.next) ? ` · 第 ${job.next + 1} 段` : ""}${job.message ? ` · ${job.message}` : ""}`;
      if (data.repairs?.state === "regenerating") $("cacheStatus").textContent += ` · 修订音频重建 ${data.repairs.done}/${data.repairs.total}`;
      if (data.prefetch?.jobs) $("cacheStatus").textContent += ` · 播放预取 ${data.prefetch.paragraphs} 段，${data.prefetch.ready}/${data.prefetch.jobs} 音频就绪${data.prefetch.cacheBudgetLimited ? "（缓存预算限制）" : ""}`;
      if (data.model?.released) $("cacheStatus").textContent += ` · 模型已释放，空闲 ${(data.model.availableBytes/1e9).toFixed(1)}GB，重载需约 ${(data.model.reloadRequiredBytes/1e9).toFixed(1)}GB`;
      if (data.model?.loading) $("cacheStatus").textContent += " · 正在重新加载模型";
      if (loading && $("voice").value === "furina-local-en" && (data.resources?.busy || data.model?.released || data.model?.loading)) status("低内存时释放模型，恢复后自动重载；已缓存段落可直接播放");
    } catch (_) {}
    if (!stopped) setTimeout(pollCache, 5000);
  }
  function updateControls() {
    $("play").textContent = loading ? "◌" : desiredPlay ? "Ⅱ" : "▶";
    $("play").classList.toggle("loading", loading);
    $("play").setAttribute("aria-label", desiredPlay ? "暂停" : "播放");
    $("previous").disabled = !book.length || index === 0 || stopped;
    $("next").disabled = !book.length || index === book.length - 1 || stopped;
    $("play").disabled = !book.length || stopped;
    $("rewind").disabled = !book.length || stopped;
    $("forward").disabled = !book.length || stopped;
  }

  function renderReading() {
    const fragment = document.createDocumentFragment();
    const [first, last] = visibleRange(index, book.length);
    wordElements = []; highlighted = -1;
    for (let i = first; i < last; i++) {
      const paragraph = book[i];
      const article = document.createElement("article");
      article.className = `passage${i === index ? " current" : ""}`;
      article.tabIndex = 0;
      article.setAttribute("role", "button");
      article.setAttribute("aria-label", `从第 ${i + 1} 段开始朗读`);
      article.dataset.index = i;
      const label = document.createElement("div");
      label.className = "passage-index";
      const number = document.createElement("span");
      number.textContent = String(i + 1).padStart(4, "0");
      const badge = document.createElement("span");
      badge.textContent = i === index ? "当前段落" : "点击朗读";
      label.append(number, badge);
      const english = document.createElement("p");
      english.className = "passage-english"; english.lang = "en";
      if (i === index && words.length) {
        let cursor = 0;
        words.forEach((word, j) => {
          english.append(document.createTextNode(paragraph.english.slice(cursor, word.from)));
          const span = document.createElement("span");
          span.className = "word";
          span.textContent = paragraph.english.slice(word.from, word.to);
          span.dataset.word = j;
          english.append(span); wordElements.push(span); cursor = word.to;
        });
        english.append(document.createTextNode(paragraph.english.slice(cursor)));
      } else english.textContent = paragraph.english;
      const chinese = document.createElement("p");
      chinese.className = "passage-chinese"; chinese.lang = "zh-CN";
      chinese.textContent = paragraph.chinese;
      article.append(label, english, chinese);
      article.addEventListener("click", event => {
        const selection = window.getSelection();
        if (selection && !selection.isCollapsed) return;
        // A highlighted word can also be used as a precise seek point.
        if (i === index && event.target.dataset.word !== undefined && audio.src) {
          audio.currentTime = words[Number(event.target.dataset.word)].start;
          desiredPlay = true; playAudio(); return;
        }
        navigate(i, { autoplay: true });
      });
      article.addEventListener("keydown", event => {
        if (event.code === "Enter" || event.code === "Space") {
          event.preventDefault(); navigate(i, { autoplay: true });
        }
      });
      fragment.append(article);
    }
    $("reading").replaceChildren(fragment);
    $("position").textContent = `第 ${(index + 1).toLocaleString()} / ${book.length.toLocaleString()} 段`;
    $("chapter").textContent = book[index].chapter;
    for (const result of $("results").children) result.classList.toggle("selected", Number(result.dataset.index) === index);
    updateControls();
  }

  function followCurrent() {
    if (!$("follow").checked) return;
    const reading = $("reading");
    const current = reading.querySelector(".current");
    if (!current) return;
    // Extra leading space also allows the first few paragraphs to be centered.
    reading.style.paddingTop = "0px";
    const bounds = current.getBoundingClientRect();
    const position = centerPosition(window.scrollY + bounds.top + bounds.height / 2, window.innerHeight);
    reading.style.paddingTop = `${position.padding}px`;
    window.scrollTo({ top: position.scroll, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  }

  function updateHighlight() {
    const next = activeWord(words, audio.currentTime);
    if (next === highlighted) return;
    if (wordElements[highlighted]) wordElements[highlighted].classList.remove("active");
    highlighted = next;
    const element = wordElements[next];
    if (element) {
      element.classList.add("active");
      if ($("follow").checked) {
        const bounds = element.getBoundingClientRect();
        const player = $("player");
        const bottom = player.classList.contains("is-visible") ? player.getBoundingClientRect().top - 15 : window.innerHeight - 15;
        if (bounds.top < 24 || bounds.bottom > bottom) element.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
  }

  function updateTimeline() {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    $("timeline").max = String(duration || 1);
    $("timeline").value = String(audio.currentTime || 0);
    $("timeline").disabled = !duration || loading || stopped;
    $("elapsed").textContent = formatTime(audio.currentTime);
    $("duration").textContent = formatTime(duration);
    updateHighlight();
  }

  function animate() {
    frame = null; updateHighlight();
    if (!audio.paused && !stopped) frame = requestAnimationFrame(animate);
  }
  function startAnimation() { if (frame === null) frame = requestAnimationFrame(animate); }

  async function jsonRequest(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json();
    if (!response.ok) {
      const error = new Error(body.message || `请求失败 (${response.status})`);
      error.code = response.status; throw error;
    }
    return body;
  }
  function abortError() { return new DOMException("Request superseded", "AbortError"); }
  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(abortError()); return; }
      const abort = () => { clearTimeout(timer); reject(abortError()); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
      signal.addEventListener("abort", abort, { once: true });
    });
  }
  function requestParagraph(i, voice, signal) {
    return jsonRequest("/api/speech", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ index: i, voice }), signal });
  }
  function warmAhead(i, voice) {
    const plan = rotationEvery() || $("paragraphRepeat").checked || pinnedVoice()
      ? playbackPlan(book, i, paragraphBaseVoice || voice, rotationOrder(), rotationEvery(), rotationCompleted, repeatEvery(), repeatPass, 21*repeatEvery(), pinnedVoice(), 20) : null;
    return jsonRequest("/api/prefetch", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: i, voice, count: 20, ...(plan ? { plan } : {}) }) }).catch(() => {});
  }
  async function speechReady(i, voice, signal) {
    const deadline = Date.now() + (voice === "furina-local-en" ? 1200000 : 85000);
    let result = null;
    while (Date.now() <= deadline) {
      try {
        result = result?.state === "pending"
          ? await jsonRequest(`/api/speech?key=${encodeURIComponent(result.key)}`, { signal })
          : await requestParagraph(i, voice, signal);
      } catch (error) {
        if (![404,429,503].includes(error.code) && !(error instanceof TypeError)) throw error;
        status("语音缓存正在重建或重新连接，请稍候…");
        result = null;
        await sleep(700, signal);
        continue;
      }
      if (result.state === "ready" || result.state === "skipped") return result;
      if (result.state !== "pending") throw new Error(result.message || "语音生成失败。");
      await sleep(250, signal);
    }
    throw new Error("语音生成超时，请重试。");
  }

  function loadAudio(source, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(abortError()); return; }
      const cleanup = () => {
        clearTimeout(timer); audio.removeEventListener("loadedmetadata", ready);
        audio.removeEventListener("error", failed); signal.removeEventListener("abort", abort);
      };
      const ready = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error("浏览器无法载入音频，请重新播放本段。")); };
      const abort = () => { cleanup(); reject(abortError()); };
      const timer = setTimeout(failed, 20000);
      audio.addEventListener("loadedmetadata", ready, { once: true });
      audio.addEventListener("error", failed, { once: true });
      signal.addEventListener("abort", abort, { once: true });
      audio.src = source; audio.load();
    });
  }

  async function playAudio() {
    try { await audio.play(); }
    catch (error) {
      if (error.name === "AbortError") return;
      desiredPlay = false; updateControls();
      status(error.name === "NotAllowedError" ? "音频已就绪，请点击播放。" : `播放失败：${error.message}`);
    }
  }

  async function navigate(target, { autoplay = true, offset = 0, fromEnd = false, automatic = false } = {}) {
    if (!book.length || stopped) return;
    if (!automatic) { resetRotation(paragraphBaseVoice || $("voice").value); filterVoices(currentSequence()[0]); }
    const ownRevision = ++revision;
    if (controller) controller.abort();
    controller = new AbortController();
    const signal = controller.signal;
    loading = true; desiredPlay = autoplay; loadedVoice = null;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    words = []; updateTimeline();
    const voice = $("voice").value;
    let nextIndex = Math.max(0, Math.min(book.length - 1, target));
    let step = fromEnd || offset < 0 || target < index ? -1 : 1;
    try {
      while (true) {
        const spokenIndex = nextSpeakable(book, nextIndex, step);
        if (spokenIndex < 0) {
          index = step > 0 ? book.length - 1 : 0;
          loading = false; desiredPlay = false;
          audio.removeAttribute("src"); audio.load();
          words = []; renderReading(); updateTimeline(); followCurrent();
          status(step > 0 ? "全书朗读完成" : "前面没有可朗读的文字");
          return;
        }
        nextIndex = spokenIndex;
        index = nextIndex; words = []; renderReading();
        status("正在准备本段语音…"); followCurrent();
        const preparing = speechReady(index, voice, signal);
        warmAhead(index, voice);
        const result = await preparing;
        if (ownRevision !== revision) return;
        if (result.state === "skipped") { nextIndex = index + step; continue; }
        words = result.words; renderReading();
        await loadAudio(result.audio, signal);
        if (ownRevision !== revision) return;
        audio.playbackRate = Number($("speed").value);
        audio.preservesPitch = true;
        const position = fromEnd ? audio.duration + offset : offset;
        // Carry the remainder across paragraph boundaries for true +/-10-second seeking.
        if (position < 0 && index > 0) { offset = position; fromEnd = true; step = -1; nextIndex = index - 1; continue; }
        if (position >= audio.duration && index < book.length - 1) { offset = position - audio.duration; fromEnd = false; step = 1; nextIndex = index + 1; continue; }
        audio.currentTime = Math.max(0, Math.min(audio.duration, position));
        loadedVoice = voice; break;
      }
      loading = false; updateControls(); updateTimeline(); followCurrent();
      status(words.length ? "音频已就绪 · 点击英文单词也可跳转" : "音频已就绪 · 本段未返回逐词时间戳");
      if (desiredPlay) await playAudio();
    } catch (error) {
      if (ownRevision !== revision || error.name === "AbortError") return;
      loading = false; desiredPlay = false; updateControls(); updateTimeline();
      status(`无法朗读：${error.message} 点击播放可重试。`);
    }
  }

  function togglePlay() {
    if (!book.length || stopped) return;
    if (loading) { desiredPlay = !desiredPlay; updateControls(); status(desiredPlay ? "准备完成后开始播放…" : "准备完成后保持暂停"); return; }
    if (!audio.src || loadedVoice !== $("voice").value) { navigate(index, { autoplay: true, automatic: true }); return; }
    if (!audio.paused) { desiredPlay = false; audio.pause(); }
    else { desiredPlay = true; playAudio(); }
  }

  function skip(seconds) {
    if (!book.length || stopped) return;
    const target = (audio.currentTime || 0) + seconds;
    if (!loading && audio.src && Number.isFinite(audio.duration) && target >= 0 && target < audio.duration) {
      audio.currentTime = target; updateTimeline(); return;
    }
    navigate(index, { autoplay: desiredPlay, offset: target });
  }

  function search(event) {
    if (event) event.preventDefault();
    if (!book.length || stopped) return;
    const query = $("query").value;
    const matches = findMatches(indexed, query);
    const fragment = document.createDocumentFragment();
    for (const i of matches.slice(0, 100)) {
      const paragraph = book[i], result = document.createElement("button");
      result.className = `search-result${i === index ? " selected" : ""}`;
      result.dataset.index = i;
      const position = document.createElement("span"); position.className = "result-position"; position.textContent = `第 ${(i + 1).toLocaleString()} 段`;
      const excerpt = document.createElement("span"); excerpt.className = "result-excerpt";
      const at = indexed[i].indexOf(normalize(query)); const start = Math.max(0, at - 35);
      excerpt.textContent = `${start ? "…" : ""}${paragraph.english.slice(start, start + 160)}${start + 160 < paragraph.english.length ? "…" : ""}`;
      const chapter = document.createElement("span"); chapter.className = "result-chapter"; chapter.textContent = paragraph.chapter;
      result.append(position, excerpt, chapter);
      result.addEventListener("click", () => navigate(i, { autoplay: true })); fragment.append(result);
    }
    $("results").replaceChildren(fragment);
    $("searchSummary").textContent = !normalize(query) ? "请输入英文关键词或一句原文。" : `找到 ${matches.length.toLocaleString()} 段${matches.length > 100 ? "，显示前 100 段，请增加关键词缩小范围。" : "，点击结果开始朗读。"}`;
  }

  $("searchForm").addEventListener("submit", search);
  $("play").addEventListener("click", togglePlay);
  $("previous").addEventListener("click", () => navigate(index - 1, { autoplay: desiredPlay || !audio.src }));
  $("next").addEventListener("click", () => navigate(index + 1, { autoplay: desiredPlay || !audio.src }));
  $("rewind").addEventListener("click", () => skip(-10));
  $("forward").addEventListener("click", () => skip(10));
  $("speed").addEventListener("input", () => {
    const speed = Number($("speed").value);
    audio.playbackRate = speed;
    $("speedValue").textContent = `${speed.toFixed(2)}×`;
    $("speed").setAttribute("aria-valuetext", `${speed.toFixed(2)}倍`);
    updateHighlight();
  });
  $("voice").addEventListener("change", () => { resetRotation(); if (audio.src || loading) navigate(index, { autoplay: desiredPlay }); });
  const refreshPlan = () => { if (book.length && (audio.src || loading)) warmAhead(index, $("voice").value); };
  const refreshCycle = () => {
    resetRotation();
    if (pinnedVoice()) {
      filterVoices(currentSequence()[0]);
      if (audio.src || loading) navigate(index, { autoplay:desiredPlay });
    } else refreshPlan();
  };
  const filtersChanged = () => { refreshCycle(); filterVoices(); };
  $("voiceSearch").addEventListener("input", filtersChanged);
  $("voiceLanguage").addEventListener("change", filtersChanged);
  $("voiceRotation").addEventListener("change", refreshCycle);
  $("voiceEvery").addEventListener("change", () => {
    $("voiceEvery").value = String(Math.max(1, Math.min(1000, Math.trunc(Number($("voiceEvery").value) || 1))));
    refreshCycle();
  });
  $("paragraphRepeat").addEventListener("change", refreshCycle);
  $("repeatEvery").addEventListener("change", refreshCycle);
  $("furinaFirst").addEventListener("change", () => {
    if (pinnedVoice()) $("paragraphRepeat").checked = true;
    refreshCycle();
  });
  $("follow").addEventListener("change", followCurrent);
  $("timeline").addEventListener("input", () => { if (Number.isFinite(audio.duration)) { audio.currentTime = Number($("timeline").value); updateTimeline(); } });
  audio.addEventListener("timeupdate", updateTimeline);
  audio.addEventListener("seeked", updateTimeline);
  audio.addEventListener("play", () => { desiredPlay = true; updateControls(); status("正在朗读 · 英文逐词高亮"); startAnimation(); });
  audio.addEventListener("pause", () => { if (!loading) { desiredPlay = false; updateControls(); status("已暂停"); } updateHighlight(); });
  audio.addEventListener("ended", () => {
    if (loading || stopped) return;
    const sequence = currentSequence();
    if (repeatPass+1 < sequence.length) {
      ++repeatPass; filterVoices(sequence[repeatPass]);
      navigate(index, { autoplay:true, automatic:true });
      return;
    }
    repeatPass = 0;
    const next = nextSpeakable(book, index + 1);
    const advanced = advanceRotation(paragraphBaseVoice || $("voice").value, rotationOrder(), rotationEvery(), rotationCompleted);
    rotationCompleted = advanced.completed;
    if (next >= 0) {
      paragraphBaseVoice = advanced.voice;
      filterVoices(currentSequence()[0]);
      navigate(next, { autoplay: true, automatic: true });
    } else {
      index = book.length - 1; words = []; loadedVoice = null; desiredPlay = false;
      filterVoices(currentSequence()[0]);
      audio.removeAttribute("src"); audio.load();
      renderReading(); updateTimeline(); updateRotation(); updateControls(); status("全书朗读完成");
    }
  });
  audio.addEventListener("error", () => { if (!loading && !stopped && audio.src) { desiredPlay = false; loadedVoice = null; updateControls(); status("音频播放失败，点击播放重新生成本段。"); } });
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.target.closest("input,select,textarea,[contenteditable=true]") || event.ctrlKey || event.metaKey) return;
    if (event.code === "Space") {
      if (event.target.closest("button,article")) return;
      event.preventDefault(); togglePlay();
    }
    else if (event.code === "KeyJ") { event.preventDefault(); skip(-10); }
    else if (event.code === "KeyL") { event.preventDefault(); skip(10); }
    else if (event.altKey && event.code === "ArrowLeft") { event.preventDefault(); navigate(index - 1, { autoplay: desiredPlay }); }
    else if (event.altKey && event.code === "ArrowRight") { event.preventDefault(); navigate(index + 1, { autoplay: desiredPlay }); }
  });
  $("shutdown").addEventListener("click", async () => {
    try {
      await jsonRequest("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      stopped = true; ++revision; if (controller) controller.abort();
      audio.pause(); audio.removeAttribute("src"); audio.load(); loading = false; desiredPlay = false;
      $("connection").textContent = "本地服务已关闭"; $("shutdown").disabled = true;
      updateControls(); updateTimeline(); status("服务已关闭。下次运行 StartReader.vbs 后刷新此页即可。");
    } catch (error) { status(`关闭失败：${error.message}`); }
  });

  async function initialize() {
    try {
      const data = await jsonRequest("/api/book");
      book = data.paragraphs; indexed = book.map(p => normalize(p.english));
      if (Number.isInteger(data.startIndex)) index = data.startIndex;
      index = Math.min(index, book.length - 1);
      $("bookTitle").textContent = data.title;
      $("bookCount").textContent = `${book.length.toLocaleString()} 段中英对照`;
      initializeVoices(data);
      if (data.diskCache) pollCache();
      renderReading(); status("点击播放，或搜索想开始的位置");
    } catch (error) { status(`载入失败：${error.message}。请确认本地服务已启动后刷新。`); $("connection").textContent = "连接失败"; }
  }
  setupPlayerVisibility();
  initialize();
}());
