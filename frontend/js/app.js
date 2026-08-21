/**
 * SignalScope Main Application Entry Point.
 */

import { FrameLog } from './frameLog.js';
import { ModalController } from './modal.js';
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

  const frameLogContainer = /** @type {HTMLElement} */ (document.getElementById('frameLogContainer'));
  const logCounterBadge = /** @type {HTMLElement} */ (document.getElementById('logCounterBadge'));
  const autoScrollToggle = /** @type {HTMLInputElement} */ (document.getElementById('autoScrollToggle'));
  const clearLogBtn = /** @type {HTMLButtonElement} */ (document.getElementById('clearLogBtn'));
  const droppedBanner = /** @type {HTMLElement} */ (document.getElementById('droppedBanner'));
  const droppedBannerText = /** @type {HTMLElement} */ (document.getElementById('droppedBannerText'));

  const sessionFramesCount = /** @type {HTMLElement} */ (document.getElementById('sessionFramesCount'));
  const rateSmoothedDisplay = /** @type {HTMLElement} */ (document.getElementById('rateSmoothedDisplay'));
  const rateInstantDisplay = /** @type {HTMLElement} */ (document.getElementById('rateInstantDisplay'));

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
  const client = new StreamClient();

  // 3. Wire Client Callbacks
  client.onStatus = (state, message) => {
    stateMachine.transition(state, message);
  };

  client.onFrames = (items, rateAvg, dropped) => {
    // Render log lines directly from the incoming batch
    frameLog.appendBatch(items, dropped);

    // Update session metrics
    if (items.length > 0) {
      const lastItem = items[items.length - 1];
      sessionFramesCount.textContent = String(lastItem.n);

      if (lastItem.rate != null) {
        rateInstantDisplay.textContent = `${(lastItem.rate / 1e6).toFixed(2)} Msps`;
      }
    }

    if (rateAvg != null) {
      rateSmoothedDisplay.textContent = (rateAvg / 1e6).toFixed(2);
    }
  };

  client.onBackendClosed = (errorMsg) => {
    stateMachine.handleBackendDisconnect(errorMsg);
  };

  // 4. Wire User Actions
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

  urlInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !connectBtn.disabled) {
      connectBtn.click();
    }
  });

  // 5. Connect to Backend WebSocket endpoint
  client.connectBackend();
});
