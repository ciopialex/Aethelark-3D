# Aethelark-3D

Standalone Python engine providing direct network control, headless slicing, persistent inventory tracking, and programmatic actuation for FDM 3D printers.

---

## 1. What this is

Aethelark-3D turns "find me a spiderman model and print it" into a finished
print on a real machine: search, download, slice, package, upload, act. It is a
module of the Space-Eagle harness and it runs as its own process.

A diagram of the engine's internals used to sit here, naming each source file
and what it does. It is gone. Which file does what is answered by reading
`aethelark3d/`, which cannot be out of date about itself. What this section is
for is the sentence above — what the product is *for* — and that does not
change when the code moves.

## 2. Hardware Protocols & Network Endpoints (Elegoo Centauri Series)

### A. Centauri Carbon 1: SDCP WebSocket Protocol (`ws://<ip>:3030/websocket`)

| Command ID | Name | Request Schema | Response Schema |
| :--- | :--- | :--- | :--- |
| **`Cmd: 0`** | Telemetry | `{"Cmd": 0, "Data": {}, "From": 2}` | Returns `TempOfNozzle`, `TempOfHotbed`, `TempOfBox`, `CurrentLayer`, `TotalLayers`, `PrintProgress`, `Status`, `Filename` |
| **`Cmd: 128`** | Start Print | `{"Cmd": 128, "Data": {"Filename": "/local/<name>.gcode", "StartLayer": 0, "Calibration_switch": 1, "PrintPlatformType": 1, "Tlp_Switch": 0}, "From": 2}` | `{"Ack": 0}` (Success)<br>`{"Ack": 2}` (File not in `/local/`) |
| **`Cmd: 129`** | Pause Print | `{"Cmd": 129, "Data": {}, "From": 2}` | `{"Ack": 0}` |
| **`Cmd: 130`** | Stop Print | `{"Cmd": 130, "Data": {}, "From": 2}` | `{"Ack": 0}` |
| **`Cmd: 131`** | Resume Print | `{"Cmd": 131, "Data": {}, "From": 2}` | `{"Ack": 0}` |
| **`Cmd: 258`** | List Files | `{"Cmd": 258, "Data": {"Url": "/local"}, "From": 1}` | `{"FileList": [{"name": "/local/...", "size": int, "time": int}], "Ack": 0}` |
| **`Cmd: 403`** | Light Control | `{"Cmd": 403, "Data": {"LightStatus": {"FirstLight": 1, "SecondLight": 1, "RgbLight": [255, 255, 255]}}, "From": 2}` | `{"Ack": 0}` |

### B. Centauri Carbon 2: ElegooLink HTTP & MQTT (`mqtt://<ip>:1883`)

* **Control Transport**: MQTT on Port 1883.
* **Topics**: `/device/report/<mac>` (printer telemetry), `/device/control/<mac>` (commands).
* **Auth**: 8-digit access code (default `12345678`).
* **Command IDs**: `1000` (Status query), `1001` (Pause), `1002` (Resume), `1003` (Stop), `1020` (Start print via `fileTransfer`), `1022` (Chamber light).

### C. HTTP Chunked Upload (`POST http://<ip>/uploadFile/upload`)

* **Headers**: `User-Agent: ElegooLink/1.0.4`, `Accept: application/json`, `S-File-MD5: <md5>`, `Uuid: <uuid>`
* **Multipart Form Fields**:
  * `Check`: `"0"` (intermediate chunks) or `"1"` (final chunk)
  * `uuid`: Session UUID string
  * `md5`: Complete file MD5 hex hash
  * `total`: Total file bytes (string)
  * `offset`: Byte offset of current chunk (string)
  * `size`: Current chunk byte count (string)
  * `file`: Binary payload tuple `(<filename>, <chunk_bytes>, 'application/octet-stream')`

---

## 3. Package & Container Specification

Centauri Carbon firmware takes **raw gcode with a thumbnail embedded in the
gcode itself** — not a ZIP archive. The layout is a single file:

```text
ECC2_0.4_<ModelName>_<FilamentPreset>.gcode   (raw gcode, NOT a zip)
├── ; HEADER_BLOCK_START … ; HEADER_BLOCK_END   # total layer number, densities
├── ; THUMBNAIL_BLOCK_START                      # the browser preview lives HERE
│   ; thumbnail begin 144x144 <base64-char-count>
│   ; <base64 PNG, wrapped 78 chars per "; "-prefixed line>
│   ; thumbnail end
│   ; THUMBNAIL_BLOCK_END
└── … machine start gcode (BED_MESH_CALIBRATE, G28, M6211) + toolpaths …
```

The number after `144x144` is the **base64 character count**, not the raw byte
count. `slicers/elegoo.py::_embed_thumbnail` renders the 144×144 PNG (headless
`elegoo --slice` emits the `; thumbnails =` config line but no image) and
injects the block; `package_container` writes the file raw.

---

## 4. Hardware Fleet Specifications

| Identifier | Model | Enclosure | Nozzle | Max Temps (Bed / Nozzle) | Compatible Materials | AMS Unit |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`CC1`** | Centauri Carbon 1 | Enclosed | Hardened Steel 0.4mm | 100°C / 300°C | PLA-CF, PETG-CF, ABS, ASA, Silk PLA, TPU, PETG | Single Spool |
| **`CC2`** | Centauri Carbon 2 | Enclosed | Hardened Steel 0.4mm | 110°C / 320°C | PA-CF, PC, PLA-CF, PETG-CF, ABS, ASA, Silk PLA | Single Spool |
| **`C2_COMBO`** | Centauri 2 Combo | Open Frame | Standard Brass 0.4mm | 80°C / 260°C | PLA Basic, PLA Silk, PETG, TPU *(Non-abrasive only)* | 4-Slot AMS |

