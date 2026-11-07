"""ParallelText loopback-only bilingual reader. TTS uses an online service."""
from __future__ import annotations
import asyncio
import argparse
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import Future
from contextlib import suppress
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import edge_tts
from Voices import load_catalogue
from SpeechCache import DiskCache, ResourceMonitor, FurinaClient
from MemoryGuard import MemoryGuard

BASE = Path(__file__).resolve().parent
BOOK = BASE / "Work" / "Book" if (BASE / "Work" / "Book" / "Aligned.json").exists() else BASE / "Examples" / "Book"
PORT = 8765 if (BASE / "Work" / "Book" / "Aligned.json").exists() else 8766
APP = "ParallelText"
VOICES = [
    {"id": "furina-local-en", "name": "Furina · 本机英文"},
    {"id": "en-US-AriaNeural", "name": "Aria · 美式女声"},
    {"id": "en-US-JennyNeural", "name": "Jenny · 美式女声"},
    {"id": "en-US-GuyNeural", "name": "Guy · 美式男声"},
    {"id": "en-GB-SoniaNeural", "name": "Sonia · 英式女声"},
]
_voice_map = {v["id"]: v for v in VOICES}
_voice_map.update({v["id"]: v for v in load_catalogue()})
VOICES = list(_voice_map.values())
VOICE_IDS = set(_voice_map)
SETTINGS = {}
QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-", "–": "-"})


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).translate(QUOTES).casefold().split())


def has_speech(text):
    return any(char.isalnum() for char in text)


