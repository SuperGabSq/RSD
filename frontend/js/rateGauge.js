/**
 * Sample-rate gauge: one SVG arc, driven by stroke-dashoffset.
 *
 * The gauge shows the EMA (assumption #11) with the honest per-frame value beside it.
 *
 * It reports a rate; it does not grade one. An earlier version scored the reading against
 * a +-1 % "nominal" band and turned the arc amber or red outside it. The brief asks only
 * for "the estimated sample rate ... in an output text box of a graphical gauge" -- no
 * tolerance, no thresholds -- and those bands ended up measuring the transport rather than
 * the instrument: arrival jitter alone kept a correctly-paced 100.0 Hz stream out of the
 * green band about 40 % of the time. Worse, the brief assigns red exactly one meaning --
 * "this frame had the wrong number of samples" -- so a gauge flashing red on a healthy
 * stream competed with the one signal that *is* specified. Two states remain, and neither
 * is a verdict: idle, and streaming.
 */

// Arc geometry. The 220 deg sweep is fixed by the path below; changing one means
// changing the other, so the length is derived rather than written twice.
const ARC_RADIUS = 42;
const ARC_DEGREES = 220;
const ARC_LENGTH = Math.PI * ARC_RADIUS * (ARC_DEGREES / 180);
const ARC_PATH = `M 20.5 75 A ${ARC_RADIUS} ${ARC_RADIUS} 0 1 1 99.5 75`;

// Full scale is 2x nominal, which puts nominal at the top-centre tick.
const FULL_SCALE_MULTIPLE = 2;

export class RateGauge {
  /**
   * @param {HTMLElement} container
   */
  constructor(container) {
    this.container = container;
    this.nominalRateHz = 2_000_000;
    this._initDom();
  }

  /** The nominal comes from the backend (`SAMPLE_RATE_HZ`), never from a constant here:
   *  an env override would otherwise make the gauge quietly lie about its own baseline. */
  setConfig(config) {
    if (config && config.nominalRateHz > 0) {
      this.nominalRateHz = config.nominalRateHz;
      this.nominalLabel.textContent = `${(this.nominalRateHz / 1e6).toFixed(2)} Msps (Nominal)`;
    }
  }

  _initDom() {
    this.container.innerHTML = `
      <div class="gauge-card gauge--idle" id="rateGaugeRoot">
        <div class="gauge-header">
          <span class="gauge-title">Sample Rate</span>
          <span class="gauge-nominal-tag" id="gaugeNominalTag">-- Msps (Nominal)</span>
        </div>
        <div class="gauge-body">
          <div class="gauge-svg-wrap">
            <svg class="gauge-svg" viewBox="0 0 120 90" preserveAspectRatio="xMidYMid meet">
              <path class="gauge-track" d="${ARC_PATH}" fill="none" stroke-width="8" stroke-linecap="round" />
              <!-- Nominal tick, top centre: nominal is half of full scale -->
              <line class="gauge-nominal-tick" x1="60" y1="18" x2="60" y2="8" stroke-width="2" stroke-linecap="round" />
              <path id="gaugeProgressArc" class="gauge-progress" d="${ARC_PATH}" fill="none" stroke-width="8" stroke-linecap="round"
                    stroke-dasharray="${ARC_LENGTH.toFixed(1)}" stroke-dashoffset="${ARC_LENGTH.toFixed(1)}" />
            </svg>
            <div class="gauge-center-readout">
              <span class="gauge-value" id="gaugeMainValue">--</span>
              <span class="gauge-unit">Msps</span>
            </div>
          </div>
          <div class="gauge-metrics">
            <div class="gauge-metric-row">
              <span class="gauge-metric-label">Deviation:</span>
              <span class="gauge-metric-value" id="gaugeDeviation">--</span>
            </div>
            <div class="gauge-metric-row">
              <span class="gauge-metric-label">Instantaneous:</span>
              <span class="gauge-metric-value" id="gaugeInstantValue">--</span>
            </div>
            <div class="gauge-status-badge" id="gaugeStatusBadge">STANDBY</div>
          </div>
        </div>
      </div>
    `;

    this.root = this.container.querySelector('#rateGaugeRoot');
    this.nominalLabel = this.container.querySelector('#gaugeNominalTag');
    this.progressArc = this.container.querySelector('#gaugeProgressArc');
    this.mainValue = this.container.querySelector('#gaugeMainValue');
    this.deviationValue = this.container.querySelector('#gaugeDeviation');
    this.instantValue = this.container.querySelector('#gaugeInstantValue');
    this.statusBadge = this.container.querySelector('#gaugeStatusBadge');
  }

  /**
   * @param {number | null} smoothedHz  EMA, shown large -- assumption #11
   * @param {number | null} instantHz   this frame's raw estimate, shown beside it
   */
  update(smoothedHz, instantHz) {
    if (!Number.isFinite(smoothedHz)) {
      this.reset();
      return;
    }

    this.mainValue.textContent = (smoothedHz / 1e6).toFixed(3);
    this.instantValue.textContent = Number.isFinite(instantHz)
      ? `${(instantHz / 1e6).toFixed(3)} Msps`
      : '--';

    // Deviation from nominal, reported as a number and left at that. It is genuinely
    // useful -- it is how you see `?rate_factor=0.5` land as -50 % -- but reading it is
    // the operator's job, not the gauge's.
    const deviationPct = ((smoothedHz - this.nominalRateHz) / this.nominalRateHz) * 100;
    const sign = deviationPct >= 0 ? '+' : '';
    this.deviationValue.textContent = `${sign}${deviationPct.toFixed(2)} %`;

    const fraction = Math.min(1, Math.max(0, smoothedHz / (this.nominalRateHz * FULL_SCALE_MULTIPLE)));
    this.progressArc.style.strokeDashoffset = (ARC_LENGTH * (1 - fraction)).toFixed(1);

    // Assigned unconditionally rather than guarded by a "did it change?" check: it is one
    // string compare per property against a value the browser already holds, 30 times a
    // second, and the guard would cost more to read than it saves.
    this.root.className = 'gauge-card gauge--live';
    this.statusBadge.textContent = 'STREAMING';
  }

  reset() {
    this.mainValue.textContent = '--';
    this.deviationValue.textContent = '--';
    this.instantValue.textContent = '--';
    this.statusBadge.textContent = 'STANDBY';
    this.progressArc.style.strokeDashoffset = ARC_LENGTH.toFixed(1);
    this.root.className = 'gauge-card gauge--idle';
  }
}
