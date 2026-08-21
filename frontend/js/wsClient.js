/**
 * WebSocket client communicating with the backend /stream endpoint.
 *
 * Handles control command transmission and server push events (status, frames).
 */

export class StreamClient {
  constructor() {
    this.ws = null;
    this.isOpen = false;

    /** @type {((state: string, message: string) => void) | null} */
    this.onStatus = null;

    /** @type {((items: Array<any>, rateAvg?: number, dropped?: number) => void) | null} */
    this.onFrames = null;

    /**
     * Unused until the FD tab lands (stretch S1): the backend only emits this after a
     * `setDomain: "fd"`, which nothing sends yet. Kept as the seam, not as behaviour.
     * @type {((frequenciesHz: Array<number>) => void) | null}
     */
    this.onSpectrumAxis = null;

    /** @type {((message: string) => void) | null} */
    this.onBackendClosed = null;
  }

  /**
   * Connect to the backend /stream WebSocket endpoint, same origin as the page.
   *
   * Called once, on load. There is deliberately no reconnect: assumption #14 says
   * reconnection is manual, and that applies to this socket too -- silently re-arming
   * it would leave the UI claiming a session the backend has forgotten.
   */
  connectBackend() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const targetUrl = `${protocol}//${window.location.host}/stream`;

    try {
      this.ws = new WebSocket(targetUrl);
    } catch (err) {
      if (typeof this.onBackendClosed === 'function') {
        this.onBackendClosed(`Could not open WebSocket to backend: ${err.message}`);
      }
      return;
    }

    this.ws.onopen = () => {
      this.isOpen = true;
    };

    this.ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          this._handleTextMessage(msg);
        } catch (err) {
          console.error('Failed to parse backend JSON message:', err, event.data);
        }
      }
      // Binary messages (waveforms) will be consumed in Phase 5
    };

    // No onerror handler: every error is followed by a close event, and reporting
    // both would produce two dialogs for one failure.

    this.ws.onclose = (event) => {
      this.isOpen = false;
      this.ws = null;

      if (typeof this.onBackendClosed === 'function') {
        const reason = event.reason ? `: ${event.reason}` : '';
        this.onBackendClosed(`Disconnected from backend server (/stream)${reason}`);
      }
    };
  }

  _handleTextMessage(msg) {
    if (!msg || typeof msg !== 'object') return;

    switch (msg.type) {
      case 'status':
        if (typeof this.onStatus === 'function') {
          this.onStatus(msg.state, msg.message || '');
        }
        break;

      case 'frames':
        if (typeof this.onFrames === 'function') {
          this.onFrames(msg.items || [], msg.rateAvg, msg.dropped || 0);
        }
        break;

      case 'spectrumAxis':
        if (typeof this.onSpectrumAxis === 'function') {
          this.onSpectrumAxis(msg.frequenciesHz || []);
        }
        break;

      default:
        console.warn('Unhandled message type from backend:', msg.type);
    }
  }

  /**
   * Request backend to connect to a microcontroller stream.
   * @param {string} uCUrl
   */
  sendConnect(uCUrl) {
    if (!this.isOpen || !this.ws) {
      throw new Error('Backend connection is not open');
    }
    this.ws.send(JSON.stringify({ type: 'connect', url: uCUrl }));
  }

  /**
   * Request backend to disconnect from the microcontroller stream.
   */
  sendDisconnect() {
    if (!this.isOpen || !this.ws) return;
    this.ws.send(JSON.stringify({ type: 'disconnect' }));
  }
}
