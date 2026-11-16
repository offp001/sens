#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import struct
import threading
import urllib.request
import tkinter as tk
from tkinter import messagebox, filedialog

PROC_HINTS = ("sober", "roblox", "com.roblox.client")
FFLAG_PREFIXES = ("FFlag", "DFFlag", "DFInt", "FInt", "FString", "DFString",
                  "FLog", "SFFlag", "SFString", "SFInt", "DFLog")
PTR_MAX = 0x7FFFFFFFFFFF

OFFSET_URL = "https://raw.githubusercontent.com/offp001/sens/refs/heads/main/fflags.hpp"
OFFSET_CACHE = "fflags_cache.hpp"
TO_VALUE_DEFAULT = 0x30

ENTRY_RE = re.compile(r'inline\s+constexpr\s+uintptr_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;')
TO_VALUE_RE = re.compile(r'ToValue\s*=\s*(0x[0-9A-Fa-f]+)')


def say(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def read_file(path, limit=0x10000):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def norm_path(path):
    if not path:
        return ""
    p = path.strip()
    if p.endswith("(deleted)"):
        p = p[: -len("(deleted)")].rstrip()
    return p


def proc_matches(pid):
    comm = read_file("/proc/%d/comm" % pid, 256).decode("utf-8", "ignore").strip()
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        exe = ""
    cmd = read_file("/proc/%d/cmdline" % pid, 8192).replace(b"\x00", b" ").decode("utf-8", "ignore").strip()
    blob = " ".join((comm, exe, cmd)).lower()
    if not any(h in blob for h in PROC_HINTS):
        return None
    score = 0
    if comm == "Main":
        score += 10
    if "sober" in exe.lower():
        score += 5
    return (pid, comm, exe, score)


def find_main_pid():
    me = os.getpid()
    best = None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        info = proc_matches(pid)
        if not info:
            continue
        if best is None or info[3] > best[3]:
            best = info
    return best[0] if best else 0


def read_maps(pid):
    regions = []
    try:
        with open("/proc/%d/maps" % pid, "r", errors="replace") as fh:
            for line in fh:
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                try:
                    lo, hi = parts[0].split("-")
                    start, end = int(lo, 16), int(hi, 16)
                    off = int(parts[2], 16)
                except ValueError:
                    continue
                if end <= start:
                    continue
                path = parts[5].rstrip("\n") if len(parts) > 5 else ""
                regions.append((start, end, parts[1], off, path))
    except OSError:
        pass
    return regions


def find_runtime_base(pid):
    regions = read_maps(pid)
    for start, end, perms, off, path in regions:
        clean = norm_path(path)
        if not clean:
            continue
        if "sober" not in clean.lower():
            continue
        if off == 0:
            return start, clean, regions
    for start, end, perms, off, path in regions:
        clean = norm_path(path)
        if clean and "sober" in clean.lower():
            return start, clean, regions
    return 0, "", regions


class Mem:
    def __init__(self, pid, writable=False):
        mode = os.O_RDWR if writable else os.O_RDONLY
        self.fd = os.open("/proc/%d/mem" % pid, mode)
        self.pid = pid

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def read(self, addr, size):
        if addr <= 0 or addr > PTR_MAX or size <= 0:
            return None
        try:
            return os.pread(self.fd, size, addr)
        except OSError:
            return None

    def write(self, addr, data):
        if addr <= 0 or addr > PTR_MAX or not data:
            return False
        try:
            return os.pwrite(self.fd, data, addr) == len(data)
        except OSError:
            return False


def fetch_offsets(url=OFFSET_URL, cache=OFFSET_CACHE):
    text = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sens-enject/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        try:
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
    except Exception:
        try:
            with open(cache, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = None
    if not text:
        return {}, TO_VALUE_DEFAULT

    to_value = TO_VALUE_DEFAULT
    m = TO_VALUE_RE.search(text)
    if m:
        try:
            to_value = int(m.group(1), 16)
        except ValueError:
            pass

    offsets = {}
    for name, hexval in ENTRY_RE.findall(text):
        if name in ("Pointer", "ToFlag", "ToValue", "ModuleBase"):
            continue
        try:
            offsets[name] = int(hexval, 16)
        except ValueError:
            continue
    return offsets, to_value


def normalize_name(name):
    for p in FFLAG_PREFIXES:
        if name.startswith(p):
            return name
    return name


def strip_prefix_for_lookup(name):
    for p in FFLAG_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def coerce_numeric(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    if isinstance(value, float):
        return int(value) & 0xFFFFFFFF
    s = str(value).strip().lower()
    if s in ("true", "1"):
        return 1
    if s in ("false", "0"):
        return 0
    try:
        return int(s, 0) & 0xFFFFFFFF
    except ValueError:
        return None


def value_field_address(mem, node, to_value):
    val_base = node + to_value
    data = mem.read(val_base, 32)
    if not data or len(data) < 32:
        return 0, 0, 0
    size = struct.unpack("<q", data[8:16])[0]
    cap = struct.unpack("<q", data[16:24])[0]
    if cap > 15:
        ptr = struct.unpack("<Q", data[0:8])[0]
        if ptr == 0 or ptr > PTR_MAX:
            return 0, 0, 0
        return ptr, size, cap
    return val_base + 16, size, cap


def write_flag(mem, node, to_value, value):
    addr, size, cap = value_field_address(mem, node, to_value)
    if not addr:
        return False, "no value field"
    numeric = coerce_numeric(value)
    if numeric is not None:
        if mem.write(addr, struct.pack("<I", numeric)):
            return True, "u32 0x%X = %d" % (addr, numeric)
    text = str(value).lower() if isinstance(value, bool) else str(value)
    payload = text.encode("utf-8") + b"\x00"
    if size >= len(payload):
        if mem.write(addr, payload):
            return True, "str 0x%X = %r" % (addr, text)
    return False, "write failed"


class Injector:
    def __init__(self, log_cb):
        self.log_cb = log_cb
        self.offsets = {}
        self.to_value = TO_VALUE_DEFAULT

    def ensure_offsets(self):
        if self.offsets:
            return True
        self.log_cb("fetching RVA offsets from %s" % OFFSET_URL)
        self.offsets, self.to_value = fetch_offsets()
        self.log_cb("loaded %d entries, ToValue=0x%X" % (len(self.offsets), self.to_value))
        return bool(self.offsets)

    def apply(self, payload, status_cb):
        if not self.ensure_offsets():
            status_cb("offset load failed", "#ff6b6b")
            return 0, 0

        pid = find_main_pid()
        if not pid:
            status_cb("Sober not found", "#ff6b6b")
            return 0, 0

        base, path, regions = find_runtime_base(pid)
        if not base:
            status_cb("runtime base not found", "#ff6b6b")
            return 0, 0
        self.log_cb("pid=%d runtime_base=0x%X module=%s" % (pid, base, path))

        try:
            mem = Mem(pid, writable=True)
        except OSError as e:
            status_cb("cannot open /proc/%d/mem: %s" % (pid, e), "#ff6b6b")
            return 0, 0

        applied = 0
        attempted = 0
        try:
            for raw_name, value in payload.items():
                attempted += 1
                name = normalize_name(str(raw_name))
                rva = self.offsets.get(name)
                if rva is None:
                    short = strip_prefix_for_lookup(name)
                    for p in FFLAG_PREFIXES:
                        cand = p + short
                        if cand in self.offsets:
                            name = cand
                            rva = self.offsets[cand]
                            break
                if rva is None:
                    self.log_cb("skip %s (no offset)" % raw_name)
                    continue

                runtime_addr = base + rva
                if runtime_addr < base or runtime_addr > PTR_MAX:
                    self.log_cb("skip %s (overflow)" % raw_name)
                    continue

                ok, info = write_flag(mem, runtime_addr, self.to_value, value)
                if ok:
                    applied += 1
                    self.log_cb("%s %s" % (name, info))
                else:
                    self.log_cb("fail %s @0x%X (%s)" % (name, runtime_addr, info))
        finally:
            mem.close()

        status_cb("applied %d/%d" % (applied, attempted),
                  "#65d98b" if applied else "#ff6b6b")
        return applied, attempted


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("sens enject (RVA)")
        self.geometry("640x580")
        self.minsize(520, 460)
        self.configure(bg="#18181b")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, bg="#18181b")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 8))
        header.columnconfigure(1, weight=1)

        tk.Label(header, text="sens enject", bg="#18181b", fg="#ffffff",
                 font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(header, text="RVA offsets  |  /proc/pid/mem", bg="#18181b",
                 fg="#888888", font=("Segoe UI", 9)).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.editor = tk.Text(self, bg="#242428", fg="#eeeeee",
                              insertbackground="#ffffff", selectbackground="#5865f2",
                              relief="flat", borderwidth=0, font=("Consolas", 10),
                              wrap="none", padx=8, pady=8)
        self.editor.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 10))
        self.editor.insert("1.0",
                           '{\n'
                           '    "DFIntTaskSchedulerTargetFps": 240,\n'
                           '    "FFlagDebugDisplayFPS": false\n'
                           '}')

        log_frame = tk.Frame(self, bg="#18181b")
        log_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_widget = tk.Text(log_frame, bg="#1e1e21", fg="#aaaaaa",
                                  relief="flat", borderwidth=0, font=("Consolas", 9),
                                  height=7, wrap="word", state="disabled", padx=8, pady=6)
        self.log_widget.grid(row=0, column=0, sticky="nsew")

        bottom = tk.Frame(self, bg="#18181b", height=60)
        bottom.grid(row=3, column=0, sticky="ew", padx=15, pady=(0, 15))
        bottom.grid_propagate(False)
        bottom.columnconfigure(0, weight=1)

        self.status = tk.Label(bottom, text="Waiting for Sober...", bg="#18181b",
                               fg="#999999", font=("Segoe UI", 9))
        self.status.grid(row=0, column=0, sticky="w")

        self.refresh_btn = tk.Button(bottom, text="REFRESH", command=self.refresh_offsets,
                                     bg="#2d2d31", fg="#ffffff", activebackground="#3a3a40",
                                     activeforeground="#ffffff", relief="flat", borderwidth=0,
                                     font=("Segoe UI", 10, "bold"), width=10, height=2, cursor="hand2")
        self.refresh_btn.grid(row=0, column=1, sticky="e", padx=(0, 6))

        self.load_button = tk.Button(bottom, text="LOAD", command=self.load_file,
                                     bg="#2d2d31", fg="#ffffff", activebackground="#3a3a40",
                                     activeforeground="#ffffff", relief="flat", borderwidth=0,
                                     font=("Segoe UI", 10, "bold"), width=8, height=2, cursor="hand2")
        self.load_button.grid(row=0, column=2, sticky="e", padx=(0, 6))

        self.apply_button = tk.Button(bottom, text="APPLY", command=self.apply_flags,
                                      bg="#5865f2", fg="#ffffff", activebackground="#4752c4",
                                      activeforeground="#ffffff", relief="flat", borderwidth=0,
                                      font=("Segoe UI", 10, "bold"), width=12, height=2, cursor="hand2")
        self.apply_button.grid(row=0, column=3, sticky="e")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.injector = Injector(self.log)
        self.after(500, self.refresh)

        threading.Thread(target=self._prefetch, daemon=True).start()

    def _prefetch(self):
        try:
            self.injector.ensure_offsets()
        except Exception as e:
            self.log("offset prefetch failed: %s" % e)

    def log(self, msg):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def refresh(self):
        pid = find_main_pid()
        if pid:
            self.status.config(text="Sober attached  PID %d" % pid, fg="#65d98b")
        else:
            self.status.config(text="Waiting for Sober...", fg="#999999")
        self.after(1000, self.refresh)

    def refresh_offsets(self):
        self.injector.offsets = {}
        self.refresh_btn.config(state="disabled", text="...")

        def worker():
            try:
                ok = self.injector.ensure_offsets()
                self.after(0, lambda: self.status.config(
                    text="offsets reloaded" if ok else "offset load failed",
                    fg="#65d98b" if ok else "#ff6b6b"))
            finally:
                self.after(0, lambda: self.refresh_btn.config(state="normal", text="REFRESH"))

        threading.Thread(target=worker, daemon=True).start()

    def load_file(self):
        path = filedialog.askopenfilename(
            title="Load JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read())
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object")
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", json.dumps(data, indent=4))
            self.log("loaded %s (%d entries)" % (path, len(data)))
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def apply_flags(self):
        try:
            raw = self.editor.get("1.0", "end-1c").strip()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object")
        except (json.JSONDecodeError, ValueError) as e:
            messagebox.showerror("Invalid JSON", str(e))
            return

        self.apply_button.config(state="disabled", text="...")
        self.status.config(text="Working...", fg="#ffcc66")

        def worker():
            try:
                applied, attempted = self.injector.apply(
                    data,
                    lambda msg, color: self.after(0, lambda: self.status.config(text=msg, fg=color)))
                self.log("result: %d/%d" % (applied, attempted))
            except Exception as e:
                self.log("error: %s" % e)
                self.after(0, lambda: self.status.config(text="error", fg="#ff6b6b"))
            finally:
                self.after(0, lambda: self.apply_button.config(state="normal", text="APPLY"))

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        self.destroy()


if __name__ == "__main__":
    if os.name != "posix":
        print("Linux only.")
        sys.exit(1)
    if os.geteuid() != 0:
        say("root required for /proc/<pid>/mem; relaunching with sudo")
        try:
            os.execvp("sudo", ["sudo", "-E", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        except OSError as e:
            say("sudo failed: %s" % e)
            sys.exit(1)
    App().mainloop()