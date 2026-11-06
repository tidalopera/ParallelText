"""Release the external model process on low memory and reload it on demand."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import urlopen

LOW_MEMORY_BYTES = 500_000_000


def available_memory_bytes():
    if os.name != "nt": return None
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [(name, ctypes.c_ulonglong) for name in
            ("total", "available", "pageTotal", "pageAvailable", "virtualTotal", "virtualAvailable", "extended")]
    mem = Memory(); mem.length = ctypes.sizeof(mem)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise OSError("Cannot read available memory")
    return mem.available


def working_set_bytes(pid, private=False):
    if os.name != "nt": return 0
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("size",wintypes.DWORD),("faults",wintypes.DWORD)] + [(name,ctypes.c_size_t) for name in
            ("peakWorkingSet","workingSet","peakPaged","paged","peakNonPaged","nonPaged","pagefile","peakPagefile","private")]
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    function = ctypes.windll.psapi.GetProcessMemoryInfo
    function.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    handle = kernel.OpenProcess(0x1000,False,pid)
    if not handle: return 0
    try:
        counters=Counters(); counters.size=ctypes.sizeof(counters)
        return (counters.private if private else counters.workingSet) if function(handle,ctypes.byref(counters),counters.size) else 0
    finally: kernel.CloseHandle(handle)


class MemoryGuard:
    def __init__(self, settings, stopped, state_path, available=available_memory_bytes,
                 resident=working_set_bytes, execute=None, health=None, clock=time.monotonic):
        self.settings, self.stopped = settings, stopped
        self.state_path=Path(state_path)
        self.available,self.resident,self.clock=available,resident,clock
        self.reservation=resident
        self.execute=execute or self._execute
        self.health=health or self._health
        self.enabled=bool(settings.get("furinaStartScript") and settings.get("furinaStopScript"))
        self.lock=threading.RLock()
        self.retry_after=0
        self.loading_started=0
        self.last_resident=0
        self.status={"state":"resident" if self.enabled else "unmanaged", "released":False,
                     "loading":False,"lowMemoryBytes":LOW_MEMORY_BYTES,"reloadRequiredBytes":LOW_MEMORY_BYTES}
        self.thread=None
        if self.enabled and self.state_path.exists():
            try:
                saved=json.loads(self.state_path.read_text("utf-8"))
                self.last_resident=saved.get("releasedModelBytes",saved.get("modelResidentBytes",0))
                if saved.get("released"):
                    self.status.update(state="released-memory",released=True,
                        reloadRequiredBytes=saved.get("reloadRequiredBytes",self.last_resident+LOW_MEMORY_BYTES+128*1024**2))
                    self.retry_after=self.clock()+30
            except (OSError,ValueError):pass

    def _execute(self, script):
        options={"capture_output":True,"text":True,"timeout":20,"check":True}
        if os.name=="nt": options["creationflags"]=subprocess.CREATE_NO_WINDOW
        subprocess.run(["pwsh.exe","-NoProfile","-NonInteractive","-File",str(Path(script).resolve())],**options)

    def _health(self):
        try:
            with urlopen(self.settings.get("furinaUrl","http://127.0.0.1:9880")+"/api/health",timeout=1) as response:
                return json.load(response)
        except OSError: return None

    def _pid(self):
        try:return int(Path(self.settings["furinaPidFile"]).read_text("utf-8-sig").strip())
        except (KeyError,OSError,ValueError):return None

    def _save(self):
        self.state_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.status,ensure_ascii=False,indent=2),encoding="utf-8")
        temporary.replace(self.state_path)

    def release(self):
        """Stop the entire validated model service so runtime allocations are released too."""
        with self.lock:
            if self.status["released"]:return
            pid=self._pid()
            if pid:self.last_resident=max(self.last_resident,self.reservation(pid))
            self.status["state"]="releasing-memory"
            self.execute(self.settings["furinaStopScript"])
            # A successful script alone is insufficient: verify the old model process exited.
            deadline=time.monotonic()+5
            while pid and self.resident(pid) and time.monotonic()<deadline:time.sleep(.1)
            if pid and self.resident(pid):raise RuntimeError("Model process did not exit after stop")
            minimum=self.last_resident+LOW_MEMORY_BYTES+128*1024**2
            previous=self.status.get("reloadRequiredBytes",LOW_MEMORY_BYTES)
            if self.status["loading"]:
                minimum=max(minimum,int(previous*1.2))
            self.status.update(state="released-memory",released=True,loading=False,
                               releasedModelBytes=self.last_resident,reloadRequiredBytes=minimum)
            self.retry_after=self.clock()+30
            self._save()

    def tick(self):
        if not self.enabled or self.stopped.is_set():return
        free=self.available()
        if free is None:return
        self.status["availableBytes"]=free
        if free < LOW_MEMORY_BYTES:
            self.release()
            return
        with self.lock:
            pid=self._pid()
            resident=self.resident(pid) if pid else 0
            if not resident and not self.status["loading"] and not self.status["released"]:
                self.status.update(state="released-memory",released=True,
                    reloadRequiredBytes=self.last_resident+LOW_MEMORY_BYTES+128*1024**2)
            if self.status["released"]:
                # Reload only with enough room for the previous model plus the 0.5GB reserve.
                if free < self.status["reloadRequiredBytes"] or self.clock()<self.retry_after:return
                self.execute(self.settings["furinaStartScript"])
                self.status.update(state="loading",released=False,loading=True)
                self.loading_started=self.clock()
                self._save()
            pid=self._pid()
            if pid:self.last_resident=max(self.last_resident,self.reservation(pid))
            if self.last_resident > self.status.get("modelResidentBytes",0)+128*1024**2:
                self.status["modelResidentBytes"]=self.last_resident
                self._save()
            if self.status["loading"]:
                health=self.health()
                if health and health.get("ready"):
                    self.status.update(state="resident",loading=False)
                    self._save()
                elif (health and health.get("error")) or self.clock()-self.loading_started>180:
                    self.release()
                    self.status["message"]="Model loading failed; retrying after memory recovery"

    def start(self):
        if not self.enabled:return
        def run():
            while not self.stopped.is_set():
                try:self.tick()
                except Exception as error:
                    self.status.update(state="error",message=type(error).__name__+": "+str(error))
                self.stopped.wait(1)
        self.thread=threading.Thread(target=run,daemon=True,name="ModelMemoryGuard")
        self.thread.start()

    def waiting(self):
        return self.enabled and (self.status["released"] or self.status["loading"] or self.status["state"] in ("releasing-memory","error"))
