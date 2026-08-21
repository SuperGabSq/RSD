/**
 * Frequency Domain (Spectrum) Plot Controller using vendored uPlot.
 *
 * Implements:
 * - Preallocated Float32Array spectrum buffers with on-demand expansion.
 * - rAF latest-wins render loop decoupled from the 30 Hz socket callback.
 * - Exact backend-driven spectrum axis (frequenciesHz) with frequency formatting (kHz / MHz).
 * - dB magnitude vertical scale with autoscaling hysteresis and 2D drag-zoom.
 * - Real-time peak frequency & magnitude tracking with callback.
 * - Max Hold running peak envelope with Clear action.
 * - Trace tinting with 250 ms latch on invalid/malformed frames.
 * - Coalesced ResizeObserver through rAF.
 */

const FLAG_INVALID = 0x01;
const FLAG_MALFORMED = 0x02;

const COLOR_NORMAL_STROKE = '#a78bfa'; // Bright Purple/Violet
const COLOR_FLAGGED_STROKE = '#f43f5e'; // Rose / Red
const COLOR_HOLD_STROKE = '#64748b'; // Slate (dim)

const COLOR_NORMAL_FILL = 'rgba(167, 139, 250, 0.16)';
const COLOR_FLAGGED_FILL = 'rgba(244, 63, 94, 0.22)';
const COLOR_HOLD_FILL = 'rgba(100, 116, 139, 0.12)';

export class FrequencyDomainPlot {
  /**
   * @param {HTMLElement} container
   */
  constructor(container) {
    this.container = container;
    this.uplot = null;

    // Buffer geometry
    this.capacity = 1000;
    this.targetBins = 1000;
    this.nominalRateHz = 2000000;
    this.frequenciesHz = null; // Float64Array or Array from spectrumAxis message

    // Preallocated data arrays
    this._allocateBuffers(this.capacity);

    // Max Hold (Peak Hold) state
    this.holdEnabled = false;
    this.holdMax = null;

    // Autoscale with hysteresis state
    this.autoScale = true;
    this.currentYMin = -40;
    this.currentYMax = 120;
    this.xSpan = this.nominalRateHz / 2;
    this.lastPeakChangeTime = performance.now();

    // Flag tint latching (250 ms)
    this.lastFlaggedTime = 0;

    // rAF latest-wins slot
    this.pendingSlot = null;
    this.isDirty = false;
    this.rafId = null;

    // ResizeObserver
    this.pendingResize = null;
    this.lastWidth = 0;
    this.lastHeight = 0;
    this._setupResizeObserver();

    // Peak tracking
    this.peakHz = 0;
    this.peakDb = -Infinity;
    /** @type {((peakHz: number, peakDb: number) => void) | null} */
    this.onPeakDetected = null;

    // Debug metrics
    this.drawsPerSec = 0;
    this.drawCountWindow = 0;
    this.lastFpsWindowTime = performance.now();
    this.lastDecodeUs = 0;

    this._initPlot();
    this._startRafLoop();
  }

  _allocateBuffers(capacity) {
    this.capacity = capacity;
    this.dbData = new Float32Array(capacity);
    this.xData = new Float64Array(capacity);
    this.blank = new Float64Array(capacity).fill(NaN);
    this.cachedPointCount = 0;

    // Pre-fill default linear frequency axis (0 to Nyquist)
    const nyquist = this.nominalRateHz / 2;
    for (let i = 0; i < capacity; i++) {
      this.xData[i] = (i / (capacity - 1 || 1)) * nyquist;
    }

    if (this.holdEnabled) {
      this.holdMax = new Float32Array(capacity);
      this.holdMax.fill(-200);
    }
  }

  _growBuffersIfNeeded(requiredCapacity) {
    if (requiredCapacity > this.capacity) {
      const newCapacity = Math.max(requiredCapacity, this.capacity * 2);
      this._allocateBuffers(newCapacity);
    }
  }

  setConfig(config) {
    if (!config) return;
    if (config.targetColumns || config.targetBins) {
      this.targetBins = config.targetBins || config.targetColumns;
      this._growBuffersIfNeeded(this.targetBins);
    }
    if (config.nominalRateHz) {
      this.nominalRateHz = config.nominalRateHz;
      if (!this.frequenciesHz) {
        // Update default frequency axis based on new Nyquist limit
        const nyquist = this.nominalRateHz / 2;
        for (let i = 0; i < this.capacity; i++) {
          this.xData[i] = (i / (this.capacity - 1 || 1)) * nyquist;
        }
      }
    }
  }

  /**
   * Set exact frequency axis bins from backend spectrumAxis message.
   * @param {Array<number>} frequenciesHz
   */
  setSpectrumAxis(frequenciesHz) {
    if (!Array.isArray(frequenciesHz) || frequenciesHz.length === 0) return;
    this.frequenciesHz = frequenciesHz;
    this._growBuffersIfNeeded(frequenciesHz.length);

    for (let i = 0; i < frequenciesHz.length; i++) {
      this.xData[i] = frequenciesHz[i];
    }
    this.cachedPointCount = frequenciesHz.length;
    this.isDirty = true;
  }

