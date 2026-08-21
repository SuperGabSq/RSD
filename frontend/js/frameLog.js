/**
 * Frame Log Manager with batched DocumentFragment rendering and bounded memory.
 *
 * Requirements:
 * - Exact line format: `[YYYY-MM-DD HH:MM:SS]: Frame <n> | <samples> | <hash>`
 * - Verbatim receipt timestamp (item.ts), bare sample count (no thousands separator)
 * - 5,000-item in-memory ring buffer (retained for future export)
 * - 500-node DOM cap (oldest elements evicted to maintain smooth 60 fps UI)
 * - Red line styling for invalid (is_valid=false) or malformed frames
 * - Dropped frame reporting
 */

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

    /**
     * Fixed-capacity ring buffer. A plain array with shift() is O(n): at 100 lines/s
     * against 5 000 entries that is half a million element moves per second, in a
     * project whose whole argument is that the hot path is cheap. A write index costs
     * nothing.
     * @type {Array<object>}
     */
    this.buffer = new Array(MAX_BUFFER_LINES);
    this.bufferWriteIndex = 0;
    this.bufferCount = 0;
    this.droppedCountTotal = 0;
    this.domLineCount = 0;

    // Auto-scroll pauses the moment the operator scrolls up, and resumes when they
    // return to the bottom. Without this, reading history at 100 lines/s is impossible:
    // every batch would yank the viewport back down.
    this._suppressScrollHandler = false;
    this.containerEl.addEventListener('scroll', () => {
      if (this._suppressScrollHandler) return;
      this.autoScrollCheckbox.checked = this._isScrolledToBottom();
    });
  }

  _isScrolledToBottom() {
    return (
      this.containerEl.scrollHeight - this.containerEl.scrollTop <=
      this.containerEl.clientHeight + 4
    );
  }

  /**
   * Process a batch of frame reports received from the backend.
   * @param {Array<{n: number, ts: string, samples: number, hash: string, valid: boolean, malformed?: boolean, rate?: number}>} items
   * @param {number} [dropped]
   */
  appendBatch(items, dropped = 0) {
    if (!items || items.length === 0) return;

    // Remove empty state placeholder if present
    const emptyState = this.containerEl.querySelector('.log-empty-state');
    if (emptyState) {
      emptyState.remove();
    }

    const fragment = document.createDocumentFragment();

    for (let i = 0; i < items.length; i++) {
      const item = items[i];

      // 1. In-memory ring buffer (up to 5 000 lines), O(1) per line
      this.buffer[this.bufferWriteIndex] = item;
      this.bufferWriteIndex = (this.bufferWriteIndex + 1) % MAX_BUFFER_LINES;
      if (this.bufferCount < MAX_BUFFER_LINES) {
        this.bufferCount++;
      }

      // 2. Format exact line: [YYYY-MM-DD HH:MM:SS]: Frame <n> | <samples> | <hash>
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

    // 3. One reflow per batch, not one per line
    this.containerEl.appendChild(fragment);
    this.domLineCount += items.length;

    // 4. Enforce the DOM cap by evicting the oldest lines
    while (this.domLineCount > MAX_DOM_LINES && this.containerEl.firstElementChild) {
      this.containerEl.removeChild(this.containerEl.firstElementChild);
      this.domLineCount--;
    }

    // 5. Auto-scroll. Scrolling programmatically fires the scroll event, which would
    // otherwise be read as the operator having moved the viewport.
    if (this.autoScrollCheckbox.checked) {
      this._suppressScrollHandler = true;
      this.containerEl.scrollTop = this.containerEl.scrollHeight;
      this._suppressScrollHandler = false;
    }

    // 6. Handle dropped frames reporting
    if (dropped && dropped > 0) {
      this.droppedCountTotal += dropped;
      this.droppedBannerEl.hidden = false;
      this.droppedTextEl.textContent = `Warning: Backend reported ${this.droppedCountTotal} dropped reports due to buffer overrun.`;
    }

    // 7. Update header badge counter
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
   * The retained lines, oldest first. Nothing calls this yet -- it is the read side of
   * the ring buffer that stretch S3 (export) needs, and the reason the buffer exists at
   * all rather than being a second copy of the DOM.
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

  updateCounter() {
    // Counters are tracked as we go: querySelectorAll over 500 nodes on every batch is
    // a full DOM walk 30 times a second for a number we already know.
    this.counterBadgeEl.textContent =
      `${this.domLineCount} / ${MAX_DOM_LINES} DOM (${this.bufferCount} in buffer)`;
  }
}
