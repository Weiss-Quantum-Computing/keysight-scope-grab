# Scope Grab

One-click waveform capture from a Keysight InfiniiVision MSO-X 2014A oscilloscope.
No BenchVue, no instrument-control licences - just PyVISA over the rear-panel USB-B
port.

Press GRAB (or the space bar) and you get, in your chosen folder:

| File | Contents |
|------|----------|
| `<prefix>_<timestamp>.csv` | Waveform samples: `time_s` plus one `CH<n>_V` column per selected channel |
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

- **Channels** - tick the channels to capture. Each becomes a column in the CSV.
- **Save to** - output folder. Defaults to `~/Desktop/scope_data`.
- **Filename prefix** - prepended to every file. Surrounding whitespace is trimmed and
  characters illegal in filenames are replaced with `_`.
- **Space** grabs, except while the focus is in a text field or on a control that uses
  the space bar itself - so typing a space in the prefix box does not fire an
  acquisition.
- **Auto-grab** - repeat on a fixed interval.

## Notes on acquisition

Capture uses `:SINGle` rather than `:DIGitize`, so the trace stays on the scope display
and the screenshot matches the CSV data. If no trigger arrives within 10 s the scope is
stopped and whatever is in acquisition memory is read out, with a note in the log.
Waveforms are transferred as unsigned bytes in `RAW` points mode and scaled to volts
using the preamble.
