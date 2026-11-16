#!/usr/bin/env python3
import argparse
import json
import os
import pwd
import re
import struct
import sys
import time
from pathlib import Path

PAGE = 0x1000
PTR_MIN = 0x1000
PTR_MAX = 0x7FFFFFFFFFFF
FFLAG_PREFIXES = ("FFlag", "DFFlag", "DFInt", "FInt", "FString", "DFString",
                  "FLog", "SFFlag", "SFString", "SFInt")
PROC_HINTS = ("sober", "roblox", "com.roblox.client")

OUT_HPP = "fflags.hpp"
OUT_MEM = "sober_memory.txt"
OUT_VALUES = "FFlagValues.json"


def say(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def banner(title):
    line = "-" * (len(title) + 4)
    say(line)
    say("  " + title)
    say(line)


def step(n, msg):
    say("[%02d] %s" % (n, msg))


def kv(key, val):
    say("     %-16s : %s" % (key, val))


def slug(raw):
    return re.sub(r'[^a-zA-Z0-9]', '_', raw)


def sane(v):
    return PTR_MIN <= v <= PTR_MAX


def elevate():
    if os.geteuid() == 0:
        return
    os.execvp("sudo", ["sudo", "-E", sys.executable,
                       os.path.abspath(__file__)] + sys.argv[1:])


def give_back(*paths):
    user = os.environ.get("SUDO_USER")
    if not user:
        return
    try:
        info = pwd.getpwnam(user)
    except KeyError:
        return
    for path in paths:
        try:
            os.chown(path, info.pw_uid, info.pw_gid)
        except OSError:
            pass


def read_file(path, limit=0x10000):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def proc_info(pid):
    comm = read_file("/proc/%d/comm" % pid, 256).decode("utf-8", "ignore").strip()
    exe = ""
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        pass
    cmd = read_file("/proc/%d/cmdline" % pid, 8192).replace(b"\x00", b" ").decode(
        "utf-8", "ignore").strip()
    if not (comm or exe or cmd):
        return None
    return comm, exe, cmd


def norm_path(path):
    if not path:
        return ""
    p = path.strip()
    if p.endswith("(deleted)"):
        p = p[: -len("(deleted)")].rstrip()
    return p


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


def find_main():
    me = os.getpid()
    cands = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        info = proc_info(pid)
        if not info:
            continue
        comm, exe, cmd = info
        blob = " ".join((comm, exe, cmd)).lower()
        if not any(h in blob for h in PROC_HINTS):
            continue
        cands.append((pid, comm, exe, cmd))

    def rank(c):
        return 1 if (c[1] == "Main" and "sober" in (c[2] or "").lower()) else 0

    cands.sort(key=rank, reverse=True)
    return cands


def module_base(pid, regions):
    for start, end, perms, off, path in regions:
        if not path:
            continue
        clean = norm_path(path)
        if "sober" not in clean.lower():
            continue
        if off == 0:
            return start, clean
    for start, end, perms, off, path in regions:
        if path and "sober" in norm_path(path).lower():
            return start, norm_path(path)
    return 0, ""


class Mem:
    def __init__(self, pid):
        self.fd = os.open("/proc/%d/mem" % pid, os.O_RDONLY)

    def read(self, addr, size):
        if addr <= 0 or addr > PTR_MAX:
            return None
        buf = bytearray()
        while len(buf) < size:
            try:
                piece = os.pread(self.fd, size - len(buf), addr + len(buf))
            except OSError:
                break
            if not piece:
                break
            buf += piece
        return bytes(buf) if buf else None

    def page(self, addr):
        try:
            return os.pread(self.fd, PAGE, addr)
        except OSError:
            return b""

    def q8(self, addr):
        raw = self.read(addr, 8)
        return struct.unpack("<Q", raw)[0] if raw and len(raw) == 8 else 0

    def s32(self, addr):
        raw = self.read(addr, 4)
        return struct.unpack("<i", raw)[0] if raw and len(raw) == 4 else 0

    def text(self, addr):
        try:
            ln = self.s32(addr + 0x08)
            if ln <= 0 or ln > 4000:
                return ""
            src = self.q8(addr) if ln >= 16 else addr + 0x10
            if not src:
                return ""
            raw = self.read(src, ln)
            if not raw:
                return ""
            return raw.decode("utf-8", errors="ignore").split("\x00")[0]
        except Exception:
            return ""


def is_flag_name(name):
    if not name or len(name) > 400:
        return False
    return name.startswith(FFLAG_PREFIXES)


def collect_holders(mem, regions):
    str_addrs = set()
    needles = [p.encode() for p in FFLAG_PREFIXES]
    for start, end, perms, _off, _path in regions:
        if "r" not in perms:
            continue
        addr = start
        while addr < end:
            d = mem.page(addr)
            if d:
                for needle in needles:
                    i = 0
                    while True:
                        j = d.find(needle, i)
                        if j < 0:
                            break
                        str_addrs.add(addr + j)
                        i = j + 1
            addr += PAGE
    holders = set()
    if not str_addrs:
        return holders
    for start, end, perms, _off, _path in regions:
        if "r" not in perms:
            continue
        addr = start
        while addr < end:
            d = mem.page(addr)
            if d and len(d) >= 8:
                for k in range(0, len(d) - 7, 8):
                    q = struct.unpack("<Q", d[k:k+8])[0]
                    if q in str_addrs:
                        holders.add(addr + k)
            addr += PAGE
    return holders


def node_from_holder(mem, holder):
    for shift in (0x10, 0x0, 0x8):
        node = holder - shift
        name = mem.text(node + 0x10)
        if is_flag_name(name):
            return node, name
    return 0, ""


def walk_chain(mem, start_node, out):
    for direction in (0x00, 0x08):
        cur = mem.q8(start_node + direction)
        seen = {start_node}
        for _ in range(4_000_000):
            if not cur or cur in seen:
                break
            seen.add(cur)
            name = mem.text(cur + 0x10)
            if is_flag_name(name) and name not in out:
                out[name] = cur + 0x30
            nxt = mem.q8(cur + direction)
            if not sane(nxt) or nxt == cur:
                break
            cur = nxt


def harvest(mem, holders):
    out = {}
    starts = []
    for h in holders:
        node, name = node_from_holder(mem, h)
        if node:
            if name not in out:
                out[name] = node + 0x30
            starts.append(node)
    seen_starts = set(starts)
    for _ in range(10):
        new_starts = []
        for node in starts:
            walk_chain(mem, node, out)
        for va in list(out.values()):
            node = va - 0x30
            if node not in seen_starts:
                seen_starts.add(node)
                new_starts.append(node)
        if not new_starts:
            break
        starts = new_starts
    return out


_NAME_RE = re.compile(rb'((?:FFlag|DFFlag|DFInt|FInt|FString|DFString|FLog|SFFlag|SFString|SFInt)[A-Za-z0-9_]+)')
_VAL_RE = re.compile(rb'"((?:True|False|[0-9]+(?:\.[0-9]+)?|0x[0-9A-Fa-f]+|(?:[!-~](?:[^"\\]|\\.)*)))"')


def parse_json_blob(mem, start, end, values, offsets):
    total = end - start
    if total <= 0:
        return 0
    buf = bytearray()
    addr = start
    while addr < end:
        d = mem.page(addr)
        if not d:
            addr += PAGE
            continue
        buf += d[:min(PAGE, end - addr)]
        addr += PAGE
    data = bytes(buf)
    found = 0
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b'"', i)
        if j < 0:
            break
        m = _NAME_RE.match(data, j + 1)
        if not m:
            i = j + 1
            continue
        name = m.group(1).decode("utf-8", "ignore")
        k = m.end()
        if k + 1 < n and data[k:k+1] == b'"' and data[k+1:k+2] == b':':
            vstart = k + 2
            if data[vstart:vstart+1] == b'"':
                vm = _VAL_RE.match(data, vstart)
                if vm:
                    value = vm.group(1).decode("utf-8", "ignore")
                    if name not in values:
                        values[name] = value
                        offsets[name] = start + j
                        found += 1
                    i = vm.end()
                    continue
        i = j + 1
    return found


