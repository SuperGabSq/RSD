/**
 * Time-domain waveform plot (uPlot).
 *
 * Four things in here are load-bearing and easy to get subtly wrong:
 *
 * 1. **One redraw per frame.** uPlot redraws on `setScale` *and* on `setData`, so
 *    driving the axes with `setScale(x) + setScale(y) + setData()` costs three full
 *    canvas passes per waveform -- 90 draws/s for a 30 Hz stream. The scales are
 *    therefore driven by `range` callbacks that uPlot invokes from inside the single
 *    `setData()` commit.
 * 2. **Autoscale expands instantly and shrinks slowly.** Symmetric autoscale on noisy
 *    ADC counts makes the trace breathe vertically, which reads as exactly the
 *    unsmoothness the brief grades.
 * 3. **`setData(data, false)` while zoomed.** `resetScales = true` re-runs autoscaling,
 *    so a user's drag-zoom would be undone 33 ms after they released the mouse.
 * 4. **Every series array is the same length as `x`.** uPlot indexes them in lockstep;
 *    a short array is not "no data", it is undefined values inside the draw loop.
 */

const FLAG_INVALID = 0x01;
const FLAG_MALFORMED = 0x02;
const FLAG_ANY_FAULT = FLAG_INVALID | FLAG_MALFORMED;

// How long a fault stays visible after the last flagged waveform. The backend already
// guarantees the flag survives the throttling boundary; this is about human eyes, not
// about the protocol -- a single-frame tint at 30 Hz is a 33 ms flash nobody sees.
const FAULT_LATCH_MS = 250;

// Autoscale hysteresis: expand on the frame that needs it, shrink only after the signal
// has stayed smaller than the current window for this long.
const DECAY_HOLD_MS = 1000;
const DECAY_RATE = 0.2;
const MARGIN_FRACTION = 0.08;

const COLOR_NORMAL_STROKE = '#38bdf8';
const COLOR_FLAGGED_STROKE = '#f43f5e';
const COLOR_HOLD_STROKE = '#64748b';

const COLOR_NORMAL_FILL = 'rgba(56, 189, 248, 0.18)';
const COLOR_FLAGGED_FILL = 'rgba(244, 63, 94, 0.22)';
const COLOR_HOLD_FILL = 'rgba(100, 116, 139, 0.12)';

// Series indices, named because `data[3]` at the call site is how the hold band and the
// live band get swapped by accident.
const S_HOLD_MAX = 1;
const S_HOLD_MIN = 2;
const S_MAX = 3;
const S_MIN = 4;

export class TimeDomainPlot {
  /**
   * @param {HTMLElement} container
   */
  constructor(container) {
    this.container = container;
    this.uplot = null;

    // Geometry, corrected by the backend's `config` message.
    this.targetColumns = 1000;
    this.expectedSamples = 20000;
    this.nominalRateHz = 2000000;
    this.rateAvgHz = 2000000;

    this._allocateBuffers(this.targetColumns);

    // frameNumber -> true sample count, so a truncated frame draws over its real span
    // instead of being stretched across the full width. Bounded: at 100 frames/s this
    // is two seconds of history, and the waveform for a frame arrives in the same tick
    // as its log line.
    this.frameSampleCounts = new Map();

    this.holdEnabled = false;

    // Autoscale state. `null` means "not yet seeded"; the first frame sets the window
    // rather than starting from a guess and animating towards the truth.
    this.autoScale = true;
    this.yMin = null;
    this.yMax = null;
    this.lastExpandMs = 0;
    this.xSpan = this.expectedSamples;

    this.lastFlaggedMs = -Infinity;

    // Latest-wins slot, drained by the rAF loop. The socket callback never draws.
    this.pendingSlot = null;
    this.isDirty = false;
    this.rafId = null;
    this.pendingResize = null;
    this.lastWidth = 0;
    this.lastHeight = 0;

    // `?debug=1` telemetry.
    this.drawsPerSec = 0;
    this.drawCountWindow = 0;
    this.lastFpsWindowMs = performance.now();
    this.lastDecodeUs = 0;

    this._initPlot();
    this._setupResizeObserver();
    this._startRafLoop();
  }

  // ------------------------------------------------------------------- buffers