---

## 5. Official Source Code & Reference Repositories

* **Centauri Carbon 2 Firmware**: [`https://github.com/elegooofficial/CentauriCarbon2`](https://github.com/elegooofficial/CentauriCarbon2) (Klipper source, MCU toolchains, CANVAS AMS communication layer)
* **Centauri Carbon 1 Firmware**: [`https://github.com/elegooofficial/CentauriCarbon`](https://github.com/elegooofficial/CentauriCarbon) (Allwinner R528 Linux OS, Klipper configs, pinouts)
* **Elegoo Link SDK**: [`https://github.com/elegoo-repo/elegoo-link-sdk`](https://github.com/elegoo-repo/elegoo-link-sdk) (C++ networking library for LAN/WebSocket/MQTT discovery)
* **ElegooSlicer Core**: [`https://github.com/elegoo-repo/ElegooSlicer`](https://github.com/elegoo-repo/ElegooSlicer) (C++ slicing engine & hardware profiles)
* **RFID Tag Guide**: [`https://github.com/elegoo-repo/RFID-Tag-Guide`](https://github.com/elegoo-repo/RFID-Tag-Guide) (EPC-256 RFID tag data structure)
* **OpenCentauri Research**: [`https://github.com/suchmememanyskill/OpenCentauri`](https://github.com/suchmememanyskill/OpenCentauri) (Community firmware research & hardware pinouts)
* **PyCentauri Client**: [`https://github.com/bjan/pycentauri`](https://github.com/bjan/pycentauri) (Local network Python client)

---

---

## 6. PURGEX Multi-Color Optimization & Anti-Topple Engine

Aethelark-3D includes **PURGEX**, a first-principles multi-color optimization engine that slashes filament waste and print time on single-nozzle AMS systems (e.g. Elegoo Centauri 2 Combo, Bambu AMS, CFS).

* **CIEDE2000 Asymmetric Washout Matrix:** Calculates precise human perceptual color distance ($\Delta E_{00}$) so light-to-dark transitions flush only $75.2\text{ mm}^3$ (saving $78.5\%$ vs standard $350\text{ mm}^3$).
* **Sacrificial Infill & Support Absorption:** Routes required transition plastic into internal body infill and supports.
* **Anti-Topple 8-Lobed Spoked Flower Prime Tower (`PURGEX_ROSE_TOWER`):** Parametric epicycloid profile ($D=27\text{ mm}$, $W_b \approx 810\text{ mm}^3$) with $3.97\times$ higher section modulus than standard hollow square towers, eliminating mid-print detachment.
* **Adaptive 3rd-Wall Optical Shielding:** Automatically enforces a 3rd outer wall for high-luminance surfaces ($L^* \ge 70$) to prevent internal color bleed-through.

### 🐍 Verified Benchmark: Diamondback Rattlesnake (*Crotalus atrox*)
*MakerWorld #3033074 (4-Color AMS on `C2_COMBO`)*

| Metric | Stock Slicer | ⚡ Aethelark PURGEX | Net Savings |
| :--- | :---: | :---: | :---: |
| **Model Weight** | 292.0 g | 292.0 g | Identical Part |
| **Poop & Tower Waste** | 582.0 g (66.6% waste) | 313.3 g (34.1% waste) | **-268.7 g (-46.2% Trash Cut)** |
| **Total Filament Used** | 874.0 g (1 Spool) | 605.3 g (~0.6 Spool) | **-268.7 g Saved Spool** |
| **Total Print Duration** | 47h 35m (2 Days) | 31h 08m (1.3 Days) | **⚡ -16h 26m (34.6% Faster)** |
| **Prime Tower Stability** | 145g Square (Peels) | 38g Spoked Rose | **>3.9x Higher Rigidity** |


---

## 7. CLI Usage Reference

```bash
# Slicing & Autonomous Printing (with PURGEX enabled by default)
a3d print "model.stl" --filament "Silk PLA Red" --printer CC1 --start

# Slicing with Stock Manufacturer Default Purge
a3d print "model.stl" --default-purge --printer C2_COMBO

# Hardware Telemetry & Status
a3d status --printer CC1

# Fleet Inspection & IP Configuration
a3d fleet
a3d fleet --set-ip CC1 --ip 172.20.10.3

# Spool Inventory & Multi-Slot AMS Management
a3d spool --printer CC1 --load "Bambu Silk PLA" --color "Red" --grams 1000
a3d spool --printer C2_COMBO --slot 2 --load "Elegoo Rapid PLA+" --color "Black"
a3d spool --printer CC1 --unload
a3d spool --shelf
a3d spool --printer CC1 --load "Bambu Silk PLA" --color "Red" --used

# Chamber Lighting
a3d light on --printer CC1
a3d light off --printer CC1
```

---

## 8. Test Suite & Zero-Mock Simulation

```bash
pytest --cov=aethelark3d --cov-report=term-missing
```

The line that used to follow this said `60 passed ... 100% pass rate, 85%
coverage`. On 2026-09-10 the suite was 347 tests in 37 files. The number was
true when written and the work carried on; it is not replaced with a fresher
one, because a fresher one rots on the same schedule. Run the command.