  _initPlot() {
    if (!window.uPlot) {
      console.warn('uPlot library not loaded');
      return;
    }

    const rect = this.container.getBoundingClientRect();
    const width = Math.max(rect.width || 600, 300);
    const height = Math.max(rect.height || 340, 200);

    const opts = {
      width,
      height,
      cursor: {
        drag: { x: true, y: true, setScale: false },
        points: { show: false },
      },
      hooks: {
        setSelect: [
          (u) => {
            const minX = u.posToVal(u.select.left, 'x');
            const maxX = u.posToVal(u.select.left + u.select.width, 'x');
            const minY = u.posToVal(u.select.top + u.select.height, 'y');
            const maxY = u.posToVal(u.select.top, 'y');

            if (u.select.width > 5 && u.select.height > 5) {
              this.autoScale = false;
              u.setScale('x', { min: minX, max: maxX });
              u.setScale('y', { min: minY, max: maxY });
            }
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          },
        ],
      },
      scales: {
        // Driven from inside the single setData() commit, so one redraw per frame.
        x: { time: false, range: () => [0, this.xSpan || this.nominalRateHz / 2] },
        // Falls back until the first frame seeds the window -- a redraw triggered by a
        // resize between reset() and the next spectrum would otherwise get +-Infinity.
        y: {
          range: () =>
            Number.isFinite(this.currentYMin) ? [this.currentYMin, this.currentYMax] : [-40, 120],
        },
      },
      axes: [
        {
          label: 'Frequency',
          stroke: '#94a3b8',
          grid: { stroke: 'rgba(51, 65, 85, 0.4)', width: 1 },
          ticks: { stroke: 'rgba(51, 65, 85, 0.6)', width: 1 },
          font: '11px JetBrains Mono, monospace',
          values: (u, splits) => {
            return splits.map((hz) => {
              if (hz >= 1_000_000) {
                return `${(hz / 1_000_000).toFixed(2)} MHz`;
              }
              if (hz >= 1_000) {
                return `${(hz / 1_000).toFixed(1)} kHz`;
              }
              return `${Math.round(hz)} Hz`;
            });
          },
        },
        {
          label: 'Magnitude (dB)',
          stroke: '#94a3b8',
          grid: { stroke: 'rgba(51, 65, 85, 0.4)', width: 1 },
          ticks: { stroke: 'rgba(51, 65, 85, 0.6)', width: 1 },
          font: '11px JetBrains Mono, monospace',
          values: (u, splits) => splits.map((v) => `${Math.round(v)} dB`),
        },
      ],
      series: [
        {}, // X Series
        {
          label: 'Max Hold',
          show: this.holdEnabled,
          stroke: COLOR_HOLD_STROKE,
          width: 1,
          fill: COLOR_HOLD_FILL,
        },
        {
          label: 'Spectrum',
          stroke: () =>
            performance.now() - this.lastFlaggedTime < 250
              ? COLOR_FLAGGED_STROKE
              : COLOR_NORMAL_STROKE,
          width: 1.5,
          fill: () =>
            performance.now() - this.lastFlaggedTime < 250
              ? COLOR_FLAGGED_FILL
              : COLOR_NORMAL_FILL,
        },
      ],
    };

    // Initial dummy data
    const initialPoints = 100;
    const dummyX = new Float64Array(initialPoints);
    const dummyHold = new Float32Array(initialPoints);
    const dummyDb = new Float32Array(initialPoints);

    for (let i = 0; i < initialPoints; i++) {
      dummyX[i] = (i / initialPoints) * 1_000_000;
      dummyDb[i] = -40;
      dummyHold[i] = -40;
    }

    this.container.innerHTML = '';
    this.uplot = new window.uPlot(opts, [dummyX, dummyHold, dummyDb], this.container);
  }