  _allocateBuffers(capacity) {
    this.capacity = capacity;
    this.xData = new Float64Array(capacity);
    // Float64 rather than Int32 for the y buffers: uPlot needs a "no value here" and
    // NaN is it. Every int32 ADC count is exact in a float64, so nothing is rounded.
    this.minData = new Float64Array(capacity);
    this.maxData = new Float64Array(capacity);
    this.holdMin = new Float64Array(capacity);
    this.holdMax = new Float64Array(capacity);
    this.blank = new Float64Array(capacity);
    this.holdMin.fill(NaN);
    this.holdMax.fill(NaN);
    this.blank.fill(NaN);
    this.cachedPointCount = 0;
    this.cachedTotalSamples = 0;
  }

  _growBuffersIfNeeded(required) {
    if (required > this.capacity) {
      this._allocateBuffers(Math.max(required, this.capacity * 2));
    }
  }

  // -------------------------------------------------------------------- config

  setConfig(config) {
    if (!config) return;
    if (config.targetColumns > 0) {
      this.targetColumns = config.targetColumns;
      this._growBuffersIfNeeded(config.targetColumns);
    }
    if (config.expectedSamples > 0) {
      this.expectedSamples = config.expectedSamples;
      this.xSpan = config.expectedSamples;
    }
    if (config.nominalRateHz > 0) {
      this.nominalRateHz = config.nominalRateHz;
      this.rateAvgHz = config.nominalRateHz;
    }
  }

  /** The derived time axis follows the *measured* rate, not the nominal one: under
   *  `--rate-factor 0.5` a 20 000-sample frame is 20 ms of signal, not 10. */
  setRateAvg(rateAvgHz) {
    if (typeof rateAvgHz === 'number' && rateAvgHz > 0) {
      this.rateAvgHz = rateAvgHz;
    }
  }

  cacheFrameSampleCount(frameNumber, sampleCount) {
    this.frameSampleCounts.set(frameNumber, sampleCount);
    if (this.frameSampleCounts.size > 200) {
      this.frameSampleCounts.delete(this.frameSampleCounts.keys().next().value);
    }
  }

  // ----------------------------------------------------------------- plot setup

  _isFaulted() {
    return performance.now() - this.lastFlaggedMs < FAULT_LATCH_MS;
  }

  _initPlot() {
    if (!window.uPlot) {
      console.warn('uPlot is not loaded; the time-domain plot will stay blank');
      return;
    }

    const rect = this.container.getBoundingClientRect();
    const liveStroke = () => (this._isFaulted() ? COLOR_FLAGGED_STROKE : COLOR_NORMAL_STROKE);

    const opts = {
      width: Math.max(rect.width || 600, 300),
      height: Math.max(rect.height || 340, 200),
      cursor: {
        // setScale: false because the zoom is applied by the hook below, which also
        // has to record that the user has taken manual control of the axes.
        drag: { x: true, y: true, setScale: false },
        points: { show: false },
      },
      hooks: {
        setSelect: [
          (u) => {
            if (u.select.width <= 5 || u.select.height <= 5) return;
            const minX = u.posToVal(u.select.left, 'x');
            const maxX = u.posToVal(u.select.left + u.select.width, 'x');
            const minY = u.posToVal(u.select.top + u.select.height, 'y');
            const maxY = u.posToVal(u.select.top, 'y');
            this.autoScale = false;
            u.setScale('x', { min: minX, max: maxX });
            u.setScale('y', { min: minY, max: maxY });
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          },
        ],
      },
      scales: {
        // These callbacks are the whole reason there is one redraw per frame instead of
        // three: uPlot calls them from inside the setData() commit.
        x: { time: false, range: () => [0, this.xSpan] },
        y: { range: () => [this.yMin ?? -1, this.yMax ?? 1] },
      },
      axes: [
        {
          label: 'Sample index (time derived from measured rate)',
          stroke: '#94a3b8',
          grid: { stroke: 'rgba(51, 65, 85, 0.4)', width: 1 },
          ticks: { stroke: 'rgba(51, 65, 85, 0.6)', width: 1 },
          font: '11px JetBrains Mono, monospace',
          // Each label carries two numbers, so it needs roughly twice the room uPlot
          // allows by default -- without this the ticks run into each other and the
          // axis becomes unreadable at exactly the width the app is used at.
          space: 150,
          values: (u, splits) => {
            const rate = this.rateAvgHz || this.nominalRateHz;
            return splits.map((s) => `${Math.round(s)} · ${((s / rate) * 1000).toFixed(2)} ms`);
          },
        },
        {
          label: 'ADC counts',
          stroke: '#94a3b8',
          grid: { stroke: 'rgba(51, 65, 85, 0.4)', width: 1 },
          ticks: { stroke: 'rgba(51, 65, 85, 0.6)', width: 1 },
          font: '11px JetBrains Mono, monospace',
          values: (u, splits) => splits.map((v) => Math.round(v).toLocaleString()),
        },
      ],
      series: [
        {},
        { label: 'Hold max', show: false, stroke: COLOR_HOLD_STROKE, width: 1 },
        { label: 'Hold min', show: false, stroke: COLOR_HOLD_STROKE, width: 1 },
        { label: 'Max', stroke: liveStroke, width: 1.5 },
        { label: 'Min', stroke: liveStroke, width: 1.5 },
      ],
      bands: [
        { series: [S_HOLD_MAX, S_HOLD_MIN], fill: () => COLOR_HOLD_FILL },
        {
          series: [S_MAX, S_MIN],
          fill: () => (this._isFaulted() ? COLOR_FLAGGED_FILL : COLOR_NORMAL_FILL),
        },
      ],
    };

    this.container.innerHTML = '';
    this.uplot = new window.uPlot(opts, this._emptyData(), this.container);
  }

