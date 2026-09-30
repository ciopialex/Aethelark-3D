# Working in Aethelark-3D

This is a module, not the harness. The harness is `~/Projects/Space-Eagle`;
its `CLAUDE.md` governs how the two fit together, and
`Space-Eagle/docs/MODULE_CONTRACT.md` is the contract this module is built to.
Do not keep a second copy of that contract here — one used to live in
`docs/MODULE_FACTORY_SPEC.md` and it drifted.

## Core Verification & Execution

- **Read the code, inspect JSON payloads.** Read the keys that come back before believing any description of them.
- **System Python:** There is no `.venv` in this repository; the suite and CLI run on system `python3`.
- **Run the suite:** `python3 -m pytest tests/ -q`
- **CLI inspection:** `a3d --help` | `a3d browse "spiderman" --json`


## Facts that are not derivable from the source

- **Discovery and the CC2 do not speak the same protocol.** Discovery is an
  SDCP v3.0.0 UDP broadcast of `M99999` to port **3030**
  (`aethelark3d/discovery.py`). The Centauri Carbon 2 answers ElegooLink on
  port **80**. A CC2 that is powered on, printing and reachable will therefore
  not appear in a broadcast scan, and that is the protocol, not a bug in the
  scanner. Name the host directly.
- **A CC2 connects with a fixed factory default (`123456`).** Whenever the on-screen "Access Code" toggle is OFF (out-of-box default), the LAN API accepts `123456` as the MQTT credentials and HTTP `X-Token`. The driver (`DEFAULT_CC2_ACCESS_CODE`) connects automatically with zero manual setup unless the operator explicitly configured a custom code.
- **Printable `.gcode` is raw gcode with an embedded thumbnail (NOT a zip archive).** The Centauri Carbon firmware reads the preview, layer count, time, and weight directly from a base64 thumbnail block embedded in the gcode header (`; THUMBNAIL_BLOCK_START` / `; thumbnail begin 144x144 <chars>`, 78 chars per `; `-prefixed line). Headless `elegoo --slice` writes config lines without the image, so `slicers/elegoo.py::_embed_thumbnail` generates and embeds the PNG; `package_container` writes raw gcode.
- **This module declares no `[island]` section in its manifest.** The harness asks `MODULE_BUS.island_face(...)` to determine if an answer is renderable and falls back to reading the card template directly. Under the sovereign Apple Dynamic Island architecture, `a3d` emits pure JSON telemetry and maps components to standard slots (`leading`, `trailing`, `center`, `bottom`).
- **A print is irreversible.** It burns physical filament and machine time. `a3d_print` and `a3d_print_batch` are confirmation-gated and declare `one_at_a_time` in the manifest.

## Where the science and design live

`docs/PURGEX_SCIENTIFIC_FOUNDATIONS.md` and `docs/BIBLIOGRAPHY.md` are the physical derivations and citations for purge volume reduction. Visual and interaction design is governed by `docs/DYNAMIC_ISLAND_3TIER_BLUEPRINT.md` and `../Space-Eagle/docs/APPLE_DYNAMIC_ISLAND_ARCHITECTURE.md`.

## Apple Human Interface Guidelines (HIG) System-Wide Mandate

**All user-facing cards, templates, and HUD surfaces in this module must strictly obey Apple Human Interface Guidelines (HIG):**

1. **Concentric Geometry (`ContainerRelativeShape`):**
   - Every child element within a card or button must respect $R_{\text{inner}} = \max(0, R_{\text{outer}} - P)$.
   - 3D viewport containers, chip buttons, and status badges must have curvature concentric with the outer island card ($R = 32\text{px}$).
2. **Materials & Deep Contrast:**
   - Dark mode materials: OLED pitch-black background, specular upper rim reflection (`1px solid rgba(255, 255, 255, 0.12)`), blur diffusion.
   - Temperature, coordinate, and state badges use low-alpha frosted fills with high-contrast text.
3. **Tabular Numerals & Zero Jitter:**
   - Mandatory `font-variant-numeric: tabular-nums` across all telemetry: temperatures (`215°C / 60°C`), live XYZ coordinates (`X: 120.4, Y: 85.0, Z: 12.2`), print layer progress (`142 / 480`), and remaining print time countdown.
4. **ADR-002: Zero Machine Filenames:**
   - Strip machine gcode prefixes (`ECC2_0.4_...gcode`). The rotating 3D preview and printer alias provide instant visual cognition. Raw filenames remain isolated to Tier 3 diagnostics.
5. **Physical Telemetry Integrity:**
   - Never report synthetic zero temperatures (`0.0°C`) or fabricated progress. If telemetry is offline or unreadable, report exact connection status.

## Active Architectural Blueprints
- **Opus 5.5 Master Mission Briefing & Physics Doctrine:** `../Space-Eagle/docs/OPUS_MISSION_BRIEFING.md`
- **Dynamic Island 3-Tier Progressive Disclosure:** `docs/DYNAMIC_ISLAND_3TIER_BLUEPRINT.md`



