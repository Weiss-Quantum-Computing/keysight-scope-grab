# DS1054Z profile: what is settled and what is not

Source: **RIGOL MSO1000Z/DS1000Z Programming Guide**, Dec 2015 (260 pp), read
in full. Page numbers below are the guide's own (`2-nn`).

Nothing here has been run against hardware. Everything in the first section is
what the guide states; everything in the second is what the guide cannot answer
and `tools/probe_scope.py` is written to measure.

## Settled by the guide

### Where the Rigol and the Keysight agree

Worth stating first, because it is more than expected and it means the shared
half of `Scope` needs no changes:

- **`:WAVeform:PREamble?` has the same ten fields in the same order** —
  `<format>,<type>,<points>,<count>,<xincrement>,<xorigin>,<xreference>,<yincrement>,<yorigin>,<yreference>`
  (2-242). So `pre[4..9]` indexes correctly on both, and `read_waveform` differs
  only in the setup commands around it.
- **`:ACQuire:TYPE` returns `NORM`/`AVER`/`PEAK`/`HRES`** (2-6) — identical short
  forms, so `avg_prefix = "AVER"` holds and the panel's choice list is unchanged.
- `:RUN`, `:STOP`, `:SINGle`, `:AUToscale` are spelled the same (2-2, 2-3).
- `:SYSTem:ERRor?` behaves the same, `0,"No error"` when empty.
- `:TRIGger:MODE`, `:TRIGger:SWEep`, `:TRIGger:NREJect`, `:TRIGger:HOLDoff` and
  `:CHANnel<n>:{SCALe,OFFSet,PROBe,DISPlay,INVert}` all carry over by name.

### Where they differ

| Keysight | Rigol | Note |
|---|---|---|
| `:ACQuire:COUNt` | `:ACQuire:AVERages` | 2^n, n=1..10, so 2–1024 only (2-4) |
| `:ACQuire:POINts?` (read-only) | `:ACQuire:MDEPth` (writable) | discrete, and **the valid list depends on how many channels are on** (2-5) |
| `:WAVeform:POINts:MODE` | `:WAVeform:MODE` | `{NORMal\|MAXimum\|RAW}` (2-218) |
| `:WAVeform:POINts` | `:WAVeform:STARt` / `:STOP` | NORMal is 1..1200, so the screen is 1200 pts (2-240) |
| `:WAVeform:COUNt` | *nothing* | the hit count is **preamble field 3**, not a queryable root |
| `:TRIGger:FORCe` | `:TFORce` | 2-3 |
| `:CDISplay` | `:CLEar` | 2-2 |
| `:TIMebase:POSition` | `:TIMebase:MAIN:OFFSet` | 2-206 |
| `:TIMebase:REFerence` | *nothing* | no equivalent; drop the row |
| `:TRIGger:EDGE:REJect` | `:TRIGger:COUPling` | `{AC\|DC\|LFReject\|HFReject}` (2-158) |
| `:OPERegister:CONDition?` | `:TRIGger:STATus?` | returns `TD\|WAIT\|RUN\|AUTO\|STOP` (2-159) |
| `:DISPlay:DATA? PNG,COLor` | `:DISPlay:DATA? ON,0,PNG` | `[<color>,<invert>,<format>]` (2-73) |

### Table changes that are not just renames

- **`:CHANnel<n>:BWLimit` is `{20M|OFF}`, not `ON`/`OFF`** (2-30). It cannot use
  the `bool` kind — the panel would write `ON` and the scope would reject it.
  It becomes a `choice`.
- **`:CHANnel<n>:COUPling` is `{AC|DC|GND}`** (2-31) — three values.
- **`:CHANnel<n>:UNITs` is `{VOLTage|WATT|AMPere|UNKNown}`**, returning
  `VOLT/WATT/AMP/UNKN` (2-35).
- **`:TRIGger:EDGe:SLOPe` is `{POSitive|NEGative|RFALl}`** (2-113), not the
  Keysight `POS/NEG/EITH/ALT`.
- **`:TIMebase:MODE` is `{MAIN|XY|ROLL}`** (2-208) — no `WIND`.
- **`:TRIGger:EDGe:SOURce`** includes the 16 digital channels on an MSO
  (2-112). For a DS1054Z the analog four plus `AC` (line) is the useful list.
- `:CHANnel<n>:PROBe` is a **discrete** list, not a free number (2-33):
  0.01 … 1000 in 1-2-5 steps.

