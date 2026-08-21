# SignalScope

Real-time visualisation of a 2 Msps signal streamed over WebSocket from a microcontroller.
Built for the TII-DERC Senior Software Engineer software challenge.

> **Status: first demoable build (Phase 4).** Domain, acquisition pipeline, throttling
> boundary, WebSocket endpoint, configuration and the browser shell — connection bar,
> frame log, modals — are all in. The waveform plot lands in Phase 5 and the rate gauge
> in Phase 6; both have placeholders on the page.

## Quickstart (Docker)

```
docker compose -f docker/docker-compose.yml up --build
```

Then open `http://localhost:8000`.

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

## Running the tests

```
pip install -r requirements.txt
pytest                     # 191 tests, ~105 s
pytest tests/phase2_domain # or any single phase
pytest --cov               # coverage gate: 85 % on domain + application, currently 97 %
pytest -m slow             # the long-running acceptance runs, deselected by default
ruff check .
```

Tests are grouped by the phase whose acceptance criteria they defend, so "is Phase 2
still good?" is one command rather than an act of memory:

| Directory | Defends | Notes |
|---|---|---|
| `tests/phase1_simulator/` | the uC simulator | Talks to it over a real socket with a plain `websockets` client and imports nothing from `backend/`. A failure here can only be the simulator's fault. |
| `tests/phase2_domain/` | the pure domain | No I/O at all: every clock and hasher is injected, so assertions are exact and nothing sleeps. |
| `tests/phase3_backend/` | the acquisition pipeline and the `/stream` route | Mostly in-memory fakes; `test_upstream_ws.py`, `test_stream_route.py` and `test_pipeline_soak.py` deliberately bind real sockets. |
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
| `validation.py` | `FrameValidator` | Distinguishes *wrong sample count* from *malformed* (length not a multiple of 4); both render red, only the second is a framing fault |
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
   do not attempt to realign or reassemble across frames.
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
11. The gauge shows a smoothed rate (EMA, α = 0.1); the log shows the instantaneous per-frame
    value. Both are shown so nothing is hidden.
12. Sample values are raw ADC counts. No calibration to volts — no scale factor was provided.

### Scope & lifecycle
13. A uC simulator is part of the deliverable, since the graders have no microcontroller. It
    includes injectable fault modes as the only way to demonstrate the red-line and
    connection-drop requirements: `--bad-frame-every` (a whole number of samples, but the
    wrong number), `--malformed-every` (a length that is not a multiple of 4, so the
    framing itself is corrupt — a different fault, reported separately), `--drop-after`,
    and `--rate-factor`.
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
framework, no build step. Five modules: `wsClient` (socket and message dispatch),
`stateMachine` (connection states → button and status-dot bindings), `frameLog`
(bounded rendering), `modal` (native `<dialog>`), and `app` (wiring).

Three decisions worth stating, because each is a deviation or a constraint the brief
implies rather than states:

- **The log is a `<div>` list, not a `<textarea>`.** The brief asks for a "textbox" and
  separately requires mismatched frames to render red. A `<textarea>` cannot colour
  individual lines, so the two requirements conflict; the colour requirement wins,
  because it carries information and the element choice does not.
- **The line is composed from backend fields, never re-derived.** `ts` is the backend's
  receipt time rendered verbatim (assumption #9) and the sample count is printed bare —
  no locale separators — so the line matches the brief's example character for
  character: `[2026-08-20 18:00:00]: Frame 1 | 20000 | e2966f42…`.
- **Two sockets, two failure modes.** The browser↔backend socket dying is a different
  event from the microcontroller dropping, and gets its own terminal state
  (`backend_offline`) rather than reusing `error`. There is no auto-reconnect on either
  hop — assumption #14 — and once the backend socket is gone the Connect button stays
  disabled, because re-enabling it would only produce a second failure.

The log keeps 5 000 lines in a fixed-capacity ring buffer behind a 500-node DOM cap
(assumption #15). Auto-scroll pauses the moment the operator scrolls up and resumes when
they return to the bottom; at 100 lines/s, reading history is impossible otherwise.

There is deliberately no JavaScript test framework — under this budget the test hours go
where the correctness risk is, which is the backend. Phase 4's acceptance is the manual
checklist plus `tests/phase3_backend/test_stream_route.py`, which covers the route wiring
the UI sits on.
