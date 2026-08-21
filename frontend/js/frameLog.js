/**
 * Frame Log Manager with rAF-coalesced DocumentFragment rendering and bounded memory.
 *
 * Requirements:
 * - Exact line format: `[YYYY-MM-DD HH:MM:SS]: Frame <n> | <samples> | <hash>`
 * - Verbatim receipt timestamp (item.ts), bare sample count (no thousands separator)
 * - 5,000-item in-memory ring buffer (retained for export)
 * - 500-node DOM cap with rAF-batched eviction to guarantee smooth 60–120 fps
 * - Red line styling for invalid (is_valid=false) or malformed frames
 * - Dropped frame reporting
 */

// 500, matching the README and the plan's §6 frontend budget. Both numbers are quoted
// in the submitted documentation, so they are not free to drift.
const MAX_DOM_LINES = 500;
const MAX_BUFFER_LINES = 5000;

export class FrameLog {
  /**
   * @param {HTMLElement} containerEl
   * @param {HTMLElement} counterBadgeEl
   * @param {HTMLInputElement} autoScrollCheckbox
   * @param {HTMLElement} droppedBannerEl
   * @param {HTMLElement} droppedTextEl
   */
  constructor(
    containerEl,
    counterBadgeEl,
    autoScrollCheckbox,
    droppedBannerEl,
    droppedTextEl
  ) {
    this.containerEl = containerEl;
    this.counterBadgeEl = counterBadgeEl;
    this.autoScrollCheckbox = autoScrollCheckbox;
    this.droppedBannerEl = droppedBannerEl;
    this.droppedTextEl = droppedTextEl;

    this.buffer = new Array(MAX_BUFFER_LINES);
    this.bufferWriteIndex = 0;
    this.bufferCount = 0;
    this.droppedCountTotal = 0;
    this.domLineCount = 0;

    // rAF Coalesced queue
    this.pendingQueue = [];
    this.rafScheduled = false;

    // Auto-scroll follows the operator's *intent*, and only a gesture expresses intent.
    //
    // The obvious implementation -- listen to `scroll`, set the checkbox to whether we
    // are at the bottom -- turns auto-scroll off by itself within a second of
    // connecting. `scroll` events are delivered asynchronously, so a flag raised around
    // `scrollTop = scrollHeight` is already lowered by the time the event arrives; and
    // by then the next batch has appended more lines, so the position the handler
    // measures is stale and reads as "the user scrolled up". At 30 flushes a second it
    // takes one race to lose the setting, and nothing on screen explains why.
    //
    // So the handler is bound to the gestures that actually mean "I want to look at
    // history", and programmatic scrolling is not one of them.
    const evaluate = () => {
      // One frame later: the browser has applied the gesture by then, and reading the
      // position here rather than in the event keeps the scroll handler layout-free.
      requestAnimationFrame(() => {
        this.autoScrollCheckbox.checked = this._isScrolledToBottom();
      });
    };
    for (const eventName of ['wheel', 'touchmove', 'keydown', 'pointerdown']) {
      this.containerEl.addEventListener(eventName, evaluate, { passive: true });
    }
  }

  _isScrolledToBottom() {
    return (
      this.containerEl.scrollHeight - this.containerEl.scrollTop <=
      this.containerEl.clientHeight + 8
    );
  }

  /**
   * Process a batch of frame reports received from the backend.
   * @param {Array<{n: number, ts: string, samples: number, hash: string, valid: boolean, malformed?: boolean, rate?: number}>} items
   * @param {number} [dropped]
   */
  appendBatch(items, dropped = 0) {
    if (!items || items.length === 0) return;

    // 1. Immediately store into in-memory ring buffer, O(1) per line
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      this.buffer[this.bufferWriteIndex] = item;
      this.bufferWriteIndex = (this.bufferWriteIndex + 1) % MAX_BUFFER_LINES;
      if (this.bufferCount < MAX_BUFFER_LINES) {
        this.bufferCount++;
      }
      this.pendingQueue.push(item);
    }

