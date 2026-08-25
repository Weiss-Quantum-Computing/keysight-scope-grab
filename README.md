# Scope Grab

One-click waveform capture from a Keysight InfiniiVision MSO-X 2014A oscilloscope.
No BenchVue, no instrument-control licences - just PyVISA over the rear-panel USB-B
port.

Press GRAB (or the space bar) and you get, in your chosen folder:

| File | Contents |
|------|----------|
| `<prefix>_<timestamp>.csv` | Waveform samples: `time_s` plus one column per selected channel, headed `CH<n>_V`, or `CH<n>_<name>_V` for a channel you have named |
| `<prefix>_<timestamp>.png` | Screenshot of the scope display |
| `<prefix>_<timestamp>.txt` | Acquisition metadata: sample rate, timebase, acquisition and trigger settings, and per-channel settings |

The panel shows the screenshots inline and you can scroll back through them while
a run is still going - see [Watching a run come in](#watching-a-run-come-in).

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
- **Peek (saves nothing)** - pull the scope's screen into the window and write no
  files at all, for adjusting things without accumulating data. See below.
- **Scope:** - the instrument's own buttons: run, stop, single, force trigger, clear
  display, autoscale. See [Scope control](#scope-control).
- **take the trace already on the scope** - save what the scope has already
  captured instead of arming a new acquisition, see below.
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

## Watching a run come in

The screenshot pane browses every PNG of the current prefix in the output folder,
in order. Use the mouse wheel over the picture, the left and right arrow keys, or
the **< prev** / **next >** buttons; double-click opens the full-resolution file.
The counter reads `6 / 10`, and `(4 newer)` in amber when you are behind the front.

While a sequence runs, a new screenshot pulls the view forward **only if you were
already on the newest one**. Scroll back to compare two runs and the view stays
put while captures keep landing, with the counter showing how many have arrived.
**newest** jumps to the front and resumes following.

Only screenshots matching the current filename prefix are browsed, so several runs
can share one folder without their screenshots interleaving. A prefix with no
captures yet shows an empty pane rather than someone else's run.

## Looking without saving

**Peek (saves nothing)** fetches the scope's screen into the panel and writes
nothing - no CSV, no PNG, no metadata, and no sequence label consumed. It is for
adjusting a setup and watching the effect without leaving a trail of files to sort
out afterwards.

It does not arm, stop or run the scope. Only the rendered display is read, so a
test in progress is left exactly as it was, and a live scope stays live. Nothing is
frozen because nothing is read out of acquisition memory.

The pane shows the screen with the caption `not saved`, and the counter under it
reads `not saved` rather than a position, since a peek is not in the browsed set.
Double-clicking it says so rather than opening anything. **prev** / **next** go
back to browsing the files on disk, and a capture arriving during a sequence still
pulls the view forward if it was following - peeking does not interrupt that.

Peek is greyed out while a capture or a sequence is running, since they share the
one VISA session.

## Taking the trace already on the scope

Tick **take the trace already on the scope (no new trigger)** and GRAB saves what
is in acquisition memory right now - the run you are already looking at - instead
of arming and waiting for another trigger. The trigger wait greys out, because
nothing is being waited for.

- The scope is stopped first, then read. That matters: reading memory while the
  scope is still acquiring returns a record torn between two acquisitions.
- Its run state is put back afterwards. A scope you had stopped on an interesting
  trace stays stopped on it, so you can grab it again - with other channels ticked,
  or under a different prefix. A scope that was live goes back to running.
- The metadata file records `capture mode : existing trace on the scope, not a new
  trigger`, so a file that was not a fresh acquisition says so months later. Normal
  captures are unchanged and gain no such line.
- The setting is **not** remembered between sessions. Left on by accident it would
  quietly save a stale trace as though it were a new capture, so every launch
  starts with it off.

A sequence refuses to start in this mode: nothing re-arms, so acquisition memory
never changes and every run would write a copy of the same trace. To capture
successive triggers - including ones you send by hand from the scope's own Single
button - leave it off and set **Wait for trigger** to `0`.

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
> The log makes this visible. Every capture reports where its time went, and in a
> sequence, a trigger already waiting the moment the scope armed - meaning earlier
> ones came and went unrecorded - is called out with the interval you would need.
> A one-off grab reports its timings but does not warn: a trigger already waiting
> there just means the signal is running. Neither does a sequence with the
> **interval set to 0** - that asks for runs back to back as fast as the readout
> allows, so a trigger pending at every re-arm is what was ordered rather than a
> fault, and saying so once per run would only bury the timing line under advice
> to slow down a sequence deliberately set to full speed. The timings are still
> printed, and they are what tells you the real cadence.
>
> ```
>   run 002: 0.0 s armed, 4.3 s reading, 5.7 s writing = 10.0 s
>   ! a trigger was already waiting when the scope armed:
>     triggers are arriving faster than a run takes, so some are being missed
>     lower 'Transfer points', or leave more than 10 s between triggers
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
  the sequence advances to the first free one and notes it in the log. That is
  what stacks one sequence on the next: leave **First label** at 1, run ten, press
  Start again, and the second batch lands on 011-020.
- **First label stays where you put it.** It is an instruction, not a counter, and
  nothing moves it - not the end of a sequence, not Stop, not a run called off
  before it saved. It used to wind on to the next free number, which meant the box
  said one thing before a sequence and another after, and re-running the same
  labels needed it set back by hand every time. Set it yourself when you want the
  series to start somewhere else. The preview line under the button is what tells
  you where the next run will actually land, and it says `(from 001 up already
  exist)` when the label you asked for is taken.
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

The **Scope settings** panel mirrors the instrument, in three blocks:

**Timebase / acquisition** - s/div, horizontal position, reference (left / centre /
right), sweep mode (main, zoom window, XY, roll), acquisition type (normal, averaging,
high-resolution, peak-detect) and the average count. Sample rate and points acquired
sit underneath, read-only - they are results of an acquisition rather than knobs.

**Trigger** - trigger type (edge, glitch, pattern, TV, ...), sweep (auto / normal),
and for the edge trigger: source, level, slope, HF/LF reject, noise reject and
holdoff.

**Per channel** - V/div, offset, coupling, probe attenuation, units (volts or amps),
bandwidth limit, invert and display state, for all four channels.

It reads from the scope when it connects, on **Read from scope**, and
automatically after every grab - so settings changed with the scope's own knobs
show up without asking.

Bandwidth limit, invert, display and noise reject are checkboxes; the rest are text
boxes and drop-downs.

To change a setting from the window, edit the field and press **Apply changes**:

- Only edited fields are written. A `*` next to a field marks it as edited but not
  yet applied, and the status line counts them.
- Fields the scope is currently ignoring are greyed out: the average count unless the
  acquisition type is averaging, and the edge-trigger fields unless the trigger type
  is edge. The scope keeps answering those queries with a stale value - an average
  count left over from the last time averaging was on, an edge level under a
  pulse-width trigger - and greying them is what says the number on show is not in
  force. Change the mode in its drop-down and the fields it governs come alive at
  once, before anything is applied.
- Writes are ordered so a mode lands before the fields it governs. Switching to
  averaging and setting the count in the same Apply works, because `:ACQuire:TYPE`
  is written before `:ACQuire:COUNt` - the other way round the scope takes the count
  and quietly does nothing with it.
- A pull never discards an edit you have not applied yet. If a value changes on the
  scope while you have a pending edit for the same field, your edit stays in the
  box and the log notes that the scope disagrees.
- After a write the panel re-reads the instrument, so what you see is what the
  scope actually accepted rather than what you asked for - relevant because the
  scope silently clamps values outside its range.
- The scope's error queue is drained after every apply and anything it reports is
  written to the log.
- Applying ends with a peek: the scope's screen is pulled into the pane so the
  effect of the change is visible without saving anything. The display is given a
  moment to redraw first, so on a slow timebase the picture may still be one sweep
  behind - peek again to catch up. **Read from scope** does not peek, and neither
  does an Apply with nothing edited.

### Putting the panel back onto the scope

**Apply changes** writes the fields edited *in this window*. Nothing marked does
not mean nothing to do, though, and the case where those come apart is a common
one: you reach over and turn a knob on the scope itself. The panel still holds
the setting you want and still believes the scope has it, so nothing is marked.
Getting back used to mean pressing **Read from scope** - which overwrites the
panel with exactly the state you are trying to leave - then re-typing the old
value from memory.

So an Apply that finds nothing marked asks instead of refusing:

```
There are no apparent changes to be made - every field matches what the
scope last reported.

If a setting was changed on the scope itself, this window would not know,
and nothing here is marked as edited.

Send all 45 settings anyway? This puts the panel back onto the scope,
overwriting anything changed at the front panel.
```

Say yes and the whole panel goes to the instrument, edited or not - which is
almost always why Apply was pressed with nothing marked. Say no and you get the
old `No setting changes to apply` in the log and nothing is written. There is no
separate button for it: the one you would reach for now covers both.

- Blank fields are skipped, so an Apply before the first read just says the panel
  has not been filled in, without offering anything.
- Greyed-out fields are skipped too: their displayed value is a stale reply the
  scope is not acting on, and writing it would assert it as a real choice.
  Liveness is judged from the panel's own modes, not the scope's, so selecting
  `AVER` and sending puts the count down with it.
- Ordering, read-back, error draining and the peek afterwards are unchanged - it
  is the same write path, given a longer list.

### Saving and loading a setup

**Load/save setups...**, beside **Connect** at the top, opens a small window with
**Save setup** and **Load setup...** in it - the same button in the same corner as
the [BK4063B AWG GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI),
so the two panels are one habit. A window rather than two more buttons in the
settings panel because saving and loading happen once at the start and once at
the end of a session, while the settings bar is for what gets pressed while
working.

Files go to `Desktop/scope_setups` as `<prefix>_<timestamp>.json` with a readable
`.txt` beside it - the `.json` is what loads back, the `.txt` is what goes in the
notebook. The prefix box in the window is the same one the captures use.

Saved is **the panel**, not the instrument. That means it works with nothing
connected, and what you can see is what you get. If a field is an edit you have
not applied, the file records it as one under `unapplied_edits` and the `.txt`
says so at the top - a setup never claims to be a reading it isn't. Fields the
scope is currently ignoring are saved anyway, because a setup that switches to
averaging has to carry the count that goes with it.

Loading never writes to the scope on its own. The values land in the panel
first, marked `*` against whatever the scope last reported, so you can see what
is about to change - and then it asks whether to send them. Say no and they stay
in the panel as ordinary edits for **Apply changes** to write later; with nothing
connected it just says so.

The capture-side fields travel with the setup as well: prefix, which channels
are ticked, their names, trigger wait and transfer points. The **output folder
does not**. That belongs to where you are working now rather than to the setup
being recalled, and a setup from another experiment quietly redirecting where
captures land is the one surprise here that costs you a file.

Settings traffic and captures share one VISA session, so they are serialised: the
buttons grey out while a grab is running and vice versa. The setups window is the
exception to half of that - its two buttons only touch the panel, so they stay
live with the scope unplugged, and grey out only while a grab or sequence is
running.

## Averaging

Set **Acquisition** to `AVER` and put the depth in **Averages** - the scope rounds it
to a power of two between 2 and 65536, and the panel shows you what it settled on.

Averaging needs successive triggers to build up, and a grab arms a *single*
acquisition, so a trace can come back with less averaging than the setting asks for.
Nothing on the scope's own screen distinguishes the two, so every averaged grab is
checked and reported:

```
  ! averaging: 3 of 8 hits in this trace
    the trace is less averaged than the setting says - leave the scope running on
    more triggers to build the average up
```

The depth is read straight after the waveform transfer, where the scope certainly
has a record to describe. Asked at any other moment - with acquisition memory empty
after a mode change, say - it answers `+109,"No Data For Operation"` and sends
nothing at all, so the query is made with a short timeout and the line is simply left
out if the scope will not answer it.

The `.txt` file records both numbers - the count that was asked for and the hits the
trace actually got - so a saved capture always says how deeply it was averaged:

```
acquisition type   : AVER
averages           : 8
averages taken     : 8 of 8   (hits actually in the trace that was read out)
```

Out of averaging mode the file says so, rather than leaving a count that reads as
though it applied:

```
acquisition type   : NORM
averages           : 8   (not in use: acquisition type is not AVERage)
```

To capture a fully averaged trace, let the scope free-run on the signal - **Run**,
wait for the average to settle, **Stop** - and then grab with **take the trace
already on the scope** ticked.

## Scope control

The row of buttons under GRAB drives the instrument directly. They are things the
scope does once rather than states it holds, so there is nothing to edit and nothing
to apply - each fires immediately, then the panel re-reads and the screen is pulled
into the preview so you can see what it did.

| Button | Does |
|--------|------|
| **Run** | Free-run on triggers, as the front-panel Run/Stop key |
| **Stop** | Stop acquiring, leaving what is in memory |
| **Single** | Arm one acquisition and wait - the same arming a grab does, without saving anything |
| **Force trig** | Trigger now, whether or not the condition was met - the way to get a trace out of a signal that never crosses the level |
| **Clear** | Clear the display, restarting averaging and persistence from zero |
| **Autoscale** | Let the scope find the signal and set the timebase and every channel's V/div and offset itself. This throws the current setup away, so it asks first |

Autoscale is the only one that rewrites settings, so it is the only one allowed to
overwrite an edit you have typed but not applied; the rest leave the panel's pending
edits where they are.

## The log

The log wraps rather than running off to the right, with continuations indented so
a long entry still reads as one. Messages are kept to a line where they fit, and
split across lines where they do not, so nothing depends on the window being a
particular width - resize it and the text reflows. Because the wrapping is the
widget's rather than baked into the text, copying an entry out gives you one line
again.

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

This is not the same thing as a saved setup. This file is one implicit
last-session state, rewritten under you and never named; a setup is a file you
asked for, holds the scope settings as well, and is only ever read back when you
pick it. The capture-side fields they have in common are stored under the same
keys and restored by the same code, so a setup recalls them the way a new
session does - except for the output folder, which a setup deliberately leaves
alone.

## Notes on acquisition

Capture uses `:SINGle` rather than `:DIGitize`, so the trace stays on the scope display
and the screenshot matches the CSV data. One consequence is that an averaged capture
gets one acquisition rather than a full set of averages - see
[Averaging](#averaging), which reports the depth each trace actually got. If no trigger arrives within 10 s the scope is
stopped and whatever is in acquisition memory is read out, with a note in the log.
Waveforms are transferred as unsigned bytes in `RAW` points mode and scaled to volts
using the preamble.

A timestamped capture never overwrites one that is already there. The timestamp
only resolves to a second, so a second capture inside the same second is saved with
a `_2` suffix rather than replacing the first. Sequence labels have their own check.

Each grab reads the instrument's settings exactly once, and both the panel and the
`.txt` file are rendered from that single snapshot - so the two can never disagree
about what the scope was doing. The file records the scope's own strings unrounded;
the panel shows the same values trimmed for legibility.