def find_json_blob_regions(mem, regions):
    needles = [b'"' + p.encode() for p in FFLAG_PREFIXES]
    out = []
    for start, end, perms, _off, pathname in regions:
        if "w" not in perms:
            continue
        hit = 0
        for probe in (start, start + PAGE, start + 2 * PAGE, max(start, end - PAGE)):
            if probe >= end:
                continue
            d = mem.page(probe)
            if not d:
                continue
            for needle in needles:
                if needle in d and b'":"' in d:
                    hit += 1
                    break
        if hit:
            out.append((start, end, perms, pathname))
    return out


def scan_json_flags(mem, regions):
    values = {}
    offsets = {}
    blob_regions = find_json_blob_regions(mem, regions)
    for start, end, perms, pathname in blob_regions:
        parse_json_blob(mem, start, end, values, offsets)
    return values, offsets


def fflag_dump_regions(flags):
    if not flags:
        return 0, 0
    addrs = sorted(flags.values())
    lo = (addrs[0] - PAGE) & ~(PAGE - 1)
    hi = (addrs[-1] + PAGE) & ~(PAGE - 1)
    return lo, hi


def dump_memory_from_fflags(mem, flags):
    if not flags:
        return
    lo, hi = fflag_dump_regions(flags)
    lo = max(0, lo)
    out_path = Path(OUT_MEM)
    with out_path.open("w", encoding="utf-8", errors="replace") as fh:
        addr = lo
        while addr < hi:
            d = mem.page(addr)
            if not d:
                addr += PAGE
                continue
            chunk = d[:min(PAGE, hi - addr)]
            for i in range(0, len(chunk), 16):
                line = chunk[i:i+16]
                hexpart = " ".join("%02x" % b for b in line)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in line)
                fh.write("%016X  %-48s  %s\n" % (addr + i, hexpart, asc))
            addr += PAGE