### The WORD question, answered against us

The MSO-X reads an averaged record back in WORD at 16x the display resolution,
which is why `read_waveform` uses WORD for everything. **The Rigol does not.**
The guide is explicit (2-218):

> WORD: a waveform point occupies two bytes (namely 16 bits) in which the lower
> 8 bits are valid and the higher 8 bits are 0.

So on the Rigol, WORD is 8-bit data padded to 16 and doubles the transfer for
nothing. The profile should use BYTE. The probe measures this anyway rather
than taking the guide's word for it, since it is the sort of claim firmware
quietly changes.

### Chunked readout — real, but out of scope for now

`:WAVeform:DATA?` serves at most 250000 points per read in BYTE, 125000 in
WORD, 15625 in ASCii (2-219), so a deep record needs a `:WAV:STARt`/`:STOP`
loop, and RAW mode requires the scope to be stopped. **Not needed for the
current plan** — short screen-depth records at 1200 points read in one go on
NORMal. Left unbuilt and marked as a gap rather than half-built.

## Not settled by the guide — what the probe is for

These are the ones that cost releases on the MSO-X, and the guide says nothing
useful about any of them.

1. **Is the averager running or block, under RUN?** On the MSO-X each sweep
   folds in with weight 1/N, so the record carries an exponential memory of
   whatever played before and nothing resets it. If the Rigol does the same,
   every use-existing grab in averaging mode is contaminated the same way.
2. **Does preamble `<count>` report the setting or the accumulated depth?**
   The guide says only "the number of averages in the average sample mode"
   (2-242). On the MSO-X the equivalent reports the *setting* the moment RUN is
   involved, which made a poll declare a contaminated average complete.
3. **Does `:CLEar` restart the average?** `:CDISplay` did not, on the MSO-X.
4. **What does `:SINGle` give in averaging mode** — a full N-deep average, or
   one hit while claiming the full depth? The MSO-X did the latter.
5. **Is there any honest counted build?** There is no `:DIGitize` in the guide's
   index. If nothing counts triggers out, `accumulate` has to be built from
   `:SINGle` in a loop with the averaging done here rather than on the scope —
   a different design, not a port.
6. **Which readout modes serve which record**, after `:SINGle` versus under RUN.
7. **The actual `*IDN?`, firmware, and whether the memory-depth option is
   installed** — the MDEPth list depends on it.
8. **PNG size**, for the preview box.

## Measured on the bench, 2 Sep 2026

DS1054Z, `DS1ZA184752794`, firmware **00.04.04.SP1**, over USB
(`USB0::0x1AB1::0x04CE::DS1ZA184752794::INSTR`). Three runs of
`tools/probe_scope.py`; reports on the Desktop as `scope_probe_2026*.txt`.

### Settled

1. **There is no `:DIGitize`.** It answers `-113,"Undefined header; keyword
   cannot be found"`. The MSO-X `accumulate` is built entirely on it, so it
   does not port - a Rigol `accumulate` is a different design, not a
   translation.
