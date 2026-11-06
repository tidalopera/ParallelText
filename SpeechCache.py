"""Persistent bounded audio cache, local speech client and background admission."""
from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.request import Request, urlopen
import wave


class DiskCache:
    def __init__(self, directory, limit=1_000_000_000):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            meta = self.directory / (key + ".json")
            try:
                data = json.loads(meta.read_text("utf-8"))
                if data["format"] not in ("mp3", "wav"):
                    return None
                audio = self.directory / (key + "." + data["format"])
                if not audio.is_file() or audio.stat().st_size != data["bytes"]:
                    return None
                os.utime(meta, None)
                return data
            except (OSError, ValueError, KeyError):
                return None

    def rows(self):
        rows = []
        for meta in self.directory.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{24}", meta.stem):
                continue
            try:
                data = json.loads(meta.read_text("utf-8"))
                audio = self.directory / (meta.stem + "." + data["format"])
                rows.append((meta.stat().st_mtime, meta, audio, data))
            except (OSError, ValueError, KeyError):
                continue
        return rows

    def stats(self):
        with self.lock:
            rows = self.rows()
            return {"bytes": sum(a.stat().st_size+m.stat().st_size for _, m, a, _ in rows if a.exists()),
                    "files": len(rows), "limitBytes": self.limit}

    def prune(self, protected=None, room=0, before=None, book=None):
        with self.lock:
            rows = sorted(self.rows(), key=lambda row: row[0])
            total = self.stats()["bytes"]
            for _, meta, audio, data in rows:
                if total + room <= self.limit:
                    break
                if meta.stem == protected:
                    continue
                if before is not None and (data.get("book") != book or data.get("index", before) >= before):
                    continue
                size = meta.stat().st_size + (audio.stat().st_size if audio.exists() else 0)
                audio.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                total -= size

    def put(self, key, data, protected=None):
        with self.lock:
            audio = self.directory / (key + "." + data["format"])
            data = {**data, "bytes": audio.stat().st_size}
            encoded = json.dumps(data, ensure_ascii=False)
            if data["bytes"]+len(encoded.encode("utf-8")) > self.limit:
                audio.unlink(missing_ok=True)
                raise ValueError("A single audio file exceeds the cache budget")
            temp = self.directory / (key + ".json.tmp")
            temp.write_text(encoded, encoding="utf-8")
            temp.replace(self.directory / (key + ".json"))
            self.prune(protected)