def parse_flags_from_memory(path):
    flags = {}
    segments = []
    current_base = None
    current = bytearray()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue

            parts = line.rstrip("\n").split("  ", 2)
            if len(parts) != 3:
                continue

            address_text = parts[0].strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{16}", address_text):
                continue

            try:
                address = int(address_text, 16)
                raw_bytes = bytes(
                    int(value, 16)
                    for value in parts[1].split()
                    if re.fullmatch(r"[0-9A-Fa-f]{2}", value)
                )
            except ValueError:
                continue

            if not raw_bytes:
                continue

            if current_base is None:
                current_base = address
                current = bytearray(raw_bytes)
            elif address == current_base + len(current):
                current.extend(raw_bytes)
            else:
                segments.append((current_base, bytes(current)))
                current_base = address
                current = bytearray(raw_bytes)

    if current_base is not None and current:
        segments.append((current_base, bytes(current)))

    for base, data in segments:
        for match in _NAME_RE.finditer(data):
            try:
                name = match.group(1).decode("ascii")
            except UnicodeDecodeError:
                continue
            if len(name) > 3 and name not in flags:
                flags[name] = base + match.start(1)

    return flags


def emit_hpp(pid, flags, values, base):
    lines = []
    lines.append("#pragma once")
    lines.append("")
    lines.append("/*")
    lines.append(" * Dumped With: sens (RVA mode)")
    lines.append(" * Dumper: https://github.com/offp001/sens")
    lines.append(" * Module base: 0x%X" % base)
    lines.append(" * All offsets are RVAs")
    lines.append(" */")
    lines.append("")
    lines.append("#include <cstdint>")
    lines.append("#include <string>")
    lines.append("")
    lines.append("namespace FFlagOffsets {")
    lines.append("")
    lines.append("    namespace FFlagList {")
    lines.append("        inline constexpr uintptr_t Pointer = 0x0;")
    lines.append("        inline constexpr uintptr_t ToFlag  = 0x0;")
    lines.append("        inline constexpr uintptr_t ToValue = 0x30;")
    lines.append("    }")
    lines.append("")
    lines.append("    namespace FFlags {")
    for name in sorted(flags):
        rva = flags[name] - base
        lines.append("        inline constexpr uintptr_t %s = 0x%X;" % (slug(name), rva))
    lines.append("    }")
    lines.append("")
    lines.append("    inline const char* DefaultValue(const char* name) {")
    for name in sorted(flags):
        if name not in values or values[name] == "":
            continue
        val = values[name]
        val_esc = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append('        if (!std::string(name).compare("%s")) return "%s";' % (name, val_esc))
    lines.append("        return nullptr;")
    lines.append("    }")
    lines.append("")
    lines.append("}")

    Path(OUT_HPP).write_text("\n".join(lines) + "\n", encoding="utf-8")