2. **`:SINGle` is a sweep-mode write, not a one-shot arm.** The guide says it
   in passing (2-3): it is "equivalent to ... sending the `:TRIGger:SWEep
   SINGle` command", and it leaves the sweep there afterwards. A capture that
   arms a single shot therefore rewrites a trigger setting the front panel
   shows as yours. **The profile must save and restore `:TRIGger:SWEep` around
   `single()`.** Found the hard way: the probe did not, and parked the scope in
   single-sweep mode, which made every phase after it wait forever.
3. **All three readout modes serve, in both states.** `NORMal`, `MAXimum` and
   `RAW` each returned 1200 points while running *and* after a stop, in BYTE
   and WORD. Nothing like the MSO-X rule where a record stopped out of RUN
   refuses RAW. So `transfer_plan` collapses to one mode for everything.
4. **WORD buys nothing.** Same two distinct levels and the same 0.08 V step in
   both formats, exactly as the guide claims (2-218). The profile reads BYTE
   and halves the transfer.
5. **`:CLEar` does not restart the average.** Noise did not jump back up after
   it, and the preamble count stayed at the setting. The same trap as the
   MSO-X, where `:CDISplay` did not reset either.
6. **Screenshot:** `:DISPlay:DATA? ON,0,PNG` returns a 800x480 PNG, ~36-39 kB.
7. **Memory-depth rejections are silent.** Of `AUTO/3000/6000/12000/30000/
   120000`, only `AUTO` and `6000` took, and the rest left the value alone with
   an **empty error queue** - no `-222`, nothing. The MSO-X would have
   complained. `errors()` cannot be relied on to catch a bad depth here.
8. **`:TRIGger:EDGE:...` - the Keysight capitalisation - is accepted**, since
   SCPI header matching is case-insensitive and `EDGE` is the long form of the
   Rigol's `EDGe`. So that root can be spelled the same in both profiles.
   `:TRIGger:EDGE:REJect` still does not exist; it is `:TRIGger:COUPling`.

### The averager, measured on a proper signal

The first two runs measured nothing here: the channel carried a DC level with
one LSB of dither and no edges, so nothing triggered and the noise ruler was
quantisation-limited. With CH1 on the probe-comp terminal - a 1 kHz square -
all of it came out.

**The count reports the SETTING, not the hits so far.** Asking for a 1024-deep
average made the preamble count read `1024` within 0.4 s and sit there, while
the noise on the trace was still visibly falling for another five seconds
(0.0126 -> 0.0040 V). It claims the average is finished long before it is.
Exactly the MSO-X trap, and it means **polling cannot tell you an average is
complete**.

**Nothing resets the averager.** Not `:CLEar`, not a stop/run cycle. So a
hardware average always carries whatever was on the probe before it.

**There is no `:DIGitize`.** `-113,"Undefined header"`.

Three ways of being lied to and no way to check, which is what drives the
design below.

The one question left genuinely open is whether the averager is running or
block. The noise ratio settled at 4.7-5.9 against sqrt(64) = 8, which is
consistent with either once the 8-bit readback floor is allowed for, and
separating them would need a controlled step change in the signal. It stopped
mattering: the profile does not use the hardware averager at all.

`:SINGle` in averaging mode was never measured either - the read after it came
back empty each time. Same reason it stopped mattering.

### Why the profile averages in software

Given a counter that lies, no reset, and no counted build, there is nothing to
build a trustworthy hardware average on. Averaging in `accumulate()` instead
turns out to be better rather than merely safer, and the deciding measurement
was direct: **64 sweeps averaged here came back using 188 distinct codes; the
scope's own 64-deep average came back using 13-14.** Thirteen times the
effective resolution, from the same 64 sweeps.

The reason is the 8-bit readback. The scope averages internally and then
re-quantises to 8 bits on the way out, so most of what averaging bought is
thrown away in the transfer. Summing raw codes in float here keeps it. This is
the mirror image of the MSO-X, where WORD readback carries a real 16x on an
averaged record and using the hardware averager is the right answer.

It also gets an exact, known depth - the sweeps are counted here - and no
contamination, because the sum starts empty every time. `accumulate()` reads
every channel out of the same sweeps so they stay simultaneous, discards a
record identical to the previous one so a fast read cannot inflate the depth,
and files the honest number where the metadata will find it.

**The cost is speed.** Each sweep is a VISA round trip, measured at about
157 ms for 1200 points in the probe and ~110 ms in the profile's tighter loop,
so a 64-deep average takes several seconds where the MSO-X's `:DIGitize` would
take 64 trigger periods. Deep averages are correspondingly slow: 1024 deep is
around two minutes. If that becomes a problem the fix is to stop re-sending
the readout setup on every sweep, which is most of the round trip.

### Verified against the instrument

`tools/check_profile.py ds1054z` runs the profile through `Scope`, the same
path the GUI uses. All 47 panel fields answer; a plain capture returns 1200
points of sane volts; `single()` leaves the trigger sweep exactly as it found
it; an 8-deep average builds in 0.9 s and raises the trace from 21 to 32
distinct levels; the screenshot is a PNG; the metadata file comes out with no
unknowns; and every setting it touched came home.

## Running the probe

```
python tools/probe_scope.py
```

It needs a live, repeating, triggering signal on channel 1. It changes the
acquisition type, the average count and the run state and puts all three back
at the end, including on Ctrl-C; it never touches the timebase, the channel
scales or the trigger setup. It writes `scope_probe_<stamp>.txt` to the Desktop.

The averaging tests work by watching a noise figure — the standard deviation of
the second difference of the trace, which ignores signal shape — fall as
sqrt(N) with depth. That is what makes questions 1 to 4 answerable without
knowing anything about the signal on the probe.

`tests/test_probe.py` drives the whole thing against two mock instruments, so a
typo in it shows up here rather than at the bench.
