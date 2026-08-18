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
- **Wait for trigger** - how long a capture stays armed. `0` waits indefinitely,
  for priming before an experiment elsewhere starts triggering. While a one-off
  grab is armed, GRAB becomes **Cancel wait**.
- **Transfer points** - how many samples to pull per channel; `max` takes the whole
  acquisition memory. Fewer points means smaller, faster files.
- **Auto-grab** - repeat on a fixed interval, keeping timestamped names.
- **Sequence** - a fixed number of runs with incrementing labels, see below.
- **save screenshot?** - whether each grab also writes the PNG. The preview only
  updates when this is on, since it displays the file that was written.

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

## Priming a capture for an external trigger

Set **Wait for trigger** to `0` and the scope stays armed indefinitely, so you can
start a capture here and then start the experiment on another machine. A sequence
with **Interval** `0` and no trigger limit paces itself off the incoming triggers:
each run arms, waits for its trigger, saves, and arms again.

A run that never triggers **writes no files at all** - not a CSV of whatever was
left in acquisition memory. If a run saves nothing, a sequence stops rather than
burning through the remaining labels, and the log says which label it stopped at.
Press **Cancel wait** (or **Stop sequence**) to call off a wait; a transfer already
under way finishes rather than being thrown away.

> **Triggers that arrive during a run are missed.** The scope is not armed while
> its data is being read out and written, which takes seconds for a long record.
> If the source triggers faster than a run takes, run `005` will not be the fifth
> shot - it will be the fifth one that happened to land while the scope was
> armed. Space the triggers wider than one run, or shorten the run with fewer
> **Transfer points**.
>
> The log makes this visible. Every run reports where its time went, and if a
> trigger was already waiting the moment the scope armed - meaning earlier ones
> came and went unrecorded - it says so and names the interval you would need:
>
> ```
>   run 002: 0.0 s waiting for the trigger, 4.3 s reading, 5.7 s writing (10.0 s total)
>   ! a trigger was already waiting when the scope armed: triggers are arriving
>     faster than a run takes, so some are being missed. Lower 'Transfer points',
>     or slow the source to more than 10 s between triggers.
> ```
>
> A worked example: 1M points on 4 channels takes about 10 s per run. Against a
> source firing every 5 s that captures every second shot, so a 30-shot run
> yields 15 files - each valid, but sampling every other shot.

## Reducing how much data each run takes

The sample rate is not directly settable on this scope - it follows from the
timebase and the memory depth, which is why the panel shows it read-only. Two
things do reduce the data:

- **Transfer points** limits what is read out and written (`:WAVeform:POINts`). The
  scope still samples at full rate and then decimates the transfer, so this shrinks
  files and shortens runs but does not change what the instrument acquired. Narrow
  features can be decimated away. The scope rounds the request to a value it likes,
  and the time axis is always taken from the waveform preamble, so the file stays
  self-consistent whatever it gives you - worth checking the first capture's time
  column against the scope screen.
- **A slower timebase** genuinely lowers the sample rate for a given memory depth,
  and can be set from the settings panel. Memory depth itself lives in the scope's
  own Acquire menu.

## Numbered sequences

A sequence takes a set number of captures and labels them by number instead of by
clock time: `EOM ramp_001.csv`, `EOM ramp_002.csv`, and so on, with the matching
`.txt` and `.png`. Set the number of runs, the interval, and the first label, then
press **Start sequence**. The next file name is shown under the button.

Nothing is lost by dropping the timestamp - the wall-clock time is still recorded
inside each `.txt`, which also gains a `sequence label` line.

- **Runs are chained off completion, not off a timer.** The next run is scheduled
  once the previous one has finished writing, so a transfer that overruns the
  interval can never overlap the next run or skip a label. You always get the
  number of files you asked for, contiguously numbered.
- The interval is measured from the *start* of each run, so a 1 s interval with a
  0.3 s run gives a 1 s cadence. If a run takes longer than the interval the next
  begins immediately and the log says so - which is how you find out the interval
  is too short to honour.
- **Existing files are never overwritten.** If the first label is already on disk
  the sequence advances to the first free one and notes it in the log. When a
  sequence ends, **First label** moves past the end, so pressing Start again
  continues the series rather than colliding with it.
- **Stop** ends a sequence early. A run already under way finishes and is saved,
  and is included in the count.
- Auto-grab is switched off when a sequence starts, since only one repeating
  mechanism should drive the scope at a time.

### How short can the interval be?

The limit is usually writing the CSV rather than the USB transfer. Measured with
`numpy.savetxt` on this machine:

| Points | Columns | CSV write | File size |
|-------:|--------:|----------:|----------:|
| 500k | time + 1 channel | 2.0 s | 26 MB |
| 500k | time + 4 channels | 3.6 s | 64 MB |
| 1M | time + 1 channel | 3.9 s | 52 MB |
| 2M | time + 1 channel | 7.9 s | 103 MB |

Those figures are for the old 18-digit format; volts are now written as `%.6e`,
which is about 40% smaller and correspondingly quicker. Samples arrive from the
scope as 8-bit codes - 256 levels - so six significant figures record far more than
the instrument resolves. Time keeps ten digits, enough to separate adjacent samples
in a long record.

So a 1 s interval is only realistic for short records. Ask for it anyway if you
like - the sequence will simply run as fast as it can and tell you it could not
keep up. Note also the disk cost: 100 runs of a 4-channel 500k-point capture is
about 6 GB.

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

The output folder, filename prefix, channel names, which channels are ticked, the
trigger wait and the transfer point count are written to

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
  "channel_names": { "1": "EOM drive", "2": "cavity refl", "3": "", "4": "" },
  "channels": { "1": true, "2": true, "3": false, "4": false },
  "trigger_wait": "0",
  "transfer_points": "max"
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
