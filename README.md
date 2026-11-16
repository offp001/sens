# sens

A Linux memory dumper for extracting Roblox FFlag offsets and values from the Sober runtime. This tool scans a running Sober process, locates FFlag registry structures in memory, and produces a C++ header containing ready-to-use offsets alongside a JSON file of default values.

---

## Table of Contents

- [Overview](#overview)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Command Line Options](#command-line-options)
- [Output Files](#output-files)
- [Using the Generated Header](#using-the-generated-header)
- [Workflow Example](#workflow-example)
- [Troubleshooting](#troubleshooting)
- [Technical Notes](#technical-notes)
- [Disclaimer](#disclaimer)

---

## Overview

`sens` is a standalone Python script that attaches to a running Sober (Roblox on Linux) process and extracts FFlag information directly from process memory. It does not require any external dependencies beyond the Python standard library.

The tool produces three artifacts:

1. **`fflags.hpp`** — A C++ header with compile-time constants for each discovered FFlag offset.
2. **`sober_memory.txt`** — A hex dump of the memory region containing the FFlag structures.
3. **`FFlagValues.json`** — A JSON map of FFlag names to their current string values.

---

## How It Works

The dumper runs through several stages:

### 1. Process Discovery
The script scans `/proc` for candidate processes whose `comm`, `exe`, or `cmdline` contain hints such as `sober`, `roblox`, or `com.roblox.client`. Candidates where the process name is `Main` and the executable path contains `sober` are ranked first.

### 2. Memory Map Reading
`/proc/<pid>/maps` is parsed to build a list of readable/writable regions. The heap region is separated out for focused scanning.

### 3. JSON Registry Scan
Writable regions are probed page-by-page for embedded JSON blobs containing FFlag entries. When found, the tool parses name/value pairs from the blob, recording both the value and the in-memory address where the name string begins.

### 4. FFlag Chain Harvesting
The heap is scanned for pointers to strings starting with known FFlag prefixes (`FFlag`, `DFFlag`, `DFInt`, `FInt`, `FString`, `DFString`, `FLog`, `SFFlag`, `SFString`, `SFInt`). Each string reference is resolved back to its owning node by testing three possible holder offsets (`0x10`, `0x0`, `0x8`). Once a valid node is found, both forward and backward linked-list chains are walked to enumerate all sibling flags. Each discovered flag records `node + 0x30` as its value address.

### 5. Memory Dump
A contiguous range is computed from the lowest to highest discovered flag offset. This range is dumped to `sober_memory.txt` with 16 bytes per line in `ADDRESS  HEX  ASCII` format.

### 6. Flag Parsing
The dump is parsed back into memory segments, and each FFlag name is matched with `_NAME_RE` to produce absolute virtual addresses.

### 7. Code Generation
`fflags.hpp` and `FFlagValues.json` are written with sorted, deduplicated entries.

---

## Requirements

- **OS:** It should be Linux, windows doesnt work, cause this is sober (linux) dumper
- **Python:** 3.8 or newer (uses f-string-free formatting but relies on `pathlib`, `os.pread`)

No `pip` packages are required. The script imports only from the standard library:

```
argparse, json, os, pwd, re, struct, sys, time, pathlib
```

---

## Installation

Clone the repository and place the script somewhere convenient:

```bash
su
git clone https://github.com/offp001/sens.git
cd sens
```

---

## Usage

### Basic invocation

While Sober running:

```bash
python3 sens.py
```

### Listing candidate processes

To see which processes match the discovery hints without performing a full dump:

```bash
python3 sens.py --list
```

Example output:

```
---------
  sens
---------
[01] candidates
     pid 42117    : comm=Main exe=/home/user/.local/share/sober/sober
     pid 41988    : comm=sober-launcher exe=/usr/bin/sober-launcher
```

### Attaching to a specific PID

If multiple candidates exist, or if you want to bypass auto-discovery:

```bash
python3 sens.py --pid 42117
```

### Waiting for Sober to start

If Sober is not yet running, wait up to 60 seconds for it to appear:

```bash
python3 sens.py --wait 60
```

The tool polls once per second and gives up after the timeout.

---

## Command Line Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--pid` | int | `0` | Attach to a specific PID instead of auto-discovering Sober. |
| `--wait` | float | `0.0` | Maximum seconds to wait for Sober to appear. `0` means wait forever. |
| `--list` | flag | off | Print candidate processes and exit. |

Options can be combined. For example, listing candidates then attaching manually:

```bash
python3 sens.py --list
python3 sens.py --pid 42117
```

---

## Output Files

### `fflags.hpp`

A self-contained C++ header. It exposes:

- `FFlagOffsets::FFlagList` — structural constants (`Pointer`, `ToFlag`, `ToValue`).
- `FFlagOffsets::FFlags` — one `inline constexpr uintptr_t` per discovered flag.
- `FFlagOffsets::DefaultValue(const char* name)` — returns the default value for a flag name, or `nullptr` if unknown.

Snippet from a generated header:

```cpp
namespace FFlagOffsets {

    namespace FFlagList {
        inline constexpr uintptr_t Pointer = 0x0;
        inline constexpr uintptr_t ToFlag  = 0x0;
        inline constexpr uintptr_t ToValue = 0x30;
    }

    namespace FFlags {
        inline constexpr uintptr_t DFIntTaskSchedulerTargetFps = 0x7F3A21C0B4A8;
        inline constexpr uintptr_t FFlagTaskSchedulerLimitTargetFpsTo2402 = 0x7F3A21C0B520;
        inline constexpr uintptr_t DFFlagSatPolyPolyContactEnabled2 = 0x7F3A21C0B5A0;
        // ...
    }

    inline const char* DefaultValue(const char* name) {
        if (!std::string(name).compare("DFIntTaskSchedulerTargetFps")) return "60";
        if (!std::string(name).compare("FFlagTaskSchedulerLimitTargetFpsTo2402")) return "False";
        if (!std::string(name).compare("DFFlagSatPolyPolyContactEnabled2")) return "True";
        return nullptr;
    }

}
```

### `sober_memory.txt`

A plain-text hex dump of the memory span covering all discovered flag offsets, in the format:

```
AAAAAAAAAAAAAAAA  bb bb bb bb bb bb bb bb bb bb bb bb bb bb bb bb  ascii...........
```

Example:

```
00007F3A21C0B4A0  44 46 49 6e 74 54 61 73 6b 53 63 68 65 64 75 6c  DFIntTaskSchedul
00007F3A21C0B4B0  65 72 54 61 72 67 65 74 46 70 73 00 00 00 00 00 00  erTargetFps.....
00007F3A21C0B4C0  3c 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  <...............
```

This file is useful for manual inspection or for re-parsing later.

### `FFlagValues.json`

A sorted JSON object of flag name to default value:

```json
{
  "DFIntTaskSchedulerTargetFps": "60",
  "FFlagDebugDisplayFPS": "False",
  "FFlagUserShowCoreGuiEnabled": "True"
}
```

Flags with no captured value are omitted from the JSON.

---

## Using the Generated Header

### Example 1: Reading a flag value in-process

Given a process handle or base pointer, you can resolve the current value of a flag at runtime:

```cpp
#include "fflags.hpp"

#include <cstdio>
#include <cstring>

// Assume `read_memory(addr, buf, size)` reads from the target process
// and `module_base` is the base address of the Sober module.

const char* GetFlagString(uintptr_t module_base, const char* flag_name) {
    // Resolve the static offset for the named flag.
    uintptr_t offset = 0;
    if (!std::strcmp(flag_name, "DFIntTaskSchedulerTargetFps"))
        offset = FFlagOffsets::FFlags::DFIntTaskSchedulerTargetFps;
    else if (!std::strcmp(flag_name, "FFlagDebugDisplayFPS"))
        offset = FFlagOffsets::FFlags::FFlagDebugDisplayFPS;
    else
        return nullptr;

    // Read the value pointer stored at the flag offset.
    uintptr_t value_addr = 0;
    if (!read_memory(module_base + offset, &value_addr, sizeof(value_addr)))
        return nullptr;

    // Read the string at value_addr.
    static char buffer[256] = {0};
    if (!read_memory(value_addr, buffer, sizeof(buffer) - 1))
        return nullptr;
    return buffer;
}
```

### Example 2: Overwriting a flag value

```cpp
#include "fflags.hpp"

#include <cstring>

void SetFlagBool(uintptr_t module_base, uintptr_t flag_offset, const char* value) {
    // Assume `write_memory(addr, buf, size)` writes into the target process.
    uintptr_t value_addr = 0;
    read_memory(module_base + flag_offset, &value_addr, sizeof(value_addr));
    if (!value_addr) return;

    // Write a null-terminated string. Length must fit the target allocation.
    write_memory(value_addr, value, std::strlen(value) + 1);
}

// Usage:
// SetFlagBool(module_base, FFlagOffsets::FFlags::FFlagDebugDisplayFPS, "True");
```

### Example 3: Enumerating all flags

```cpp
#include "fflags.hpp"

#include <cstdio>
#include <initializer_list>

// Define a table of flag names and offsets.
struct FlagEntry { const char* name; uintptr_t offset; };

static const FlagEntry kFlags[] = {
    { "DFIntTaskSchedulerTargetFps", FFlagOffsets::FFlags::DFIntTaskSchedulerTargetFps },
    { "FFlagDebugDisplayFPS",        FFlagOffsets::FFlags::FFlagDebugDisplayFPS },
    { "FFlagUserShowCoreGuiEnabled", FFlagOffsets::FFlags::FFlagUserShowCoreGuiEnabled },
    // ... add more as desired
};

void DumpAllFlags(uintptr_t module_base) {
    for (const auto& entry : kFlags) {
        const char* def = FFlagOffsets::DefaultValue(entry.name);
        std::printf("%-40s offset=0x%X default=%s\n",
                    entry.name, (unsigned)entry.offset, def ? def : "(unknown)");
    }
}
```

### Example 4: Setting all boolean-looking flags

```cpp
#include "fflags.hpp"
#include <cstring>
#include <vector>

void OverrideBooleans(uintptr_t module_base, bool new_value) {
    const char* text = new_value ? "True" : "False";
    for (const auto& e : kFlags) {
        const char* def = FFlagOffsets::DefaultValue(e.name);
        if (!def) continue;
        if (std::strcmp(def, "True") && std::strcmp(def, "False")) continue;
        SetFlagBool(module_base, e.offset, text);
    }
}
```

---

## Workflow Example

A complete run from a fresh shell:

```bash
# 1. Start Sober normally.
$ ./sober

# 2. In another terminal, list candidates.
$ python3 sens.py --list
---------
  sens
---------
[01] candidates
     pid 42117    : comm=Main exe=/home/user/.local/share/sober/sober

# 3. Run the dumper. It will sudo itself.
$ python3 sens.py
---------
  sens
---------
[01] searching for Sober
     pid          : 42117
     comm         : Main
     exe          : /home/user/.local/share/sober/sober
[02] reading maps
[03] scanning JSON flag registry
[04] harvesting fflag
[05] dumping fflag memory
[06] parsing flags
     parsed flags : 1284
[07] writing fflags.hpp

done - 1284 flags

# 4. Inspect the results.
$ head -30 fflags.hpp
$ python3 -c "import json; d=json.load(open('FFlagValues.json')); print(len(d), 'values')"
1284 values
```

---

## Troubleshooting

### `cannot open /proc/<pid>/mem`

Root access is required. Confirm `sudo -E` works and that Yama's `ptrace_scope` is not blocking reads:

```bash
cat /proc/sys/kernel/yama/ptrace_scope
# 0 or 1 typically; 2 or 3 will block even root in some configs.
```

If the value is `2` or higher, temporarily relax it:

```bash
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

### `no fflags found`

Possible causes:

- The Sober process is still loading. Add `--wait 30`.
- The JSON registry scan failed because flags are stored lazily. Try opening the Roblox settings menu once, then re-run.
- Multiple candidate processes exist. Use `--list` and pass `--pid` manually.

### Offsets change every launch

FFlag addresses are ASLR-relative. Treat the generated offsets as **relative to the Sober module base**, not absolute addresses. Always compute the runtime address as `module_base + offset`.

### Header fails to compile

Ensure you are compiling with C++17 or later. The header uses `inline constexpr` members and `<cstdint>`, both of which require C++17.

---

## Technical Notes

### Structure layout assumptions

The tool assumes the following node layout for a flag:

```
+0x00  <prev pointer>     (linked list backward)
+0x08  <next pointer>     (linked list forward)
+0x10  <name string node> (std::string-like: ptr, size, capacity or SSO)
+0x30  <value string node> (same layout)
```

The `Mem::text` helper decodes both long-string (pointer + length) and SSO (inline buffer at `+0x10`) representations.

### Why two discovery paths?

The JSON scan is fast and captures the current default value. The linked-list harvest is slower but reaches flags that may not yet appear in the registry (lazily instantiated ones). Both are merged into `base_flags`, with chain-derived offsets taking precedence.

### Memory dump purpose

Dumping the memory span serves two purposes:

1. It provides a stable, human-readable record for debugging.
2. `parse_flags_from_memory` re-derives offsets from the dump, acting as a consistency check on the earlier harvest.

### Performance

On a typical Sober session with ~50 MB of heap, the scan completes in under 20 seconds. The dominant cost is the bytewise pointer scan in `collect_holders`.

---

## Disclaimer

This software is provided for educational and research purposes only. Extracting offsets from a live process and modifying in-memory state may violate the terms of service of the target application. Use at your own risk. The authors assume no responsibility for account bans, data loss, or any other consequences arising from use of this tool.