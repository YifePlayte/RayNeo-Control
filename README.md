# RayNeo Control

A [Decky Loader](https://decky.xyz) plugin that controls RayNeo (雷鸟 / FFalcon) XR
glasses from the Steam Deck — no phone app required.

> ## ⚠️ Scope: RayNeo GT Max only
>
> **This plugin was developed and tested against the RayNeo GT Max (雷鸟 GT Max)
> and nothing else.**
>
> The protocol is shared across the family, so other RayNeo / FFalcon models may
> well work — but nothing here has been verified on them, the capability gating
> has only ever been exercised against one unit's bitmap, and the brightness
> table is selected by device type. Treat any other model as untested.
>
> If you try it on one, please open an issue with your `tools/rayneo_cli.py caps`
> output. That alone usually tells us whether it can work.

The glasses are treated as what they are: a **DisplayPort / USB-C external
display** with a side channel. The plugin talks to them directly over USB
interrupt transfers, the same way the official Android app does.

## Features

| Section | Controls |
| --- | --- |
| **连接 Connection** | Connect / disconnect, model + firmware readout |
| **显示 Display** | 屏幕亮度 brightness, 显示模式 display mode 2D/3D, 屏幕尺寸 screen size 115 % / 100 % / 85 % |
| | 场景模式 scene mode (通用/电影/护眼/阅读), 动态画质 dynamic quality SDR / AI-HDR, 色彩增强 colour-boost |
| **声音 Sound** | 音量 volume, 音效 sound effect (标准/轻语/空间环绕), 导音鳍 Sound Tube |
| **调试 Debug** | Largely hidden by default; see [Debug mode](#debug-mode) |

Every control is gated on the capability bitmap the glasses report (command
`0xE0`), so unsupported options grey themselves out rather than failing a write.

## Requirements

* Steam Deck (or any Decky Loader host) with **Decky Loader** installed
* **RayNeo GT Max** glasses — see the scope note above
* The glasses connected over USB-C

No udev rules, no system packages. `plugin.json` declares `"flags": ["root"]` so
Decky starts the plugin as root and it opens the glasses directly.

Root is genuinely required, not incidental: the glasses' USB node is
`0664 root:root`, so a non-root process cannot open it, and Decky offers no way
for a plugin to install a udev rule on an immutable read-only root.

## Install

### From a release zip

1. Download `RayNeo-Control.zip` from [Releases](../../releases).
2. Open Decky → **Settings → General** and turn on **开发者模式 / Developer mode**.
   This adds a **开发者 / Developer** tab.
3. In that tab, under **第三方插件 / Third-Party Plugins**, press
   **浏览文件 / Browse** next to **从 ZIP 压缩文件安装插件 / Install Plugin from
   ZIP File** and pick the zip.
4. Confirm the install prompt.

Decky hides the ZIP option behind developer mode, so step 2 is not optional — the
setting is not there without it.

### From source

```bash
brew install pnpm          # pnpm is required; the repo ships pnpm-lock.yaml
pnpm install
pnpm run build
python3 tools/package.py   # runs every check, then writes ../RayNeo-Control.zip
```

`tools/package.py` refuses to build if any check fails, so a successful run is a
tested artefact. Install the resulting zip the same way as above.

## Usage notes

* **Brightness is a lookup table, not a raw index.** The official app sends
  `table[index]`, with the table chosen per device. The GT Max uses the 29-step
  table. The plugin reproduces this exactly, down to the `0xFF` the firmware
  expects for an out-of-range index.
* **The brightness slider tops out at 12 steps while the panel may reflect fewer.**
  The glasses stop *reporting* brightness above the current display strategy
  mode's band (防抖 / 固定 / 随行) even though the write is accepted — which is
  also why the official app's slider appears to fall back on its own. The mode is
  not on the wire, so the ceiling is fixed rather than learned.
* **Changing 3D mode briefly blanks the glasses** and shows a split screen. That
  is the device's normal behaviour, matching the phone app.
* **Settings changed elsewhere are picked up within ~1.5 s.** There is no push
  channel; the plugin polls. See [How state reaches the UI](#how-state-reaches-the-ui).
* **护眼 (eye protection) clears colour boost once.** The device does that by
  itself on entry, but accepts it straight back, so the toggle stays available.
  **阅读 (reading) genuinely refuses it** — and reports success anyway, so the
  plugin greys the toggle out rather than trusting the read-back.

## Troubleshooting

The log is the first place to look:

```bash
ls -t ~/homebrew/logs/RayNeo-Control/*.log | head -1   # newest session
```

Each plugin reload starts a new file, so always take the newest.

| Symptom | What to check |
| --- | --- |
| Panel says **not connected** but the glasses are plugged in | Press Connect; the plugin retries once through a stale handle. If it still fails, the log says why — `libusb error -3` is a permissions problem, `-4` means the device is not there. |
| **Nothing updates** in the panel | The log should not show `glasses stopped answering`. If it does, the plugin releases the dead handle and keeps looking — replugging needs no button press. |
| A control **greys out unexpectedly** | Its capability bit is 0 for your unit. `tools/rayneo_cli.py caps` prints the ground truth. |
| **Brightness won't stay at the top** | Expected, see the usage note above. |
| Something **failed silently** | It should not: a refused write is logged as `backend REFUSED the change`, never as a confirmation. If you see the opposite, that is a bug worth reporting. |

For a report, turning on **调试模式 / Debug mode** at the bottom of the panel
first will capture the per-frame trace and the startup protocol tables. See
[Debug mode](#debug-mode).

## How state reaches the UI

There is no push channel: the USB IN endpoint is read only while a reply is being
awaited, so the plugin polls every 1.5 s. This is deliberate — the interrupt
endpoint on this hardware is fragile and a permanent reader would add exactly the
load that wedges it.

Two things follow from that, and both are visible in normal use:

* A change made on the glasses' own side (the phone app, a hardware button) shows
  up within one poll interval.
* The panel keeps itself honest. Every write that draws no reply — brightness,
  scene mode, picture quality, audio mode — is confirmed against the status
  read-back before the panel reports success, and if the device refused it, the
  control moves back to where the hardware actually is rather than staying on the
  requested value.

The plugin also recovers on its own: three unanswered polls release the handle
and it keeps looking for the glasses, so replugging needs no button press. A
disconnect *you* asked for is left alone — the button is not undone.

The long version of all of this, including the measurements behind it, is in
[`docs/NOTES.md`](docs/NOTES.md).

## Debug mode

One switch at the bottom of the panel covers both halves of "quiet by default":

| | debug off | debug on |
|---|---|---|
| per-frame TX trace | — | ✓ |
| startup protocol tables (0x00 / 0xE0 / 0xE3) | — | ✓ |
| `[ui]` timing notes | — | ✓ |
| debug tools (raw command, dumps, probes, calibration) | hidden | ✓ |
| errors, WARN lines, connect/disconnect, recovery | **✓** | ✓ |

Errors are deliberately outside the switch: a failure worth acting on is not debug
output, and gating it would make a bug report start blind. The switch itself never
hides, since it is the only way back to the tools it controls. The flag is
remembered across reloads.

## Development

The shipped layout matches what Decky Loader expects — `<plugin>/main.py` and
`<plugin>/dist/index.js` — with the Python modules in `py_modules/`, the one
directory the loader adds to `sys.path`.

```
plugin.json          Decky manifest
main.py              Decky RPC bridge (Python)
py_modules/
  rayneo.py            protocol + device control (pure Python, no deps)
  libusb_ctypes.py     dependency-free libusb-1.0 ctypes binding
src/                 the panel (TypeScript / React)
tools/               dev-only: packaging, checks, and a standalone CLI
docs/PROTOCOL.md     the reverse-engineering write-up
docs/NOTES.md        engineering notes: why it is built this way
```

### Checks

```bash
python3 tools/smoke_test.py .   # ~200 behavioural guards + the structural checks
python3 tools/structcheck.py    # the structural rules alone
ruff check --select=F,E9 main.py py_modules tools/
```

`tools/package.py` runs all of them before staging anything.

### Verifying against hardware

`tools/rayneo_cli.py` talks to the glasses without Decky — useful for checking a
value that looks wrong, or for bringing up a different model:

```bash
python3 tools/rayneo_cli.py devices      # is the glasses visible?
python3 tools/rayneo_cli.py caps         # capability bitmap — start here
python3 tools/rayneo_cli.py info         # full device-info block
python3 tools/rayneo_cli.py set brightness 8
python3 tools/rayneo_cli.py set scene-mode movie
python3 tools/rayneo_cli.py raw 00 00     # raw frame
```

As the normal `deck` user this needs the bundled udev rules; as root it does not:

```bash
sudo cp 99-rayneo.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# then unplug and replug the glasses
```

## How the protocol was obtained

The official Android app (雷鸟 XR 眼镜 v2.1.2) was decompiled and the native
library that carries all device traffic, `libFFalconXRServer.so`, was
reverse-engineered to recover the USB framing and the complete command set.

The full write-up — framing, every command byte, the response dispatch, the
device-info and status-report field offsets, and which of them are measured
versus inferred — is in [`docs/PROTOCOL.md`](docs/PROTOCOL.md). The command table
there was recovered twice, by two independent methods (raw disassembly and
Ghidra's decompiler), which agree on all 33 opcodes.

Nothing in this project redistributes anything from the app; it is an
independent implementation of the wire protocol, written from a disassembly.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by RayNeo or FFalcon. Product names are used only
to say what the software is for.