    if (dropped && dropped > 0) {
      this.droppedCountTotal += dropped;
      this.droppedBannerEl.hidden = false;
      this.droppedTextEl.textContent = `Warning: Backend reported ${this.droppedCountTotal} dropped reports due to buffer overrun.`;
    }

    // 2. Schedule DOM flush inside requestAnimationFrame to prevent layout thrashing
    if (!this.rafScheduled) {
      this.rafScheduled = true;
      requestAnimationFrame(() => this._flushDom());
    }
  }

  _flushDom() {
    this.rafScheduled = false;
    if (this.pendingQueue.length === 0) return;

    const itemsToFlush = this.pendingQueue;
    this.pendingQueue = [];

    // Remove empty state placeholder if present
    const emptyState = this.containerEl.querySelector('.log-empty-state');
    if (emptyState) {
      emptyState.remove();
    }

    const fragment = document.createDocumentFragment();

    for (let i = 0; i < itemsToFlush.length; i++) {
      const item = itemsToFlush[i];
      const lineEl = document.createElement('div');
      lineEl.className = 'log-line';

      if (!item.valid) {
        lineEl.classList.add('log-line--invalid');
      }
      if (item.malformed) {
        lineEl.classList.add('log-line--malformed');
      }

      lineEl.textContent = `[${item.ts}]: Frame ${item.n} | ${item.samples} | ${item.hash}`;
      fragment.appendChild(lineEl);
    }

    this.containerEl.appendChild(fragment);
    this.domLineCount += itemsToFlush.length;

    // Batch eviction of old nodes
    const excess = this.domLineCount - MAX_DOM_LINES;
    if (excess > 0) {
      for (let k = 0; k < excess; k++) {
        if (this.containerEl.firstElementChild) {
          this.containerEl.removeChild(this.containerEl.firstElementChild);
          this.domLineCount--;
        }
      }
    }

    // Auto-scroll once per rAF tick rather than once per socket message: the read of
    // scrollHeight forces a layout, and doing it 30 times a second instead of 100 is
    // most of what this batching buys.
    if (this.autoScrollCheckbox.checked) {
      this.containerEl.scrollTop = this.containerEl.scrollHeight;
    }

    this.updateCounter();
  }

  /**
   * Reset the frame log view and in-memory buffer.
   */
  clear() {
    this.buffer = new Array(MAX_BUFFER_LINES);
    this.bufferWriteIndex = 0;
    this.bufferCount = 0;
    this.domLineCount = 0;
    this.pendingQueue = [];
    this.containerEl.innerHTML = `
      <div class="log-empty-state" id="logEmptyState">
        Log cleared. Waiting for new frames...
      </div>
    `;
    this.droppedBannerEl.hidden = true;
    this.droppedCountTotal = 0;
    this.updateCounter();
  }

  /**
   * The retained lines, oldest first.
   * @returns {Array<object>}
   */
  snapshot() {
    if (this.bufferCount < MAX_BUFFER_LINES) {
      return this.buffer.slice(0, this.bufferCount);
    }
    return this.buffer
      .slice(this.bufferWriteIndex)
      .concat(this.buffer.slice(0, this.bufferWriteIndex));
  }

  /** S3: the retained log as CSV. The ring buffer is already the export -- it exists so
   *  the DOM cap does not throw data away, which is exactly what an export needs. */
  toCsv() {
    const rows = this.snapshot().map(
      (i) =>
        `${i.n},${i.ts},${i.samples},${i.hash},${i.valid},${i.malformed === true},${i.rate ?? ''}`
    );
    return ['frame,timestamp,samples,hash,valid,malformed,estimated_rate_hz', ...rows].join('\n');
  }

  updateCounter() {
    this.counterBadgeEl.textContent =
      `${this.domLineCount} / ${MAX_DOM_LINES} DOM (${this.bufferCount} in buffer)`;
  }
}