def emit_values(values):
    Path(OUT_VALUES).write_text(json.dumps(dict(sorted(values.items())),
                                          indent=2, ensure_ascii=False),
                                encoding="utf-8")


def boot():
    ap = argparse.ArgumentParser(description="sens (RVA mode)")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--wait", type=float, default=0.0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    banner("sens (RVA mode)")

    if args.list:
        step(1, "candidates")
        for pid, comm, exe, cmd in find_main():
            kv("pid %d" % pid, "comm=%s exe=%s" % (comm or "-", exe or "-"))
        return 0

    step(1, "searching for Sober")
    start = time.time()
    announced = False
    pid = 0
    comm = ""
    exe = ""
    if args.pid:
        pid = args.pid
        info = proc_info(pid)
        if info:
            comm, exe, _ = info
    else:
        while True:
            cands = find_main()
            if cands:
                pid, comm, exe, _ = cands[0]
                for p, c, e, _cmd in cands:
                    if c == "Main" and "sober" in (e or "").lower():
                        pid, comm, exe = p, c, e
                        break
                break
            if not announced:
                say("     waiting for Sober...")
                announced = True
            if args.wait and (time.time() - start) >= args.wait:
                say("     Sober not found")
                return 1
            time.sleep(1.0)

    kv("pid", pid)
    kv("comm", comm or "-")
    kv("exe", exe or "-")

    try:
        mem = Mem(pid)
    except OSError as err:
        say("     cannot open /proc/%d/mem: %s" % (pid, err))
        return 1

    step(2, "reading maps")
    regions = read_maps(pid)
    if not regions:
        say("empty maps")
        return 1

    base, base_path = module_base(pid, regions)
    if not base:
        say("     module base not found")
        return 1
    kv("module base", "0x%X (%s)" % (base, base_path))

    heap = [r for r in regions if norm_path(r[4]) == "[heap]"]

    step(3, "scanning JSON flag registry")
    values, json_offsets = scan_json_flags(mem, regions)

    step(4, "harvesting fflag")
    holders = collect_holders(mem, heap) if heap else set()
    chain_flags = harvest(mem, holders) if holders else {}

    base_flags = {}
    for name, off in chain_flags.items():
        base_flags[name] = off
    for name, off in json_offsets.items():
        if name not in base_flags:
            base_flags[name] = off

    if not base_flags:
        say("no fflags found")
        return 1

    step(5, "dumping fflag memory")
    dump_memory_from_fflags(mem, base_flags)
    give_back(OUT_MEM)

    step(6, "parsing flags")
    flags = parse_flags_from_memory(OUT_MEM)
    kv("parsed flags", len(flags))

    step(7, "writing fflags.hpp (RVA)")
    emit_hpp(pid, flags, values, base)
    emit_values(values)
    give_back(OUT_HPP, OUT_VALUES)

    say("")
    say("done - %d flags (RVA, base=0x%X)" % (len(flags), base))
    return 0


if __name__ == "__main__":
    try:
        elevate()
        sys.exit(boot())
    except KeyboardInterrupt:
        say("")
        say("cancelled")
        sys.exit(130)
