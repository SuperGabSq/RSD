/**
 * SignalScope Main Application Entry Point.
 */

import { FrameLog } from './frameLog.js';
import { ModalController } from './modal.js';
import { FrequencyDomainPlot } from './plotFD.js';
import { TimeDomainPlot } from './plotTD.js';
import { RateGauge } from './rateGauge.js';
import { StateMachine } from './stateMachine.js';
import { StreamClient } from './wsClient.js';

document.addEventListener('DOMContentLoaded', () => {
  // 1. Resolve DOM Elements
  const urlInput = /** @type {HTMLInputElement} */ (document.getElementById('urlInput'));
  const connectBtn = /** @type {HTMLButtonElement} */ (document.getElementById('connectBtn'));
  const disconnectBtn = /** @type {HTMLButtonElement} */ (document.getElementById('disconnectBtn'));
  const statusDot = /** @type {HTMLElement} */ (document.getElementById('statusDot'));
  const statusText = /** @type {HTMLElement} */ (document.getElementById('statusText'));

  const statusModalEl = /** @type {HTMLDialogElement} */ (document.getElementById('statusModal'));
  const modalTitleEl = /** @type {HTMLElement} */ (document.getElementById('modalTitle'));
  const modalMessageEl = /** @type {HTMLElement} */ (document.getElementById('modalMessage'));
  const modalDismissBtn = /** @type {HTMLButtonElement} */ (document.getElementById('modalDismissBtn'));

  // Visualisation & Domain Switcher
  const domainTabTD = /** @type {HTMLButtonElement} */ (document.getElementById('domainTabTD'));
  const domainTabFD = /** @type {HTMLButtonElement} */ (document.getElementById('domainTabFD'));
  const waveformPlotContainer = /** @type {HTMLElement} */ (document.getElementById('waveformPlot'));
  const spectrumPlotContainer = /** @type {HTMLElement} */ (document.getElementById('spectrumPlot'));
  const peakFreqBadge = /** @type {HTMLElement} */ (document.getElementById('peakFreqBadge'));
  const traceHoldToggle = /** @type {HTMLInputElement} */ (document.getElementById('traceHoldToggle'));
  const holdToggleLabel = /** @type {HTMLElement} */ (document.getElementById('holdToggleLabel'));
  const clearHoldBtn = /** @type {HTMLButtonElement} */ (document.getElementById('clearHoldBtn'));
  const resetZoomBtn = /** @type {HTMLButtonElement} */ (document.getElementById('resetZoomBtn'));

  const rateGaugeContainer = /** @type {HTMLElement} */ (document.getElementById('rateGaugeContainer'));
  const sessionFramesCount = /** @type {HTMLElement} */ (document.getElementById('sessionFramesCount'));
  const supersededCount = /** @type {HTMLElement} */ (document.getElementById('supersededCount'));

  const frameLogContainer = /** @type {HTMLElement} */ (document.getElementById('frameLogContainer'));
  const logCounterBadge = /** @type {HTMLElement} */ (document.getElementById('logCounterBadge'));
  const autoScrollToggle = /** @type {HTMLInputElement} */ (document.getElementById('autoScrollToggle'));
  const clearLogBtn = /** @type {HTMLButtonElement} */ (document.getElementById('clearLogBtn'));
  const droppedBanner = /** @type {HTMLElement} */ (document.getElementById('droppedBanner'));
  const droppedBannerText = /** @type {HTMLElement} */ (document.getElementById('droppedBannerText'));

  // Debug HUD elements
  const debugOverlay = /** @type {HTMLElement} */ (document.getElementById('debugOverlay'));
  const dbgDraws = /** @type {HTMLElement} */ (document.getElementById('dbgDraws'));
  const dbgDecode = /** @type {HTMLElement} */ (document.getElementById('dbgDecode'));
  const dbgLongTasks = /** @type {HTMLElement} */ (document.getElementById('dbgLongTasks'));
  const dbgSuperseded = /** @type {HTMLElement} */ (document.getElementById('dbgSuperseded'));

  // 2. Initialize Controllers
  const modal = new ModalController(statusModalEl, modalTitleEl, modalMessageEl, modalDismissBtn);
  const stateMachine = new StateMachine(urlInput, connectBtn, disconnectBtn, statusDot, statusText, modal);
  const frameLog = new FrameLog(
    frameLogContainer,
    logCounterBadge,
    autoScrollToggle,
    droppedBanner,
    droppedBannerText
  );
  const plotTD = new TimeDomainPlot(waveformPlotContainer);
  const plotFD = new FrequencyDomainPlot(spectrumPlotContainer);
  const rateGauge = new RateGauge(rateGaugeContainer);
  const client = new StreamClient();

  let activeDomain = 'td';
  let totalSuperseded = 0;

  // 3. Domain Switcher Logic
  function switchDomain(domain) {
    if (domain === activeDomain) return;
    activeDomain = domain;

    if (domain === 'td') {
      if (domainTabTD) {
        domainTabTD.classList.add('domain-tab--active');
        domainTabTD.setAttribute('aria-selected', 'true');
      }
      if (domainTabFD) {
        domainTabFD.classList.remove('domain-tab--active');
        domainTabFD.setAttribute('aria-selected', 'false');
      }
      waveformPlotContainer.hidden = false;
      spectrumPlotContainer.hidden = true;
      if (peakFreqBadge) peakFreqBadge.hidden = true;
      if (holdToggleLabel) holdToggleLabel.textContent = 'Trace Hold';
      traceHoldToggle.checked = plotTD.holdEnabled;
      clearHoldBtn.disabled = !plotTD.holdEnabled;

      client.sendSetDomain('td');
    } else if (domain === 'fd') {
      if (domainTabFD) {
        domainTabFD.classList.add('domain-tab--active');
        domainTabFD.setAttribute('aria-selected', 'true');
      }
      if (domainTabTD) {
        domainTabTD.classList.remove('domain-tab--active');
        domainTabTD.setAttribute('aria-selected', 'false');
      }
      waveformPlotContainer.hidden = true;
      spectrumPlotContainer.hidden = false;
      if (peakFreqBadge) peakFreqBadge.hidden = false;
      if (holdToggleLabel) holdToggleLabel.textContent = 'Max Hold';
      traceHoldToggle.checked = plotFD.holdEnabled;
      clearHoldBtn.disabled = !plotFD.holdEnabled;

      client.sendSetDomain('fd');
    }
  }

  if (domainTabTD) domainTabTD.addEventListener('click', () => switchDomain('td'));
  if (domainTabFD) domainTabFD.addEventListener('click', () => switchDomain('fd'));

  // Peak frequency readout update
  plotFD.onPeakDetected = (peakHz, peakDb) => {
    if (!peakFreqBadge) return;
    if (peakHz >= 1_000_000) {
      peakFreqBadge.textContent = `Peak: ${(peakHz / 1_000_000).toFixed(2)} MHz (${peakDb.toFixed(1)} dB)`;
    } else if (peakHz >= 1_000) {
      peakFreqBadge.textContent = `Peak: ${(peakHz / 1_000).toFixed(1)} kHz (${peakDb.toFixed(1)} dB)`;
    } else {
      peakFreqBadge.textContent = `Peak: ${Math.round(peakHz)} Hz (${peakDb.toFixed(1)} dB)`;
    }
  };

  // 4. Wire Client Callbacks
  client.onConfig = (config) => {
    plotTD.setConfig(config);
    plotFD.setConfig(config);
    rateGauge.setConfig(config);
  };

  client.onStatus = (state, message) => {
    stateMachine.transition(state, message);
    if (state === 'idle' || state === 'error' || state === 'disconnected') {
      plotTD.reset();
      plotFD.reset();
      rateGauge.reset();
    }
  };

  client.onWaveform = (frameNumber, flags, pointCount, payload) => {
    plotTD.offerWaveform(frameNumber, flags, pointCount, payload);
  };

  client.onSpectrum = (frameNumber, flags, pointCount, payload) => {
    plotFD.offerSpectrum(frameNumber, flags, pointCount, payload);
  };

  client.onSpectrumAxis = (frequenciesHz) => {
    plotFD.setSpectrumAxis(frequenciesHz);
  };

  client.onFrames = (items, rateAvg, dropped, superseded = 0) => {
    // Render log lines directly from the incoming batch
    frameLog.appendBatch(items, dropped);

    // Cache exact sample count for incoming waveforms
    for (const item of items) {
      if (item.n && item.samples) {
        plotTD.cacheFrameSampleCount(item.n, item.samples);
      }
    }

    // Update session metrics
    if (items.length > 0) {
      const lastItem = items[items.length - 1];
      sessionFramesCount.textContent = String(lastItem.n);
      rateGauge.update(rateAvg, lastItem.rate);
    } else if (rateAvg != null) {
      rateGauge.update(rateAvg, null);
    }

    if (rateAvg != null) {
      plotTD.setRateAvg(rateAvg);
    }

    if (superseded > 0) {
      totalSuperseded += superseded;
      supersededCount.textContent = String(totalSuperseded);
      if (dbgSuperseded) dbgSuperseded.textContent = String(totalSuperseded);
    }
  };

  client.onBackendClosed = (errorMsg) => {
    stateMachine.handleBackendDisconnect(errorMsg);
    plotTD.reset();
    plotFD.reset();
    rateGauge.reset();
  };

  // 5. Wire Plot Toolbar Controls
  traceHoldToggle.addEventListener('change', () => {
    const isChecked = traceHoldToggle.checked;
    if (activeDomain === 'td') {
      plotTD.setTraceHold(isChecked);
    } else {
      plotFD.setMaxHold(isChecked);
    }
    clearHoldBtn.disabled = !isChecked;
  });

  clearHoldBtn.addEventListener('click', () => {
    if (activeDomain === 'td') {
      plotTD.clearTraceHold();
    } else {
      plotFD.clearMaxHold();
    }
  });

  resetZoomBtn.addEventListener('click', () => {
    if (activeDomain === 'td') {
      plotTD.resetZoom();
    } else {
      plotFD.resetZoom();
    }
  });

  // 6. Wire User Actions
  connectBtn.addEventListener('click', () => {
    const url = urlInput.value.trim();
    if (!url) return;
    try {
      client.sendConnect(url);
    } catch (err) {
      modal.show('Connection Error', err.message);
    }
  });

  disconnectBtn.addEventListener('click', () => {
    client.sendDisconnect();
  });

  clearLogBtn.addEventListener('click', () => {
    frameLog.clear();
  });

  // S3: export. The complete log is the primary deliverable, so it is what gets exported.
  // The waveform is the lossy 30 Hz view -- exporting a decimated envelope would be
  // exporting the plot rather than the data.
  document.getElementById('exportCsvBtn').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([frameLog.toCsv()], { type: 'text/csv' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `signalscope-frames-${Date.now()}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  });

  urlInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !connectBtn.disabled) {
      connectBtn.click();
    }
  });

  // 7. Setup Debug HUD (?debug=1)
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('debug') === '1' && debugOverlay) {
    debugOverlay.hidden = false;
    let longTaskCount = 0;

    if (typeof PerformanceObserver !== 'undefined') {
      try {
        const observer = new PerformanceObserver((list) => {
          longTaskCount += list.getEntries().length;
          if (dbgLongTasks) dbgLongTasks.textContent = String(longTaskCount);
        });
        observer.observe({ entryTypes: ['longtask'] });
      } catch (err) {
        console.warn('Longtask PerformanceObserver not supported:', err);
      }
    }

    setInterval(() => {
      const activePlot = activeDomain === 'td' ? plotTD : plotFD;
      if (dbgDraws) dbgDraws.textContent = String(activePlot.drawsPerSec);
      if (dbgDecode) dbgDecode.textContent = `${activePlot.lastDecodeUs} µs`;
    }, 500);
  }

  // 8. Connect to Backend WebSocket endpoint
  client.connectBackend();
});