class ResourceMonitor:
    def __init__(self, pid_file=None):
        self.pid_file = Path(pid_file) if pid_file else None
        self.previous = None
        self.updated = 0
        self.gpu_updated = 0
        self.state = {"busy": False, "reason": "initializing"}

    def sample(self):
        if time.monotonic()-self.updated < 3:
            return self.state
        cpu, free, gpu = 0.0, None, self.state.get("otherGpuPercent")
        if os.name == "nt":
            warming = self.previous is None
            from ctypes import wintypes
            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            values = [x.dwLowDateTime+(x.dwHighDateTime << 32) for x in (idle, kernel, user)]
            if self.previous:
                d = [a-b for a, b in zip(values, self.previous)]
                cpu = max(0, min(100, 100*(1-d[0]/max(1,d[1]+d[2]))))
            self.previous = values
            class Memory(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [(name, ctypes.c_ulonglong) for name in
                    ("total", "available", "pageTotal", "pageAvailable", "virtualTotal", "virtualAvailable", "extended")]
            mem = Memory(); mem.length = ctypes.sizeof(mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            free = mem.available/1024**3
            if time.monotonic()-self.gpu_updated > 15:
                try:
                    ignored = {os.getpid()}
                    if self.pid_file and self.pid_file.exists():
                        ignored.add(int(self.pid_file.read_text("utf-8-sig").strip()))
                    script = "Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine | Select-Object Name,UtilizationPercentage | ConvertTo-Json -Compress"
                    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                        capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
                    rows = json.loads(result.stdout) if result.stdout.strip() else []
                    if isinstance(rows, dict): rows = [rows]
                    gpu = min(100, sum(float(row["UtilizationPercentage"]) for row in rows
                        if re.search(r"engtype_(?:3D|Compute|Video)", row["Name"], re.I)
                        and int(re.search(r"pid_(\d+)", row["Name"])[1]) not in ignored))
                except Exception:
                    gpu = None
                self.gpu_updated = time.monotonic()
        elif hasattr(os, "getloadavg"):
            cpu = min(100, os.getloadavg()[0]/max(1,os.cpu_count())*100)
        reasons = []
        if os.name == "nt" and warming: reasons.append("measuring CPU load")
        if cpu >= 65: reasons.append("CPU busy")
        if free is not None and free < 4: reasons.append("less than 4 GiB memory available")
        if gpu is not None and gpu >= 50: reasons.append("other GPU workloads busy")
        self.state = {"busy": bool(reasons), "reason": "; ".join(reasons), "cpuPercent": round(cpu,1),
                      "availableGiB": round(free,2) if free is not None else None, "otherGpuPercent": gpu}
        self.updated = time.monotonic()
        return self.state


class FurinaClient:
    def __init__(self, url, monitor, stopped):
        self.url = url.rstrip("/")
        self.monitor, self.stopped = monitor, stopped
        self.condition = threading.Condition()
        self.busy = False
        self.foreground_waiters = 0

    @contextmanager
    def admission(self, background):
        while self.monitor.sample()["busy"]:
            if self.stopped.wait(2): raise RuntimeError("Speech stopped")
        with self.condition:
            if not background: self.foreground_waiters += 1
            try:
                while self.busy or (background and self.foreground_waiters):
                    if self.stopped.is_set(): raise RuntimeError("Speech stopped")
                    self.condition.wait(.3)
                if self.stopped.is_set(): raise RuntimeError("Speech stopped")
                self.busy = True
            finally:
                if not background: self.foreground_waiters -= 1
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    @staticmethod
    def split(text, limit=450):
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text); break
            at = max(text.rfind(mark, 0, limit) for mark in (". ", "! ", "? ", "; ", ", ", " "))
            at = at+1 if at > 0 else limit
            chunks.append(text[:at]); text = text[at:]
        return [chunk.strip() for chunk in chunks if chunk.strip()]

    def generate(self, text, path, background=False):
        parts = []
        started = time.monotonic()
        synthesis_seconds = 0.0
        for chunk in self.split(text):
            with self.admission(background):
                request = Request(self.url+"/api/speech", data=json.dumps({"text":chunk,"language":"en","transient":True}).encode(),
                                  headers={"Content-Type":"application/json"})
                with urlopen(request, timeout=600) as response:
                    data = response.read()
                    synthesis_seconds += float(response.headers.get("X-Synthesis-Seconds", "0"))
                    if "json" in response.headers.get("Content-Type", ""):
                        raise RuntimeError("Furina service needs transient WAV support; restart the updated service")
                with wave.open(io.BytesIO(data)) as audio:
                    params = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate())
                    if audio.getnframes() == 0: raise ValueError("Empty Furina audio")
                    parts.append((params, audio.readframes(audio.getnframes())))
        if not parts: raise ValueError("No speech content")
        if any(params != parts[0][0] for params, _ in parts): raise ValueError("Inconsistent WAV formats")
        temp = path.with_suffix(".wav.tmp")
        with wave.open(str(temp), "wb") as out:
            out.setnchannels(parts[0][0][0]); out.setsampwidth(parts[0][0][1]); out.setframerate(parts[0][0][2])
            for _, data in parts: out.writeframes(data)
        temp.replace(path)
        duration = sum(len(data) for _,data in parts)/(parts[0][0][0]*parts[0][0][1]*parts[0][0][2])
        return {"generationSeconds":synthesis_seconds, "audioSeconds":duration, "wallSeconds":time.monotonic()-started}