def book_id(book):
    return hashlib.sha256(json.dumps(book, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_book():
    aligned = json.loads((BOOK / "Aligned.json").read_text(encoding="utf-8-sig"))
    english = [" ".join(p.split()) for p in re.split(
        r"\n\s*\n", (BOOK / "English.txt").read_text(encoding="utf-8-sig")) if p.strip()]
    if len(english) != len(aligned):
        raise ValueError("English.txt 与 Aligned.json 的段落数量不一致。")
    book = []
    for i, row in enumerate(aligned):
        if english[i] != " ".join(row["translation"].split()):
            raise ValueError(f"第 {i + 1} 段英文与配对数据不一致，请核对原文件。")
        book.append({"index": i, "english": english[i], "chinese": row["source"], "chapter": row["chapter"]})
    if not book:
        raise ValueError("没有可朗读的段落。")
    return book


def map_boundaries(text, events):
    """Use real word times and exact source offsets (UTF-16 offsets for JS)."""
    cursor, words = 0, []
    for event in events:
        token = event["text"]
        start = text.find(token, cursor)
        if start < 0:
            for position in range(cursor, len(text)):
                if text[position:position + len(token)].translate(QUOTES).casefold() == token.translate(QUOTES).casefold():
                    start = position
                    break
        if start < 0:
            continue
        end = start + len(token)
        words.append({"start": event["offset"] / 10_000_000,
                      "end": (event["offset"] + event["duration"]) / 10_000_000,
                      "from": len(text[:start].encode("utf-16-le")) // 2,
                      "to": len(text[:end].encode("utf-16-le")) // 2})
        cursor = end
    return words


class BusyError(Exception):
    pass


class SpeechStore:
    def __init__(self, book, directory, edge_workers=2):
        if type(edge_workers) is not int or not 1 <= edge_workers <= 4:
            raise ValueError("edgePrefetchWorkers must be an integer between 1 and 4")
        self.book, self.directory = book, directory
        self.edge_workers = edge_workers
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Speech")
        self.prefetch_pool = ThreadPoolExecutor(max_workers=edge_workers, thread_name_prefix="EdgePrefetch")
        self.local_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="FurinaSpeech")
        self.lock = threading.Lock()
        self.entries = {}
        self.background = set()
        self.current_key = None
        self.stopped = threading.Event()
        self.local_keys = set()
        self.prefetch_targets = []
        self.prefetch_keys = set()
        self.prefetch_origin = 0
        self.prefetch_retry = {}
        self.prefetch_event = threading.Event()
        self.prefetch_thread = None

    def key(self, index, voice):
        return hashlib.sha256(f"{index}:{voice}".encode()).hexdigest()[:24]

    def request(self, index, voice, background=False):
        if not has_speech(self.book[index]["english"]):
            return {"state": "skipped", "index": index, "voice": voice, "reason": "no-spoken-content"}
        key = self.key(index, voice)
        with self.lock:
            if not background:
                self.current_key = key
            # A queued speculative job must not keep the current paragraph waiting.
            if not background and key in self.entries and self.entries[key].cancel():
                del self.entries[key]
                self.background.discard(key)
            if key in self.entries and self.entries[key].done() and (self.entries[key].cancelled() or self.entries[key].exception()):
                del self.entries[key]
                self.background.discard(key)
            if key not in self.entries:
                local = voice == "furina-local-en"
                if background:
                    if local:
                        pending = sum(not f.done() for k,f in self.entries.items() if k in self.local_keys)
                        planned = {self.key(x["index"],x["voice"]) for x in self.prefetch_targets if x["voice"] == "furina-local-en"}
                        urgent = any(k not in self.entries or not self.entries[k].done() or self.entries[k].cancelled() or self.entries[k].exception() for k in planned)
                        if pending >= 1 or (key not in planned and urgent):
                            raise BusyError("正在准备优先朗读的本机语音。")
                    elif sum(not f.done() for k,f in self.entries.items() if k in self.background and k not in self.local_keys) >= self.edge_workers+1:
                        raise BusyError("Edge 预取名额已满，任务会继续排队。")
                self._prune()
                pool = self.local_pool if local else self.prefetch_pool if background else self.pool
                if local:
                    self.local_keys.add(key)
                if background:
                    self.background.add(key)
                self.entries[key] = pool.submit(self._generate, index, voice, key)
            return self._status(key)

    def prefetch(self, index, voice, count=20):
        wanted = []
        for i in range(index+1, len(self.book)):
            if has_speech(self.book[i]["english"]):
                wanted.append(i)
                if len(wanted) == count:
                    break
        return self.prefetch_plan(index, voice, [{"index": i, "voice": voice} for i in wanted])

    def prefetch_plan(self, index, voice, plan):
        keep = {self.current_key, self.key(index, voice), *(self.key(item["index"], item["voice"]) for item in plan)}
        with self.lock:
            self.prefetch_origin = index
            self.prefetch_targets = [dict(item) for item in plan]
            self.prefetch_keys = {self.key(x["index"],x["voice"]) for x in plan}
            self.prefetch_retry = {k:v for k,v in self.prefetch_retry.items() if k in keep}
            for key in list(self.entries):
                if key in self.background and key not in keep and not self.entries[key].done() and self.entries[key].cancel():
                    del self.entries[key]
                    self.background.discard(key)
            if self.prefetch_thread is None:
                self.prefetch_thread = threading.Thread(target=self._prefetch_loop,daemon=True,name="PlaybackPrefetch")
                self.prefetch_thread.start()
        self.prefetch_event.set()
        return {"state": "scheduled", "indices": [item["index"] for item in plan]}

    def _prefetch_loop(self):
        while not self.stopped.is_set():
            self.prefetch_event.wait(.5)
            self.prefetch_event.clear()
            with self.lock:
                targets = list(self.prefetch_targets)
            for item in targets:
                if self.stopped.is_set(): return
                key = self.key(item["index"],item["voice"])
                with self.lock:
                    if targets != self.prefetch_targets: break
                    previous = self.prefetch_retry.get(key)
                    if previous and time.monotonic() < previous[2]: continue
                    future = self.entries.get(key)
                    if future:
                        if not future.done(): continue
                        if not future.cancelled() and future.exception() is None: continue
                        if previous is None or previous[0] is not future:
                            attempts = previous[1]+1 if previous else 1
                            self.prefetch_retry[key] = (future,attempts,time.monotonic()+min(30,2**min(attempts,5)))
                            continue
                try:
                    self.request(item["index"],item["voice"],background=True)
                except BusyError:
                    continue
                except Exception as error:
                    logging.warning("Prefetch will retry paragraph %s: %s",item["index"]+1,error)
                    with self.lock:
                        self.prefetch_retry[key] = (None,1,time.monotonic()+5)

    def prefetch_status(self):
        with self.lock:
            ready = pending = failed = 0
            for item in self.prefetch_targets:
                future = self.entries.get(self.key(item["index"],item["voice"]))
                if not future: continue
                if not future.done(): pending += 1
                elif future.cancelled() or future.exception(): failed += 1
                else: ready += 1
            return {"paragraphs":len({x["index"] for x in self.prefetch_targets if x["index"] > self.prefetch_origin}),
                    "jobs":len(self.prefetch_targets),"ready":ready,"pending":pending,"failed":failed,
                    "waiting":len(self.prefetch_targets)-ready-pending-failed,"edgeWorkers":self.edge_workers,"localWorkers":1,
                    "cacheBudgetLimited":getattr(self,"prefetch_budget_limited",False)}

    def _prune(self):
        total = sum(p.stat().st_size for p in self.directory.glob("*.mp3"))
        for key in list(self.entries):
            if len(self.entries) < 96 and total < 64 * 1024 * 1024:
                break
            if key != self.current_key and key not in self.prefetch_keys and self.entries[key].done():
                path = self.directory / f"{key}.mp3"
                if path.exists():
                    total -= path.stat().st_size
                    path.unlink(missing_ok=True)
                del self.entries[key]
                self.background.discard(key)

    def _status(self, key):
        future = self.entries.get(key)
        if future is None:
            raise KeyError("语音缓存已过期，请重新播放本段。")
        if not future.done():
            return {"state": "pending", "key": key}
        try:
            return {"state": "ready", "key": key, **future.result()}
        except Exception as error:
            return {"state": "error", "key": key, "message": str(error) or type(error).__name__}

    def status(self, key):
        with self.lock:
            return self._status(key)

    def audio(self, key):
        with self.lock:
            if self._status(key)["state"] != "ready":
                raise KeyError("音频尚未就绪。")
            return (self.directory / f"{key}.mp3").read_bytes()

    async def _stream(self, text, voice, path):
        for attempt in range(2):
            boundaries = []
            try:
                with path.open("wb") as audio:
                    speech = edge_tts.Communicate(text, voice, boundary="WordBoundary", connect_timeout=10, receive_timeout=25)
                    async for chunk in speech.stream():
                        if chunk["type"] == "audio":
                            audio.write(chunk["data"])
                        elif chunk["type"] == "WordBoundary":
                            boundaries.append(chunk)
                if not path.stat().st_size:
                    raise RuntimeError("语音服务返回了空音频。")
                return map_boundaries(text, boundaries)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt:
                    raise
                await asyncio.sleep(1)

    async def _generate_async(self, index, voice, key):
        task = asyncio.create_task(self._stream(self.book[index]["english"], voice, self.directory / f"{key}.mp3"))
        try:
            deadline = time.monotonic() + 70
            while not task.done():
                if self.stopped.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("语音生成已停止或超时，请重试。")
                await asyncio.wait([task], timeout=0.1)
            return {"audio": f"/audio/{key}.mp3", "words": await task, "index": index, "voice": voice}
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _generate(self, index, voice, key):
        try:
            return asyncio.run(self._generate_async(index, voice, key))
        except Exception:
            logging.exception("Speech generation failed for paragraph %s", index + 1)
            (self.directory / f"{key}.mp3").unlink(missing_ok=True)
            raise

    def close(self):
        self.stopped.set()
        self.prefetch_event.set()
        if self.prefetch_thread: self.prefetch_thread.join(timeout=2)
        self.prefetch_pool.shutdown(wait=True, cancel_futures=True)
        self.local_pool.shutdown(wait=True, cancel_futures=True)
        self.pool.shutdown(wait=True, cancel_futures=True)


class CachedSpeechStore(SpeechStore):
    def __init__(self, book, directory, settings=None):
        self.settings = settings or {}
        super().__init__(book, directory, self.settings.get("edgePrefetchWorkers",2))
        self.identity = book_id(book)
        self.cache = DiskCache(directory, int(self.settings.get("speechCacheBytes", 1_000_000_000)))
        self.monitor = ResourceMonitor(self.settings.get("furinaPidFile"))
        self.client = FurinaClient(self.settings.get("furinaUrl", "http://127.0.0.1:9880"), self.monitor, self.stopped)
        self.guard = MemoryGuard(self.settings,self.stopped,self.directory/"ModelState.json")
        self.client.guard = self.guard
        self.guard.start()
        self.preparation = {"state": "idle", "done": 0}
        self.preparing = None
        self.repairs = {"state":"idle", "done":0, "total":0}
        self.anchor = 0
        self.prepare_lock = threading.Lock()

    def key(self, index, voice):
        backend = self.client.url if voice == "furina-local-en" else "edge-tts-7.3.1"
        return hashlib.sha256(("v2:"+voice+backend+self.book[index]["english"]).encode()).hexdigest()[:24]

    def request(self, index, voice, background=False):
        key = self.key(index, voice)
        if not background:
            self.anchor = index
        with self.lock:
            existing = self.entries.get(key)
            if existing and existing.done() and not existing.cancelled() and existing.exception() is None and not self.cache.get(key):
                del self.entries[key]
                self.background.discard(key)
            if key not in self.entries:
                cached = self.cache.get(key)
                if cached:
                    if cached.get("book") != self.identity or cached.get("index") != index:
                        cached = {**cached,"book":self.identity,"index":index}
                        self.cache.put(key,cached,key)
                    future = Future()
                    future.set_result({**cached, "index":index, "voice":voice})
                    self.entries[key] = future
        return super().request(index, voice, background)

    def _prune(self):
        self.cache.prune(self.current_key)
        for key in list(self.entries):
            if key != self.current_key and key not in self.prefetch_keys and self.entries[key].done() and (len(self.entries) > 256 or not (self.directory/(key+".json")).exists()):
                del self.entries[key]
                self.background.discard(key)

    def _generate(self, index, voice, key):
        if voice == "furina-local-en":
            # The backend serializes model execution; never pile requests into it.
            background = key in self.background or key != self.current_key
            stats = self.client.generate(self.book[index]["english"], self.directory/(key+".wav"), background)
            result = {"audio":f"/audio/{key}.wav", "words":[], "index":index, "voice":voice, **(stats or {})}
            extension = "wav"
        else:
            result = super()._generate(index, voice, key)
            extension = "mp3"
        self.cache.put(key, {**result, "format":extension, "book":self.identity}, self.current_key)
        return result

    def audio(self, key):
        data = self.cache.get(key)
        if not data: raise KeyError("Audio cache expired")
        return (self.directory/(key+"."+data["format"])).read_bytes()

    def prefetch(self, index, voice, count=20):
        if voice == "furina-local-en":
            result = super().prefetch(index,voice,count)
            self.start_preparation(index+1, voice)
            return {**result,"preparation":self.preparation}
        return super().prefetch(index, voice, count)

    def prefetch_plan(self,index,voice,plan):
        # Keep the nearest playback jobs when a very large voice/repetition set
        # would exceed the shared disk budget. Foreground requests remain allowed.
        accepted, size = [], 0
        for item in plan:
            cached = self.cache.get(self.key(item["index"],item["voice"]))
            estimate = cached["bytes"] if cached else len(self.book[item["index"]]["english"])*(5000 if item["voice"] == "furina-local-en" else 500)+12000
            if size+estimate > self.cache.limit*.9: break
            size += estimate
            accepted.append(item)
        self.prefetch_budget_limited = len(accepted) < len(plan)
        return super().prefetch_plan(index,voice,accepted)

    def start_repairs(self, items):
        self.repairs = {"state":"regenerating", "done":0, "total":len(items), "errors":[]}
        def one(item):
            if self.stopped.is_set(): return
            index, voice = item["index"], item["voice"]
            self.repairs["current"] = index
            try:
                while not self.stopped.is_set():
                    try:
                        result = self.request(index,voice,background=True)
                        break
                    except BusyError:
                        self.stopped.wait(.3)
                else: return
                while result["state"] == "pending" and not self.stopped.wait(.25):
                    result = self.status(result["key"])
                if self.stopped.is_set(): return
                if result["state"] != "ready": raise RuntimeError(result.get("message","Audio failed"))
                with self.prepare_lock:
                    self.repairs["done"] += 1
            except Exception as error:
                self.repairs["errors"].append({"index":index,"message":str(error)})
        def run():
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix="RepairQueue") as pool:
                list(pool.map(one,items))
            self.repairs["state"] = "complete" if not self.repairs["errors"] else "error"
        threading.Thread(target=run,daemon=True,name="RepairAudio").start()

    def start_preparation(self, start, voice):
        with self.prepare_lock:
            if self.preparing and self.preparing.is_alive():
                return
            self.preparation = {"state":"preparing", "start":start, "next":start, "done":0, "voice":voice, "end":len(self.book)}
            def run():
                state = self.preparation
                try:
                    for index in range(start, len(self.book)):
                        if self.stopped.is_set(): break
                        state["next"] = index
                        if not has_speech(self.book[index]["english"]): continue
                        key = self.key(index, voice)
                        if not self.cache.get(key):
                            # Retain unread audio. Only roll out consumed entries to make room.
                            stats = self.cache.stats()
                            estimate = max(1_000_000, stats["bytes"]//max(1,stats["files"]))
                            self.cache.prune(self.current_key, room=estimate, before=self.anchor, book=self.identity)
                            if self.cache.stats()["bytes"]+estimate > self.cache.limit:
                                state["state"] = "cache-full"
                                return
                            while self.monitor.sample()["busy"] or self.guard.waiting():
                                state["state"] = "paused-resources"
                                if self.stopped.wait(2): return
                            state["state"] = "preparing"
                            while not self.stopped.is_set():
                                try:
                                    result = self.request(index, voice, background=True)
                                    break
                                except BusyError:
                                    self.stopped.wait(.5)
                            else: return
                            while result["state"] == "pending" and not self.stopped.wait(.5):
                                result = self.status(result["key"])
                            if self.stopped.is_set(): return
                            if result["state"] != "ready": raise RuntimeError(result.get("message", "Speech generation failed"))
                        state["done"] += 1
                    state["state"] = "stopped" if self.stopped.is_set() else "complete"
                except Exception as error:
                    state.update(state="error", message=str(error))
                    logging.exception("Preparation failed")
            self.preparing = threading.Thread(target=run, daemon=True, name="PrepareBook")
            self.preparing.start()


def make_handler(book, store):
    start_index = next((i for i,p in enumerate(book) if re.match(rf"^Chapter {SETTINGS.get('startChapter', -1)}\b", p["english"])), 2)
    book_json = json.dumps({"title": book[0]["english"], "paragraphs": book, "voices": VOICES,
                           "defaultVoice":SETTINGS.get("defaultVoice", "en-US-AriaNeural"), "startIndex":start_index,
                           "diskCache":isinstance(store, CachedSpeechStore)}, ensure_ascii=False).encode("utf-8")
    compressed_book = gzip.compress(book_json)
    files = {"/": ("Reader.html", "text/html; charset=utf-8"),
             "/Reader.css": ("Reader.css", "text/css; charset=utf-8"),
             "/Reader.js": ("Reader.js", "text/javascript; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        server_version = APP

        def log_message(self, *_):
            pass

        def _valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

        def _send(self, data, content_type, status=200, extra=None):
            self.send_response(status)
            for key, value in {"Content-Type": content_type, "Content-Length": len(data), "Cache-Control": "no-store",
                               "X-Content-Type-Options": "nosniff",
                               "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                               **(extra or {})}.items():
                self.send_header(key, str(value))
            self.end_headers()
            if self.command != "HEAD":
                with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.wfile.write(data)

        def _json(self, payload, status=200):
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._valid_host():
                return self._json({"message": "Invalid host"}, 403)
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/api/health":
                    return self._json({"app": APP, "paragraphs": len(book), "bookId": book_id(book)})
                if parsed.path in files:
                    name, content_type = files[parsed.path]
                    return self._send((BASE / name).read_bytes(), content_type)
                if parsed.path == "/api/book":
                    compressed = "gzip" in self.headers.get("Accept-Encoding", "")
                    return self._send(compressed_book if compressed else book_json, "application/json; charset=utf-8",
                                      extra={"Vary": "Accept-Encoding", **({"Content-Encoding": "gzip"} if compressed else {})})
                if parsed.path == "/api/cache" and isinstance(store, CachedSpeechStore):
                    return self._json({**store.cache.stats(), "preparation":store.preparation, "resources":store.monitor.state,
                                       "repairs":store.repairs,"playbackIndex":store.anchor,"model":store.guard.status,
                                       "prefetch":store.prefetch_status()})
                if parsed.path == "/api/speech":
                    return self._json(store.status(parse_qs(parsed.query).get("key", [""])[0]))
                match = re.fullmatch(r"/audio/([a-f0-9]{24})\.(mp3|wav)", parsed.path)
                if match:
                    return self._audio(store.audio(match[1]), "audio/wav" if match[2] == "wav" else "audio/mpeg")
                return self._json({"message": "Not found"}, 404)
            except KeyError as error:
                self._json({"message": str(error)}, 404)
            except Exception as error:
                logging.exception("GET failed")
                self._json({"message": str(error)}, 500)

        def _audio(self, data, content_type="audio/mpeg"):
            headers = {"Accept-Ranges": "bytes"}
            byte_range = self.headers.get("Range")
            if byte_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
                if not match or not any(match.groups()):
                    return self._send(b"", content_type, 416, {"Content-Range": f"bytes */{len(data)}"})
                first, last = match.groups()
                start = int(first) if first else max(0, len(data) - int(last))
                end = min(int(last), len(data) - 1) if first and last else len(data) - 1
                if start >= len(data) or end < start:
                    return self._send(b"", content_type, 416, {"Content-Range": f"bytes */{len(data)}"})
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                return self._send(data[start:end + 1], content_type, 206, headers)
            return self._send(data, content_type, extra=headers)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._valid_host() or (origin and origin not in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}):
                return self._json({"message": "Invalid origin"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"message": "Expected JSON"}, 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid JSON body")
                if self.path == "/api/shutdown":
                    self._json({"state": "stopped"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if self.path not in {"/api/speech", "/api/prefetch"}:
                    return self._json({"message": "Not found"}, 404)
                index, voice = payload.get("index"), payload.get("voice")
                if type(index) is not int or not 0 <= index < len(book) or not isinstance(voice, str) or voice not in VOICE_IDS:
                    raise ValueError("Invalid paragraph or voice")
                if self.path == "/api/prefetch":
                    count = payload.get("count", 20)
                    if type(count) is not int or not 1 <= count <= 20:
                        raise ValueError("Prefetch count must be between 1 and 20")
                    if "plan" in payload:
                        plan = payload["plan"]
                        if not isinstance(plan, list) or len(plan) > (count+1)*len(VOICE_IDS):
                            raise ValueError("Invalid prefetch plan")
                        previous = index
                        seen = {(index, voice)}
                        paragraphs = set()
                        for item in plan:
                            if (not isinstance(item, dict) or set(item) != {"index", "voice"}
                                or type(item["index"]) is not int or not previous <= item["index"] < len(book)
                                or not isinstance(item["voice"], str) or item["voice"] not in VOICE_IDS
                                or not has_speech(book[item["index"]]["english"])):
                                raise ValueError("Invalid prefetch plan item")
                            pair = (item["index"], item["voice"])
                            if pair in seen:
                                raise ValueError("Duplicate prefetch plan item")
                            seen.add(pair)
                            if item["index"] > index: paragraphs.add(item["index"])
                            if len(paragraphs) > count:
                                raise ValueError("Prefetch plan exceeds paragraph limit")
                            previous = item["index"]
                        return self._json(store.prefetch_plan(index, voice, plan))
                    return self._json(store.prefetch(index, voice, count))
                self._json(store.request(index, voice))
            except BusyError as error:
                self._json({"message": str(error)}, 429)
            except (ValueError, TypeError) as error:
                self._json({"message": str(error)}, 400)
            except Exception as error:
                logging.exception("POST failed")
                self._json({"message": str(error)}, 500)

    return Handler


def main():
    global BOOK, PORT, SETTINGS
    settings_file = BASE / "Config.json"
    SETTINGS = json.loads(settings_file.read_text("utf-8-sig")) if settings_file.exists() else {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=BOOK, help="Directory containing Aligned.json and English.txt")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--check", action="store_true", help="Check files without starting a server")
    args = parser.parse_args()
    BOOK = args.book.expanduser().resolve()
    PORT = args.port
    if not 1 <= PORT <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    if args.check:
        book = load_book()
        for name in ("Reader.html", "Reader.css", "Reader.js"):
            if not (BASE / name).read_text(encoding="utf-8").strip():
                raise RuntimeError(f"Empty asset: {name}")
        print(f"Startup check passed: {len(book)} aligned bilingual paragraphs; HTML/CSS/JS present")
        return
    book = load_book()
    running = None
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1) as response:
            running = json.load(response)
    except Exception:
        pass
    if running and running.get("app") == APP:
        if running.get("bookId") != book_id(book):
            raise SystemExit("This port serves a different book; stop it or choose another --port")
        print(f"Already running: http://127.0.0.1:{PORT}")
        return
    store = CachedSpeechStore(book, BASE / "Work" / "Speech", SETTINGS)
    repairs_file = BOOK / "Speechrepairs.json"
    if repairs_file.exists():
        store.start_repairs(json.loads(repairs_file.read_text("utf-8-sig")))
    if SETTINGS.get("prepareChapter"):
        start = next((i for i,p in enumerate(book) if re.match(rf"^Chapter {SETTINGS['prepareChapter']}\b",p["english"])), None)
        if start is not None:
            store.anchor = start
            store.monitor.sample()
            store.start_preparation(start, SETTINGS.get("defaultVoice", "furina-local-en"))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(book, store))
        server.daemon_threads = True
        print(f"Reader ready: http://127.0.0.1:{PORT}", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