  _setupResizeObserver() {
    if (typeof ResizeObserver === 'undefined') return;
    // Rounded and de-duplicated for the same reason as the time-domain plot: uPlot's
    // root is taller than the plot by its legend, so an observer that feeds the reported
    // height straight back into setSize() grows the canvas without bound whenever the
    // container's height depends on its contents. See plotTD.js and the `.plot-body`
    // note in style.css.
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

  /**
   * Called on incoming WebSocket binary frequency-domain message.
   * Single-slot overwrite (latest-wins).
   */
  offerSpectrum(frameNumber, flags, pointCount, payload) {
    this.pendingSlot = { frameNumber, flags, pointCount, payload };
    this.isDirty = true;
  }

  setMaxHold(enabled) {
    this.holdEnabled = enabled;
    if (enabled && (!this.holdMax || this.holdMax.length < this.capacity)) {
      this.holdMax = new Float32Array(this.capacity);
      this.holdMax.fill(-200);
    }
    if (this.uplot) {
      this.uplot.series[1].show = enabled;
    }
    this.isDirty = true;
  }

  clearMaxHold() {
    if (this.holdMax) {
      this.holdMax.fill(-200);
    }
    this.isDirty = true;
  }

  resetZoom() {
    // Hand the axes back to the autoscaler and let the next frame reseed them through
    // the range callbacks. Pinning -40/120 dB here meant Reset Zoom snapped to a window
    // the signal might not be in, rather than to whatever the signal actually is.
    this.autoScale = true;
    this.currentYMin = Infinity;
    this.currentYMax = -Infinity;
    this.lastPeakChangeTime = 0;
    this.isDirty = true;
  }

  _startRafLoop() {
    const renderFrame = (now) => {
      if (this.isDirty) {
        this._render(now);
      }
      this.rafId = requestAnimationFrame(renderFrame);
    };
    this.rafId = requestAnimationFrame(renderFrame);
  }

  _render(now) {
    if (!this.uplot) return;
    if (this.container.hidden || this.container.offsetParent === null) {
      this.isDirty = false;
      return;
    }

    // Handle coalesced resize
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

    const t0 = performance.now();
    const { flags, pointCount, payload } = slot;
    this.pendingSlot = null;
    this.isDirty = false;

    this._growBuffersIfNeeded(pointCount);

    if (flags & (FLAG_INVALID | FLAG_MALFORMED)) {
      this.lastFlaggedTime = now;
    }

    // Rebuild default linear frequency axis if backend axis is not provided
    if (!this.frequenciesHz && this.cachedPointCount !== pointCount) {
      const nyquist = this.nominalRateHz / 2;
      const binWidth = nyquist / pointCount;
      for (let i = 0; i < pointCount; i++) {
        this.xData[i] = (i + 0.5) * binWidth;
      }
      this.cachedPointCount = pointCount;
    }

    // Copy dB values, update Max Hold, and find global peak
    let frameMin = 200;
    let frameMax = -200;
    let peakVal = -Infinity;
    let peakIdx = 0;

    for (let i = 0; i < pointCount; i++) {
      const val = payload[i];
      this.dbData[i] = val;

      if (val < frameMin) frameMin = val;
      if (val > frameMax) frameMax = val;

      if (val > peakVal) {
        peakVal = val;
        peakIdx = i;
      }

      if (this.holdEnabled && this.holdMax) {
        if (val > this.holdMax[i]) {
          this.holdMax[i] = val;
        }
      }
    }

    // Notify peak frequency & magnitude
    if (peakVal > -Infinity && typeof this.onPeakDetected === 'function') {
      const peakFreq = this.xData[peakIdx] || 0;
      this.peakHz = peakFreq;
      this.peakDb = peakVal;
      this.onPeakDetected(peakFreq, peakVal);
    }

    // Autoscale with asymmetric hysteresis -- expand on the frame that needs it, shrink
    // only after a quiet second. `lastPeakChangeTime` has to be *updated* for that to be
    // true; it was set once in the constructor and never touched, which made this a
    // symmetric lerp that chased the noise floor. Same defect as plotTD carried.
    if (this.autoScale) {
      const targetMin = Math.floor(Math.max(frameMin - 10, -60));
      const targetMax = Math.ceil(Math.max(frameMax + 15, 20));

      let expanded = false;
      if (targetMin < this.currentYMin) {
        this.currentYMin = targetMin;
        expanded = true;
      }
      if (targetMax > this.currentYMax) {
        this.currentYMax = targetMax;
        expanded = true;
      }
      if (expanded) {
        this.lastPeakChangeTime = now;
      } else if (now - this.lastPeakChangeTime > 1000) {
        this.currentYMin += (targetMin - this.currentYMin) * 0.15;
        this.currentYMax += (targetMax - this.currentYMax) * 0.15;
      }

      this.xSpan = this.frequenciesHz
        ? this.frequenciesHz[pointCount - 1]
        : this.nominalRateHz / 2;
    }

    // Zero-allocation subviews. Full length even when hold is off: uPlot indexes every
    // series in lockstep with x, so a zero-length array is undefined values, not "no
    // data". NaN is how uPlot spells that.
    const xView = this.xData.subarray(0, pointCount);
    const dbView = this.dbData.subarray(0, pointCount);
    const holdView = this.holdEnabled
      ? this.holdMax.subarray(0, pointCount)
      : this.blank.subarray(0, pointCount);

    // One commit, one redraw. setScale redraws too, so scale-then-setData was three
    // canvas passes per frame; the scales now come from range callbacks inside this call.
    this.uplot.setData([xView, holdView, dbView], this.autoScale);

    // Track FPS & decode time
    this.lastDecodeUs = Math.round((performance.now() - t0) * 1000);
    this.drawCountWindow++;
    if (now - this.lastFpsWindowTime >= 1000) {
      this.drawsPerSec = this.drawCountWindow;
      this.drawCountWindow = 0;
      this.lastFpsWindowTime = now;
    }
  }

  reset() {
    this.pendingSlot = null;
    this.isDirty = false;
    this.clearMaxHold();
    this.resetZoom();
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