  /** Same length on every series, which is what uPlot requires, and all-NaN, which is
   *  how uPlot spells "nothing measured yet". */
  _emptyData() {
    const x = new Float64Array(2);
    const y = new Float64Array(2).fill(NaN);
    return [x, y, y, y, y];
  }

  _setupResizeObserver() {
    if (typeof ResizeObserver === 'undefined') return;
    // A window drag fires this per pixel and setSize() is a full re-layout, so it is
    // coalesced through the same rAF loop the waveform uses.
    //
    // The rounding is not cosmetic. uPlot's root element is the plot height plus its
    // legend, so if the container is ever sized by its content, observing the container
    // and calling setSize() with what it reports makes the two chase each other upward
    // for ever. The CSS takes the container out of the height calculation; this rounds
    // sub-pixel noise away and drops no-op resizes, so the loop cannot restart from a
    // future stylesheet edit.
    const observer = new ResizeObserver((entries) => {
      const rect = entries[entries.length - 1].contentRect;
      const width = Math.round(rect.width);
      const height = Math.round(rect.height);
      if (width <= 0 || height <= 0) return;
      if (width === this.lastWidth && height === this.lastHeight) return;
      this.lastWidth = width;
      this.lastHeight = height;
      this.pendingResize = { width, height };
      this.isDirty = true;
    });
    observer.observe(this.container);
  }

  // -------------------------------------------------------------------- controls

  /** Called from the socket callback. Overwrite, never queue. */
  offerWaveform(frameNumber, flags, pointCount, payload) {
    this.pendingSlot = { frameNumber, flags, pointCount, payload };
    this.isDirty = true;
  }

  setTraceHold(enabled) {
    this.holdEnabled = enabled;
    if (!enabled) this.clearTraceHold();
    if (this.uplot) {
      this.uplot.setSeries(S_HOLD_MAX, { show: enabled });
      this.uplot.setSeries(S_HOLD_MIN, { show: enabled });
    }
    this.isDirty = true;
  }

  clearTraceHold() {
    this.holdMin.fill(NaN);
    this.holdMax.fill(NaN);
    this.isDirty = true;
  }

  /** Hands the axes back to the autoscaler. */
  resetZoom() {
    this.autoScale = true;
    this.yMin = null;
    this.yMax = null;
    this.lastExpandMs = 0;
    this.isDirty = true;
    // Deliberately does not touch the scales. The next frame reseeds them through the
    // range callbacks; `setScale(key, {min: null, max: null})` is not a uPlot reset, it
    // is a request to render against an undefined range, and it blanks the plot.
  }

  // ----------------------------------------------------------------- render loop

  _startRafLoop() {
    const step = (now) => {
      if (this.isDirty) this._render(now);
      this.rafId = requestAnimationFrame(step);
    };
    this.rafId = requestAnimationFrame(step);
  }

