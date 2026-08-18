# Scope Grab

One-click waveform capture from a Keysight InfiniiVision MSO-X 2014A oscilloscope.
No BenchVue, no instrument-control licences - just PyVISA over the rear-panel USB-B
port.

Press GRAB (or the space bar) and you get, in your chosen folder:

| File | Contents |
|------|----------|
| `<prefix>_<timestamp>.csv` | Waveform samples: `time_s` plus one column per selected channel, headed `CH<n>_V`, or `CH<n>_<name>_V` for a channel you have named |
| `<prefix>_<timestamp>.png` | Screenshot of the scope display |
| `<prefix>_<timestamp>.txt` | Acquisition metadata: sample rate, timebase, trigger and per-channel settings |

The panel shows the most recent screenshot inline; double-click it to open the
full-resolution PNG.

## Requirements

- [Keysight IO Libraries Suite](https://www.keysight.com/find/iosuite) (provides the VISA layer)
- Python 3.9+
- `pip install pyvisa numpy pillow`

Pillow is optional - it only gives the screenshot preview a smoother rescale. Without
it the preview falls back to Tk's integer subsample.

## Usage

```
pythonw scope_grab.py
```

`pythonw` keeps the console window from appearing. The app auto-connects to the first
Keysight/Agilent USB instrument it finds; hit **Connect** to retry.

- **Channels** - tick the channels to capture and optionally name each one. Each
  ticked channel becomes a column in the CSV.
- **Save to** - output folder. Defaults to `~/Desktop/scope_data`.
- **Filename prefix** - prepended to every file. Surrounding whitespace is trimmed and
  characters illegal in filenames are replaced with `_`.
- **Space** grabs, except while the focus is in a text field or on a control that uses
  the space bar itself - so typing a space in the prefix box does not fire an
  acquisition.
- **Auto-grab** - repeat on a fixed interval.

## Channel names

Naming a channel changes its CSV header from `CH1_V` to `CH1_EOM_drive_V`, and adds
a `CH1 name` line to the metadata file recording the name exactly as typed. The
column the name will produce is shown next to the box as you type.

The channel number stays in the header even when a channel is named, so a column
is always traceable to the per-channel settings in the metadata file, and two
channels sharing a name cannot collide. Headers are reduced to ASCII letters,
digits, `-`, `.` and `_`, so no name can introduce a stray delimiter or an
encoding problem: `cavity refl,fast` becomes `CH3_cavity_refl_fast_V`.

Names are remembered between sessions - see below.

## Scope settings

The **Scope settings** panel mirrors the instrument: timebase, trigger and
acquisition mode, plus V/div, offset, coupling, probe attenuation, bandwidth limit
and display state for each channel.

Sample rate and points acquired sit alongside them, read-only - they are results
of an acquisition rather than knobs.

It reads from the scope when it connects, on **Read from scope**, and
automatically after every grab - so settings changed with the scope's own knobs
show up without asking.

To change a setting from the window, edit the field and press **Apply changes**:

- Only edited fields are written. A `*` next to a field marks it as edited but not
  yet applied, and the status line counts them.
- A pull never discards an edit you have not applied yet. If a value changes on the
  scope while you have a pending edit for the same field, your edit stays in the
  box and the log notes that the scope disagrees.
- After a write the panel re-reads the instrument, so what you see is what the
  scope actually accepted rather than what you asked for - relevant because the
  scope silently clamps values outside its range.
- The scope's error queue is drained after every apply and anything it reports is
  written to the log.

Settings traffic and captures share one VISA session, so they are serialised: the
buttons grey out while a grab is running and vice versa.

## Remembered settings

The output folder, filename prefix and channel names are written to

```
%APPDATA%\ScopeGrab\config.json
```

so a session starts where the last one left off. The file is written after a
capture, when you pick a folder, and on close - only when something actually
changed. It lives outside the program folder, so updating this repo will not
touch it.

```json
{
  "outdir": "C:\\Users\\you\\Desktop\\scope_data",
  "prefix": "EOM run",
  "channel_names": { "1": "EOM drive", "2": "cavity refl", "3": "", "4": "" }
}
```

Delete the file to go back to defaults. A missing, truncated or malformed file is
ignored - each value falls back to its default independently, and the log notes
when a file could not be read, so a bad config can never stop the app from
starting.

## Notes on acquisition

Capture uses `:SINGle` rather than `:DIGitize`, so the trace stays on the scope display
and the screenshot matches the CSV data. If no trigger arrives within 10 s the scope is
stopped and whatever is in acquisition memory is read out, with a note in the log.
Waveforms are transferred as unsigned bytes in `RAW` points mode and scaled to volts
using the preamble.

Each grab reads the instrument's settings exactly once, and both the panel and the
`.txt` file are rendered from that single snapshot - so the two can never disagree
about what the scope was doing. The file records the scope's own strings unrounded;
the panel shows the same values trimmed for legibility.
