# SignalScope

Real-time visualisation of a 2 Msps signal streamed over WebSocket from a microcontroller.
Built for the TII-DERC Senior Software Engineer software challenge.

> **Status: work in progress.** This README is filled in as each phase lands; see
> `docs/PLAN.md` (not included in the repo — internal planning doc) for the full
> architecture rationale. Sections below are placeholders until their phase is done.

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

## Running the tests

```
pip install -r requirements.txt
pytest --cov          # coverage gate: 85 % on backend/domain, currently 100 %
ruff check .
```

The suite is pure and offline — no sockets, no sleeps, no clock reads — because every
clock and hasher is injected. It runs in well under a second, which is what makes it
usable as a change-by-change gate rather than a pre-commit ritual.

Two tests are worth knowing about by name:

- `test_matches_reference_xxh3_128_digests` — pins our digest against `xxhsum -H128` on
  fixed vectors. A wrong hash variant is the one defect in this project that would look
  completely normal on screen while invalidating every log line, so it is gated rather
  than assumed.
- `test_domain_has_no_infrastructure_imports` — walks the domain's ASTs and fails if a
  web framework, socket library, or outer-layer module appears where pure logic belongs.
  The dependency rule is enforced by one test instead of an extra CI tool.

## Architecture

_TODO — filled in with Phase 3+: threading model, wire protocol, diagram._

### Domain layer (Phase 2, complete)

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
    includes injectable fault modes (`--bad-frame-every`, `--drop-after`, `--rate-factor`) as
    the only way to demonstrate the red-line and connection-drop requirements.
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

_TODO — filled in with Phase 3: per-frame budget, threading/GIL argument, downstream
bandwidth reduction._

## License

Submitted as a software challenge deliverable.