  _render(now) {
    if (!this.uplot) return;
    // A background tab stops firing rAF on its own; this is the other case -- the FD
    // tab is in front and this container is hidden, where setSize() and setData() would
    // compute a layout against a 0x0 box. The slot and the dirty flag are both left
    // alone so the first frame after switching back draws immediately.
    if (this.container.hidden || this.container.offsetParent === null) return;

    if (this.pendingResize) {
      const { width, height } = this.pendingResize;
      this.pendingResize = null;
      this.uplot.setSize({ width, height });
    }

    const slot = this.pendingSlot;
    if (!slot) {
      this.isDirty = false;
      return;
    }
    this.pendingSlot = null;
    this.isDirty = false;

    const t0 = performance.now();
    const { frameNumber, flags, pointCount, payload } = slot;
    this._growBuffersIfNeeded(pointCount);

    if (flags & FLAG_ANY_FAULT) this.lastFlaggedMs = now;

    // The exact sample count, so a 19 995-sample frame draws over 19 995 samples. The
    // fallback only runs if a binary waveform outran its own log line.
    const totalSamples =
      this.frameSampleCounts.get(frameNumber) ??
      pointCount * Math.max(1, Math.round(this.expectedSamples / this.targetColumns));

    if (this.cachedPointCount !== pointCount || this.cachedTotalSamples !== totalSamples) {
      const step = totalSamples / pointCount;
      for (let i = 0; i < pointCount; i++) this.xData[i] = i * step;
      this.cachedPointCount = pointCount;
      this.cachedTotalSamples = totalSamples;
    }
    this.xSpan = totalSamples;

    // De-interleave [min, max] pairs into the preallocated columns, tracking the frame
    // envelope and the hold envelope in the same pass.
    let frameMin = Infinity;
    let frameMax = -Infinity;
    const hold = this.holdEnabled;

    for (let i = 0; i < pointCount; i++) {
      const lo = payload[i * 2];
      const hi = payload[i * 2 + 1];
      this.minData[i] = lo;
      this.maxData[i] = hi;
      if (lo < frameMin) frameMin = lo;
      if (hi > frameMax) frameMax = hi;
      if (hold) {
        // Negated comparisons so the NaN seed is replaced on the first pass.
        if (!(this.holdMin[i] <= lo)) this.holdMin[i] = lo;
        if (!(this.holdMax[i] >= hi)) this.holdMax[i] = hi;
      }
    }

    if (this.autoScale) this._updateAutoScale(frameMin, frameMax, now);

    const x = this.xData.subarray(0, pointCount);
    const max = this.maxData.subarray(0, pointCount);
    const min = this.minData.subarray(0, pointCount);
    const blank = this.blank.subarray(0, pointCount);
    const holdMax = hold ? this.holdMax.subarray(0, pointCount) : blank;
    const holdMin = hold ? this.holdMin.subarray(0, pointCount) : blank;

    // One commit, one redraw. `resetScales` is the autoscale switch: true lets the range
    // callbacks above apply the window we just computed, false preserves a drag-zoom.
    this.uplot.setData([x, holdMax, holdMin, max, min], this.autoScale);

    this.lastDecodeUs = Math.round((performance.now() - t0) * 1000);
    this.drawCountWindow++;
    if (now - this.lastFpsWindowMs >= 1000) {
      this.drawsPerSec = this.drawCountWindow;
      this.drawCountWindow = 0;
      this.lastFpsWindowMs = now;
    }
  }

  /**
   * Asymmetric hysteresis: a spike widens the window on the frame it arrives, but the
   * window only closes again after DECAY_HOLD_MS with nothing pushing it open. A
   * symmetric autoscaler tracks the noise floor and the trace visibly breathes.
   */
  _updateAutoScale(frameMin, frameMax, now) {
    if (!Number.isFinite(frameMin) || !Number.isFinite(frameMax)) return;

    const margin = Math.max((frameMax - frameMin) * MARGIN_FRACTION, 1);
    const targetMin = frameMin - margin;
    const targetMax = frameMax + margin;

    if (this.yMin === null || this.yMax === null) {
      // First frame of a session: adopt the real window rather than animating out to it
      // from an arbitrary guess.
      this.yMin = targetMin;
      this.yMax = targetMax;
      this.lastExpandMs = now;
      return;
    }

    let expanded = false;
    if (targetMin < this.yMin) {
      this.yMin = targetMin;
      expanded = true;
    }
    if (targetMax > this.yMax) {
      this.yMax = targetMax;
      expanded = true;
    }
    if (expanded) {
      this.lastExpandMs = now;
      return;
    }

    if (now - this.lastExpandMs > DECAY_HOLD_MS) {
      this.yMin += (targetMin - this.yMin) * DECAY_RATE;
      this.yMax += (targetMax - this.yMax) * DECAY_RATE;
    }
  }

  // ------------------------------------------------------------------- teardown

  /** Called when the microcontroller link drops: the old trace is not the current state
   *  of anything, so it goes. */
  reset() {
    this.pendingSlot = null;
    this.frameSampleCounts.clear();
    this.lastFlaggedMs = -Infinity;
    this.clearTraceHold();
    this.resetZoom();
    if (this.uplot) this.uplot.setData(this._emptyData(), true);
    this.isDirty = false;
  }

  destroy() {
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    if (this.uplot) {
      this.uplot.destroy();
      this.uplot = null;
    }
  }
}
