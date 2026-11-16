# sens

A Linux memory dumper for extracting Roblox FFlag offsets from the Sober runtime. It attaches to a running Sober process, discovers FFlag registry structures in memory, and writes a C++ header of module-relative offsets (RVAs) alongside a JSON file of default values.

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
- [Disclaimer](#disclaimer)

---

## Overview

`sens` is a standalone Python script that attaches to a running Sober (Roblox on Linux) process and extracts FFlag information directly from process memory. It does not require any external dependencies beyond the Python standard library.

The tool produces three artifacts:

1. **`fflags.hpp`** — A C++ header with module-relative offsets (RVAs) for each discovered FFlag.
2. **`sober_memory.txt`** — A hex dump of the memory region containing the FFlag structures.
3. **`FFlagValues.json`** — A JSON map of FFlag names to their current string values.

---

## How It Works

The dumper runs through several stages:

### 1. Process Discovery
The script scans `/proc` for candidate processes whose `comm`, `exe`, or `cmdline` contain hints such as `sober`, `roblox`, or `com.roblox.client`. Candidates where the process name is `Main` and the executable path contains `sober` are ranked first.

### 2. Memory Map Reading
`/proc/<pid>/maps` is parsed to build a list of readable/writable regions. The heap region is separated out for focused scanning, and the Sober module base is located as the region whose path contains `sober` and whose file offset is `0`.

### 3. JSON Registry Scan
Writable regions are probed page-by-page for embedded JSON blobs containing FFlag entries. When found, the tool parses name/value pairs from the blob, recording both the value and the in-memory address where the name string begins.

### 4. FFlag Chain Harvesting
The heap is scanned for pointers to strings starting with known FFlag prefixes (`FFlag`, `DFFlag`, `DFInt`, `FInt`, `FString`, `DFString`, `FLog`, `SFFlag`, `SFString`, `SFInt`). Each string reference is resolved back to its owning node by testing three possible holder offsets (`0x10`, `0x0`, `0x8`). Once a valid node is found, both forward and backward linked-list chains are walked to enumerate all sibling flags. Each discovered flag records `node + 0x30` as its value address.

### 5. Memory Dump
A contiguous range is computed from the lowest to highest discovered flag offset. This range is dumped to `sober_memory.txt` with 16 bytes per line in `ADDRESS  HEX  ASCII` format.

### 6. Flag Parsing
The dump is parsed back into memory segments, and each FFlag name is matched with `_NAME_RE` to produce absolute virtual addresses.

### 7. RVA Conversion
The Sober module base captured in step 2 is subtracted from each absolute address, producing a module-relative offset (RVA). RVAs are written to `fflags.hpp`.

---

## Requirements

- **OS:** Linux. Windows is not supported — this targets Sober, which runs on Linux.
- **Python:** 3.8 or newer.
- **Privileges:** root or `sudo`, so `/proc/<pid>/mem` can be read.

No `pip` packages are required. The script imports only from the standard library:

```
argparse, json, os, pwd, re, struct, sys, time, pathlib
```

---

## Installation

```bash
su
git clone https://github.com/offp001/sens.git
cd sens
```

---

## Usage

While Sober is running:

```bash
python3 sens.py
```

The script auto-elevates with `sudo -E` if not already root. It writes `fflags.hpp`, `sober_memory.txt`, and `FFlagValues.json` into the current directory.

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
- `FFlagOffsets::FFlags` — one `inline constexpr uintptr_t` per discovered flag, as an RVA.

Snippet from a generated header:

```cpp
#pragma once

/*
 * Dumped With: sens (RVA mode)
 * Dumper: https://github.com/offp001/sens
 * Module base at dump time: 0x56319BECD000
 * All offsets are RVAs, relative to the Sober module base.
 */

#include <cstdint>
#include <string>

namespace FFlagOffsets {

    namespace FFlagList {
        inline constexpr uintptr_t Pointer = 0x0;
        inline constexpr uintptr_t ToFlag  = 0x0;
        inline constexpr uintptr_t ToValue = 0x30;
    }

    namespace FFlags {
        inline constexpr uintptr_t DFIntTaskSchedulerTargetFps = 0x1489AD5D;
        inline constexpr uintptr_t FFlagDebugDisplayFPS = 0x1489B007;
        // ...
    }

}
```

The `ModuleBase` comment records the base used during the dump. It is informational only; at runtime you always resolve the base yourself and add the RVA.

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

Given a process handle and the module base resolved at runtime:

```cpp
#include "fflags.hpp"

#include <cstdio>
#include <cstring>

// Assume `read_memory(addr, buf, size)` reads from the target process
// and `module_base` is the Sober module base.

const char* GetFlagString(uintptr_t module_base, const char* flag_name) {
    uintptr_t offset = 0;
    if (!std::strcmp(flag_name, "DFIntTaskSchedulerTargetFps"))
        offset = FFlagOffsets::FFlags::DFIntTaskSchedulerTargetFps;
    else if (!std::strcmp(flag_name, "FFlagDebugDisplayFPS"))
        offset = FFlagOffsets::FFlags::FFlagDebugDisplayFPS;
    else
        return nullptr;

    uintptr_t value_addr = 0;
    if (!read_memory(module_base + offset, &value_addr, sizeof(value_addr)))
        return nullptr;

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

void SetFlagString(uintptr_t module_base, uintptr_t flag_offset, const char* value) {
    uintptr_t value_addr = 0;
    read_memory(module_base + flag_offset, &value_addr, sizeof(value_addr));
    if (!value_addr) return;
    write_memory(value_addr, value, std::strlen(value) + 1);
}

// Usage:
// SetFlagString(module_base, FFlagOffsets::FFlags::FFlagDebugDisplayFPS, "True");
```

### Example 3: Enumerating all flags

```cpp
#include "fflags.hpp"

#include <cstdio>

struct FlagEntry { const char* name; uintptr_t offset; };

static const FlagEntry kFlags[] = {
    { "DFIntTaskSchedulerTargetFps", FFlagOffsets::FFlags::DFIntTaskSchedulerTargetFps },
    { "FFlagDebugDisplayFPS",        FFlagOffsets::FFlags::FFlagDebugDisplayFPS },
};

void PrintFlags(uintptr_t module_base) {
    for (const auto& e : kFlags) {
        std::printf("%-40s rva=0x%X runtime=0x%lX\n",
                    e.name, (unsigned)e.offset,
                    (unsigned long)(module_base + e.offset));
    }
}
```

---

## Disclaimer

This software is provided for educational and research purposes only.
