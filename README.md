# SignalScope

Real-time visualisation of a 2 Msps signal streamed over WebSocket from a microcontroller.
Built for the TII-DERC Senior Software Engineer software challenge.

> **Status: all seven phases complete, plus stretch items S1–S4.**
> Domain, acquisition pipeline, throttling boundary, `/stream` endpoint, browser shell,
> time-domain plot (Phase 5), sample-rate gauge (Phase 6), and Docker delivery (Phase 7).
> Stretch: frequency-domain tab with `setDomain` gating (S1), a `ctypes`-bound C
> decimator with a numpy fallback (S2), CSV export of the retained log (S3), and live
> FFT peak detection (S4).

## Quickstart (Docker)

```
docker compose up --build
```

Then open `http://localhost:8000`, enter `ws://localhost:8765` in the URL box and click
Connect. That is the whole procedure: the compose file brings up both the app and the
microcontroller simulator, and the image compiles the optional C decimator on the way.

## Quickstart (native, Ubuntu 24.04 / Fedora 42)

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python backend/mock/mock_uc_server.py --port 8765 &
gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:8000 "backend.app:create_app()"
```

Open `http://localhost:8000`, enter `ws://localhost:8765` in the URL box, click Connect.
`-w 1` is load-bearing, not a default — see [Why `gunicorn -w 1`](#why-gunicorn--w-1).

`GET /healthz` reports the configuration the process actually started with, so "it is
running" and "it is running with the settings you think" are one question rather than
two. Every setting is environment-driven: `EXPECTED_SAMPLES`, `SAMPLE_RATE_HZ`,
`PUBLISH_HZ`, `TARGET_COLUMNS`, `SPECTRUM_BINS`, `MAX_PENDING_REPORTS`,
`UPSTREAM_CONNECT_TIMEOUT_S`, `RATE_EMA_ALPHA`, `MAX_DOWNSTREAM_MESSAGE_BYTES`.

## Driving the simulator

**The short version: every fault is triggered from the URL box in the app.** Paste one of
these into the WebSocket URL field and press Connect. Nothing to restart, nothing to edit.

```
ws://localhost:8765/                        clean 2 Msps stream
ws://localhost:8765/?bad_frame_every=25     red log lines
ws://localhost:8765/?drop_after=500         drop popup
ws://localhost:8765/?rate_factor=0.5        gauge reads ~1 Msps, deviation −50 %
ws://localhost:8765/?malformed_every=40     corrupt framing (reported separately)
```

They combine: `ws://localhost:8765/?bad_frame_every=25&drop_after=500` gives red lines for
twenty seconds and then the disconnect popup.

The same settings exist as command-line flags, which set the **defaults** the simulator
starts with. A query parameter overrides one of them for that connection only; anything
not named in the URL keeps the flag's value.

```
python backend/mock/mock_uc_server.py --port 8765                      # clean 2 Msps stream
python backend/mock/mock_uc_server.py --port 8765 --bad-frame-every 25 # red log lines
python backend/mock/mock_uc_server.py --port 8765 --drop-after 500     # drop popup
python backend/mock/mock_uc_server.py --port 8765 --rate-factor 0.5    # gauge reads ~1 Msps
```

| Flag | URL parameter | Default | Effect |
|---|---|---|---|
| `--host` / `--port` | — | `0.0.0.0` / `8765` | where it listens |
| `--bad-frame-every N` | `bad_frame_every` | off | every Nth frame carries 19 995 samples — the wrong *count*, but valid framing |
| `--malformed-every N` | `malformed_every` | off | every Nth frame is 79 997 bytes — not a whole number of `int32`s, so the framing itself is corrupt |
| `--drop-after N` | `drop_after` | off | closes the connection after N frames |
| `--rate-factor F` | `rate_factor` | `1.0` | scales the frame rate: `0.5` → ~1 Msps, `2.0` → ~4 Msps |
| `--seed` | — | `42` | noise seed. The three tones are fixed at 50, 210 and 700 kHz |

Three things worth knowing. Faults were already **per connection** — `--drop-after` counted
from zero on every connect, which is what makes the popup easy to demonstrate more than
once — so resolving them per connection from the URL introduces no shared state and no new
lifecycle. Pacing likewise starts when a client connects rather than when the process does,
so there is never a backlog to flush on connect. And a query parameter that is unparseable
or non-positive is logged and ignored rather than refused: a typo in a demo URL streams
cleanly instead of failing the handshake with an error the browser cannot show you.

The simulator never marks a frame as faulty. It sends a short payload and the backend
discovers it: `FrameValidator` derives `len(payload) // 4` and compares it against
`EXPECTED_SAMPLES`, and the hash is taken over the raw bytes *before* validation, so a
corrupt frame still gets a fingerprint. The red lines are real detection, not the
simulator telling the UI what to display.

Under Docker the simulator is a compose service started with a fixed command line, which is
exactly why the URL parameters exist: they are the only way to change fault injection
without stopping a container. `docker compose up` and the URL box cover every requirement
in the brief. (Changing the *defaults* under Docker still means editing the `command:` line
in `docker-compose.yml` and restarting — no rebuild, the command is not baked into the
image — but you should not need to.)

The backend rewrites `localhost:8765` to `simulator:8765` when it is itself containerised,
and that rewrite preserves the query string, so the URLs above are the same whether you run
under Docker or from a virtualenv.

`scripts/probe_simulator.py` connects to it directly and reports measured frame rate, byte
rate and the frame sizes it saw, which answers "is the simulator doing what I asked?"
without the backend in the way.

**Choosing a demo rate.** `?bad_frame_every=25` is the setting to demo with. Fault
flags survive the 30 Hz throttling boundary by design (see the fault-completeness note
below), so at `--bad-frame-every 5` every 33 ms publish interval contains a faulted frame
and the 250 ms trace tint never releases — the trace sits permanently red. That is a true
statement about a stream where one frame in five is broken, but it stops the tint carrying
information. At 25 the trace tints and clears visibly.

## Running the tests

```
pip install -r requirements.txt
pytest                          # the full suite across all phases
pytest tests/phase7_spectrum   # or any single phase
pytest --cov                    # coverage gate: 85 % on domain + application, currently 97 %
pytest -m slow                  # the long-running acceptance runs, deselected by default
ruff check .
```

Tests are grouped by the phase whose acceptance criteria they defend, so "is Phase 7
still good?" is one command rather than an act of memory:

| Directory | Defends | Notes |
|---|---|---|
| `tests/phase1_simulator/` | the uC simulator | Talks to it over a real socket with a plain `websockets` client and imports nothing from `backend/`. A failure here can only be the simulator's fault. |
| `tests/phase2_domain/` | the pure domain | No I/O at all: every clock and hasher is injected, so assertions are exact and nothing sleeps. |
| `tests/phase3_backend/` | the acquisition pipeline and the `/stream` route | Mostly in-memory fakes; `test_upstream_ws.py`, `test_stream_route.py` and `test_pipeline_soak.py` deliberately bind real sockets. |
| `tests/phase5_plot/` | time-domain waveform streaming | Talks to Flask on a real ephemeral port and verifies 8-byte header, int32 min/max pairs, and fault flag propagation. |
| `tests/phase6_rate/` | sample rate telemetry & gauge | Verifies rateAvg telemetry against nominal rate and scaled factor stream rates. |
| `tests/phase7_spectrum/` | frequency-domain spectrum streaming | Verifies spectrumAxis metadata, float32 dB payloads, multi-tone spectral peak detection (50/210/700 kHz), and domain toggling. |
| `tests/architecture/` | the dependency rule | Cross-cutting by nature — it constrains every layer, so it does not belong to one phase. |
| `tests/support/` | — | Fixtures and in-memory doubles. No assertions live here. |

Most of the suite is offline and deterministic. `test_upstream_ws.py` and
`test_pipeline_soak.py` are the deliberate exceptions: in-memory fakes are only worth
something if something else proves the real library raises what the adapter claims to
translate.

Five tests are worth knowing about by name:

- `test_matches_reference_xxh3_128_digests` — pins our digest against `xxhsum -H128` on
  fixed vectors. A wrong hash variant is the one defect in this project that would look
  completely normal on screen while invalidating every log line, so it is gated rather
  than assumed.
- `test_domain_has_no_infrastructure_imports` (and its application-layer twin) — walks
  the ASTs and fails if a web framework, socket library, or outer-layer module appears
  where pure logic belongs. The dependency rule is enforced by one test instead of an
  extra CI tool; it is also what forces `MessageCodec` and `DownstreamSink` to exist as
  ports rather than imports.
- `test_preserves_a_single_sample_spike_that_stride_decimation_would_lose` — the
  assertion that the cheap decimation shortcut was not taken. Stride sampling passes
  every other test in that file and fails this one.
- `test_every_report_survives_batching` — 100 frames in, one tick, 100 log lines out.
  The completeness half of the throttling contract.
- `test_five_minute_soak_holds_flat_memory_and_loses_nothing` — the only test that can
  catch an unbounded queue, which is invisible to every other assertion here: the frame
  rate stays perfect right up until the process dies.

## Architecture

```
┌─────────────────────────── Browser ────────────────────────────┐
│  Connection bar │ TD/FD plot │ Frame log │ Rate gauge          │   ← Phase 4+
└───────────────────────────┬────────────────────────────────────┘
             JSON control ↓ │ ↑ JSON status/log-batches + binary waveforms
                    ws://localhost:8000/stream   (flask-sock)
┌────────────────────── Backend (Flask, 1 worker) ───────────────┐
│ infrastructure/  stream_route · upstream_ws · wire             │
│ application/     AcquisitionSession · ThrottledPublisher       │
│ domain/  Hasher · Validator · RateEstimator · Decimator · FFT  │  ← pure
└───────────────────────────┬────────────────────────────────────┘
                8 MB/s raw  │  blocking recv() on the acquisition thread
                    ┌───────┴─────────┐
                    │  uC / simulator │
                    └─────────────────┘
```

### The one decision everything else follows from

> **Acquisition is complete and runs at 100 Hz. Presentation is lossy and runs at
> ~30 Hz.** A throttling boundary sits between them, and the two sides have
> deliberately different loss semantics.

- **Frame log lines are complete.** Every frame is hashed, validated and logged. They
  are *batched* — a tick sends three or four lines instead of one — but never sampled.
- **Waveforms are latest-wins.** One slot. If a second frame arrives before the first
  is drawn, the first is discarded. That is correct, not a compromise: nobody wants a
  trace from 400 ms ago, and drawing it costs the same as drawing the current one.

Confusing these two is the most likely way to get this problem wrong, in either
direction — dropping log lines loses the deliverable, and queueing waveforms builds a
backlog that grows latency until it exhausts memory.

### Threading model

| Thread | Rate | Job |
|---|---|---|
| **Acquisition** (daemon, one per session) | 100 Hz | blocking `recv()` → hash → validate → estimate rate → hand a `FrameReport` and the raw samples to the publisher. Never touches Flask. |
| **Publisher** (daemon, one per session) | ~30 Hz | wakes on a monotonic timer, drains the pending batch and the latest-wins slot under one lock, then decimates / FFTs, serialises and writes — all *outside* the lock. |
| **gunicorn gthread workers** | on demand | serve static files and the `/stream` handshake. The handler thread then parks in `receive()` reading control messages; frames never pass through it. |

All cross-thread state is four fields inside `ThrottledPublisher`, guarded by one
`threading.Lock`. Producers do nothing under the lock but append or replace; every
expensive operation happens outside it, so a stalled browser cannot apply backpressure
to acquisition. Thread-safety is auditable by reading one file, and
`tests/phase3_backend/test_publisher.py` asserts both halves of that claim.

Decimation runs on the publisher thread, not the acquisition thread. It is a
presentation concern — turning 20 000 samples into ~1 000 screen columns — and only
about 30 of every 100 frames are ever drawn, so doing it here cuts that work by ~70 %
and puts it beside the FFT, which was always going to be gated this way. The
acquisition thread is left with only what the brief makes mandatory.

### Connection states

One enum, not a scatter of booleans that can disagree. Four terminal states, because
the brief treats them differently:

| State | Meaning | UI |
|---|---|---|
| `error` | never connected | "could not connect" popup |
| `disconnected` | was connected, lost it | "connection dropped" popup, Connect re-enabled on dismissal |
| `idle` | the *user* pressed Disconnect | no popup — they know |
| `connected` | streaming | — |

Collapsing `idle` into `disconnected` would pop a "connection lost" dialog in the face
of someone who just clicked Disconnect.

### Wire protocol (browser ↔ backend)

**Browser → backend (JSON):** `{"type":"connect","url":…}` · `{"type":"disconnect"}` ·
`{"type":"setDomain","domain":"td"|"fd"|"none"}`

`setDomain` is not cosmetic: it decides whether an FFT runs thirty times a second.
While no plot is visible, waveforms are not even parked.

**Backend → browser:**

- `{"type":"status","state":…,"message":…}` — drives popups and button state.
- `{"type":"frames","items":[{n,ts,samples,hash,valid,rate}],"rateAvg":…}` — batched log
  lines, complete. `dropped` appears only if a stalled browser overran the bounded
  queue; the loss is reported rather than hidden. `malformed` appears only when true.
- `{"type":"spectrumAxis","frequenciesHz":[…]}` — the exact FD x-axis, sent once per
  geometry rather than per frame.
- **Binary waveform**, 8-byte header then payload:

  ```
  u32 frameNumber │ u8 kind (1=TD, 2=FD) │ u8 flags │ u16 pointCount
  TD: Int32Array,   interleaved [min,max] per column  (exact raw counts)
  FD: Float32Array, dB magnitude per bin
  ```

  Eight bytes so the payload starts 4-byte aligned and the browser can wrap it in a
  typed-array **view** with no copy. A 7- or 9-byte header would force a copy of every
  waveform, thirty times a second, for ever. TD stays `Int32Array` so displayed values
  are exactly the counts received — no lossy conversion inside a measurement instrument.

**Downstream bandwidth:** 2 000 × 4 B ≈ 8 kB per waveform × 30 Hz ≈ **240 kB/s** — a
**33× reduction** from 8 MB/s upstream, with no loss of visible detail.

### Why `gunicorn -w 1`

Session state — the upstream connection, the acquisition thread, the publisher — lives
in process memory. A second worker would be a second, independent copy, and which one
your browser reached would depend on which socket the OS handed the connection to. The
symptom would be an instrument that works, then inexplicably does not. The worker count
is pinned in the run command and logged at startup, and a second browser connection is
refused with a message it can display rather than silently fighting the first for one
microcontroller.

### Domain layer

Pure, dependency-free (stdlib + numpy + xxhash), fully unit-tested:

| Module | Responsibility | Decision worth noting |
|---|---|---|
| `frame.py` | `RawFrame`, `FrameReport` | Frozen dataclasses with `slots`; two clocks — wall-clock for the displayed timestamp, monotonic for rate deltas, so an NTP step can never produce a bogus rate |
| `hashing.py` | `FrameHasher` protocol, `Xxh3_128Hasher` | Hashes raw bytes before and independently of validation, so a corrupted frame is still fingerprintable |
| `validation.py` | `FrameValidator` | Distinguishes *wrong sample count* from *malformed* (length not a multiple of 4); both render red (orange-red for the latter), only the second is a framing fault |
| `rate.py` | `SampleRateEstimator` | Reports instantaneous **and** EMA-smoothed rate so the smoothing hides nothing; unmeasurable intervals report `None` rather than poisoning the EMA with infinity |
| `decimation.py` | `MinMaxDecimator` | Min/max, not stride — stride sampling aliases and drops single-sample transients, which is exactly what an operator is watching for |
| `spectrum.py` | `SpectrumAnalyzer` | Hann window with coherent-gain correction; **max**-per-bucket reduction so a narrow spur survives being squeezed from 10 001 bins into 1 000 |

## Assumptions

These are the assumptions made where the brief left something unspecified.

### Protocol & data
1. Expected frame size is 20 000 samples, configurable via `EXPECTED_SAMPLES`. The brief's
   requirement that mismatched frames log in red implies mismatches can occur.
2. Sample count is derived as `len(payload) // 4`. A payload whose length is not a multiple
   of 4 is a malformed frame: logged red with the truncated count and a `malformed` flag. We
   do not attempt to realign or reassemble across frames. Both faults render red, as the
   brief requires — a malformed payload also reports a sample count that differs from the
   expected one — but malformed lines are hue-shifted to orange-red so the two can be told
   apart without reading the count. Red still means "this frame is wrong"; the hue says
   which way.
3. The hash is computed over the raw bytes exactly as received, before and independently of
   validation. A short frame still gets a hash — otherwise a corrupted frame would be
   undiagnosable.
4. One WebSocket message = one frame, per the brief. WebSocket fragmentation is handled by
   the library layer, which delivers whole messages.
5. Text messages from the uC are ignored (counted, not logged as frames). Only binary
   messages are frames.
6. Frame numbering starts at 1 and is assigned by our client, monotonically, per connection
   session. Reconnecting resets to 1. The uC sends no frame IDs.
7. No gap/loss detection. The brief guarantees that on a healthy connection data reaches the
   OS, and WebSocket over TCP is ordered and reliable.
8. `ws://` only, no `wss://`, no auth — explicitly out of scope per the brief.

### Semantics
9. Timestamps are PC-side receipt time, local timezone, `YYYY-MM-DD HH:MM:SS` as in the
   brief's example.
10. Sample rate is estimated per frame as `samples_in_frame / (t_n − t_{n-1})`. The first
    frame of a session has no predecessor and reports `null` (no rate line, no bogus value
    computed from time-since-connect).
11. The gauge shows a smoothed rate (EMA, α = 0.1, applied to the arrival *interval* — see
    [One measurement, three statistics](#one-measurement-three-statistics)); the log shows
    the instantaneous per-frame value. Both are shown so nothing is hidden.
    The gauge **reports** the rate and does not **grade** it: it has no tolerance bands and
    no pass/fail colouring. The brief asks for "the estimated sample rate (measured at every
    frame) in an output text box of a graphical gauge" and specifies no tolerance, so any
    threshold would have been invented here — and an invented one measured the transport,
    not the instrument. It also reserves red for a specific meaning (a frame whose sample
    count is wrong), which a red gauge on a healthy stream would have competed with. The
    deviation-from-nominal percentage is still displayed, as a number.
12. Sample values are raw ADC counts. No calibration to volts — no scale factor was provided.

### Scope & lifecycle
13. A uC simulator is part of the deliverable, since the graders have no microcontroller. It
    includes injectable fault modes as the only way to demonstrate the red-line and
    connection-drop requirements: `--bad-frame-every` (a whole number of samples, but the
    wrong number), `--malformed-every` (a length that is not a multiple of 4, so the
    framing itself is corrupt — a different fault, reported separately), `--drop-after`,
    and `--rate-factor`. Each is also settable per connection as a query parameter on the
    WebSocket URL, so every mandatory failure mode can be demonstrated from the URL box the
    brief already requires, without restarting the simulator or editing a compose file. The
    simulator is a test fixture, so accepting instructions from the client costs nothing —
    the real uC would ignore the query string, and the backend never reads it.
14. Reconnection is manual, never automatic. The brief specifies a popup on drop and the
    Connect button re-enabled once the popup is closed — auto-reconnect would contradict
    that by re-arming the connection without the user's action.
15. The frame log is bounded to the most recent 5 000 lines in memory and ~500 DOM nodes,
    recycled.
16. Single client, single server, per brief. The backend runs one gunicorn worker
    (`-w 1 -k gthread --threads 4`) so session state lives in one process; a second browser
    connection is rejected with a clear message.
17. Browser ↔ backend runs over loopback (`ws://localhost:8000/stream`). Backend ↔ uC is the
    LAN hop.

## Performance notes

"Is Python fast enough for 8 MB/s?" is the first question this design invites, so here
is the arithmetic. The numbers that matter:

| Quantity | Value |
|---|---|
| Sample rate | 2 000 000 samples/s |
| Frame size | 20 000 samples = 80 000 bytes |
| Frame rate | 100 frames/s |
| Byte rate | 8 MB/s |
| **Frame period** | **10 ms** |

Per-frame cost on the acquisition thread, against that 10 ms deadline:

| Step | Cost |
|---|---|
| Socket `recv`, 80 kB | ~20 µs |
| `xxh128` of 80 kB (~10 GB/s, **releases the GIL**) | ~8 µs |
| Length validation + rate estimate | ~1 µs |
| `np.frombuffer` view (zero-copy) | ~1 µs |
| **Total** | **≈ 30 µs — 0.3 % of the budget** |

Decimation (~30 µs) and `rfft` (~300 µs) are not in that column: they run on the
publisher thread, at ≤30 Hz, and only for frames that are actually drawn.

### The C decimator, and why it is optional

`backend/native/minmax.c` is the same min/max reduction as the numpy path — ~40 lines,
bound with `ctypes`, loaded at import. The Dockerfile compiles it; `decimation.py` falls
back to numpy when the library is absent, and a test hides the `.so` to prove that branch
runs. Both paths produce **byte-identical** output, held there by a property test over
random buffers at every awkward length the fault injector can produce (19 995, 19 999,
fewer samples than columns).

| Path | Per frame, 20 000 samples → 1 000 columns |
|---|---|
| C via `ctypes` | **22.8 µs** |
| numpy | 59.1 µs |
| Ratio | 2.6× |

Read that table honestly: both are a rounding error against a 10 ms deadline, so the C
path is not what makes this work. It is here because the Python/compiled-library boundary
is worth demonstrating, and 2.6× is what the measurement says rather than what a
benchmark was arranged to say. Drop the build step and the app loses 36 µs a frame and
nothing else — which is the property that makes it safe to ship.

### One measurement, three statistics

The sample rate is estimated once per frame and reported three ways, because three
consumers need different things from the same number:

| Statistic | Shown on | Why that one |
|---|---|---|
| Instantaneous | each log line | the honest per-frame value the brief asks for |
| EMA (α = 0.1) of the **interval** | the gauge | readable; responds to a real change in ~10 frames |
| Session mean | the FD frequency axis | immune to a single stall-and-burst |

**Average the interval, not the rate.** The rate is `1/Δt`, and the reciprocal is convex,
so the two directions of the same jitter are not weighted alike. A frame arriving 6 µs
after its predecessor — ordinary TCP coalescing — reports 3.3 Gsps, while the gap that
compensates for it can only ever pull the estimate toward zero. Averaging those unequal
excursions biases the result upward by roughly `(σ/Δt)²` and lets one coalesced burst
dominate the EMA for its whole time constant.

That is not theoretical. Measured over 20 s on loopback — the friendliest possible
transport, against a simulator pacing a flat 100.0 Hz:

| Smoothing | p50 \|dev\| | p95 \|dev\| | max \|dev\| | > 5 % from nominal |
|---|---|---|---|---|
| EMA of the rate | 0.76 % | 3.55 % | **16 375 %** | 4.5 % of the time |
| EMA of the interval | 0.52 % | 1.05 % | 17 % | 0.3 % of the time |
| Session mean | 0.01 % | 0.07 % | 0.15 % | never |

Smoothing `Δt / samples` and inverting once at the end removes the asymmetry at its
source, because the averaging now happens in the domain where the noise actually lives.
Same α, same "measured at every frame" contract, same class — no display trick, and no
threshold tuned until the symptom went away. The biased number was wrong; this one is not.

The session mean survives as the third statistic because even an unbiased ten-frame EMA
still swings by whole percent under a stall-and-burst, and a frequency axis is `fs/2`
wide — any error in `fs` walks *every* peak by the same proportion while the user is
looking at it. Total samples over total elapsed time cannot move like that: a burst
arriving early is cancelled by the gap before it. It still tracks a genuine rate change
(`?rate_factor=0.5`) over seconds rather than frames, which is the right time constant for
a property of the hardware rather than of the link.

The per-frame value is displayed beside the smoothed one throughout, so nothing is hidden.

The point is not that Python is fast. It is that at 100 Hz the *Python* work per frame
is a handful of dictionary and attribute operations, while the per-sample work happens
in C inside numpy and xxhash — both with the GIL released, so the 100 Hz thread and the
30 Hz publisher genuinely overlap rather than merely interleave. Headroom is ~300×,
which is why a single Flask process with two worker threads is not merely adequate here
but generously so.

**Measured, not asserted.** `tests/phase3_backend/test_pipeline_soak.py` runs the real simulator into
the real pipeline over a real socket:

- 5-minute soak: **30 026 frames received, 30 026 reported, zero gaps, zero drops.**
- Acquisition held ~100 Hz while presentation held ~30 Hz — the rate-decoupling claim,
  measured end to end rather than argued.
- RSS after warm-up: **flat** (≈12 KiB of movement across 13 000 frames in the trace
  run). This is the test that catches an unbounded queue, which is otherwise invisible:
  the frame rate stays perfect right up until the process dies.

Run it with `pytest -m slow` (`SOAK_SECONDS=60` for a quicker pass).

**On WebGL** — named in the job description, and deliberately not used. 2 000 decimated
columns per frame at 30 Hz sits far below the point where Canvas 2D struggles; uPlot
handles it with headroom. WebGL becomes the right answer when the requirement changes to
many simultaneous traces at full rate without decimation. Knowing where that threshold
sits is worth more than crossing it unnecessarily.

## License

Submitted as a software challenge deliverable.

## Frontend notes

Vanilla ES modules served by Flask from the same origin as `/stream` — no bundler, no
framework, no build step. Eight modules:
- `wsClient`: socket lifecycle, text JSON parsing, binary waveform decoding, domain switching (`setDomain`), and background tab visibility gating.
- `stateMachine`: connection states (`idle`, `connecting`, `streaming`, `disconnected`, `error`, `backend_offline`) driving control inputs and status badges.
- `frameLog`: ring-buffered frame log (5 000 in memory, 500 in DOM) with auto-scroll and red fault line styling.
- `modal`: native `<dialog>` controller for connection and runtime error dialogs.
- `plotTD`: uPlot time-domain renderer. Preallocated buffers, an rAF latest-wins render loop, asymmetric-hysteresis autoscale, 2D drag-zoom, trace hold, and a 250 ms latched fault tint. Scales are driven by uPlot `range` callbacks so each frame costs exactly one redraw.
- `plotFD`: the frequency-domain counterpart (stretch S1/S4) — Hann-windowed spectrum, backend-supplied frequency axis, live peak readout, max hold.
- `rateGauge`: precision SVG radial arc gauge with dynamic nominal baseline and percentage deviation. Two visual states, idle and streaming; no pass/fail colouring.
- `app`: dependency injection, DOM binding, and `?debug=1` performance HUD.

### Key Architectural & Design Decisions

- **Lossy Waveform Presentation vs. Lossless Log Audit Trail**: The human eye cannot resolve 100 waveform redraws/second (causing 100% CPU lockup and severe browser lag). Waveforms are throttled at the backend boundary to 30 Hz (and decoupled on the frontend via an rAF latest-wins slot), while 100% of frame reports are delivered in batched JSON payloads so that every single acquired frame is logged with zero omissions.
- **The log is a `<div>` list, not a `<textarea>`.** The brief asks for a "textbox" and separately requires mismatched frames to render red. A `<textarea>` cannot colour individual lines, so the two requirements conflict; the colour requirement wins, because it carries information and the element choice does not.
- **The line is composed from backend fields, never re-derived.** `ts` is the backend's receipt time rendered verbatim (assumption #9) and the sample count is printed bare — no locale separators — so the line matches the brief's example character for character: `[2026-08-20 18:00:00]: Frame 1 | 20000 | e2966f42…`.
- **Preallocated Buffers & Zero-Copy Alignment**: Binary waveforms arrive as 8-byte header (`<IBBH`) + int32 min/max pairs. The frontend creates a typed `new Int32Array(event.data, 8, pointCount * 2)` view directly over the aligned `ArrayBuffer` and de-interleaves in-place into preallocated typed arrays without GC churn.
- **Exact Span for Truncated Frames**: The frontend caches `frameNumber -> sample_count` from incoming frame log batches so that truncated/malformed frames map to their exact true sample span rather than stretching to fill nominal width.
- **2D Drag-Zoom with Autoscale Hysteresis**: Oscilloscope amplitude/time zoom is enabled via uPlot 2D box selection (`drag: { x: true, y: true }`). In autoscale mode, expansion is instantaneous on new peaks while contraction uses a 1.0s decay window to prevent dizzying jitter.
- **Rate Gauge**: Configured via `nominalRateHz` from the backend handshake, showing the smoothed rate, the instantaneous rate and the percentage deviation from nominal. It reports; it does not grade — see assumption #11 for why the tolerance bands it used to carry were removed rather than retuned. One class on the gauge root drives arc, readout and badge, so they cannot disagree. The arc carries **no** `stroke-dashoffset` transition: the offset is rewritten every 33 ms, and a transition longer than the update interval never arrives.
- **Fault flags are complete even though waveforms are not**: waveforms are latest-wins, and because acquisition (100 Hz) and publication (30 Hz) are both monotonic-paced, the surviving frame cycles through a *fixed* set of residues rather than a random one — measured, that set never contained a faulted frame. The backend therefore OR-s every fault seen during a tick interval onto whichever frame is drawn. The trace says "something in this 33 ms was wrong"; the log says exactly which frame.
- **Export (stretch S3)**: the retained ring buffer is already the export — `Export CSV` writes the last 5 000 frame reports (`frame,timestamp,samples,hash,valid,malformed,estimated_rate_hz`) via a Blob. The complete log is the deliverable, so it is what is exported; re-exporting a decimated envelope would be exporting the plot rather than the data.
- **Visibility Gating**: Background tabs automatically request `setDomain('none')` to halt binary waveform serialization on the backend and reduce browser draw CPU to zero.

