/**
 * WebSocket client communicating with the backend /stream endpoint.
 *
 * Handles control command transmission, server push events (config, status, frames),
 * and binary waveform streaming (Time Domain / Frequency Domain).
 */

const KIND_TIME_DOMAIN = 1;
const KIND_FREQUENCY_DOMAIN = 2;

export class StreamClient {
  constructor() {
    this.ws = null;
    this.isOpen = false;
    this.activeDomain = 'td';

    /** @type {((config: {nominalRateHz: number, expectedSamples: number, targetColumns: number}) => void) | null} */
    this.onConfig = null;

    /** @type {((state: string, message: string) => void) | null} */
    this.onStatus = null;

    /** @type {((items: Array<any>, rateAvg?: number, dropped?: number, superseded?: number) => void) | null} */
    this.onFrames = null;

    /** @type {((frameNumber: number, flags: number, pointCount: number, payload: Int32Array) => void) | null} */
    this.onWaveform = null;

    /** @type {((frameNumber: number, flags: number, pointCount: number, payload: Float32Array) => void) | null} */
    this.onSpectrum = null;

    /** @type {((frequenciesHz: Array<number>) => void) | null} */
    this.onSpectrumAxis = null;

    /** @type {((message: string) => void) | null} */
    this.onBackendClosed = null;

    this._setupVisibilityListener();
  }

  _setupVisibilityListener() {
    document.addEventListener('visibilitychange', () => {
      if (!this.isOpen) return;
      if (document.hidden) {
        this.sendSetDomain('none');
      } else {
        this.sendSetDomain(this.activeDomain || 'td');
      }
    });
  }

  /**
   * Connect to the backend /stream WebSocket endpoint, same origin as the page.
   */
  connectBackend() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const targetUrl = `${protocol}//${window.location.host}/stream`;

    try {
      this.ws = new WebSocket(targetUrl);
      this.ws.binaryType = 'arraybuffer';
    } catch (err) {
      if (typeof this.onBackendClosed === 'function') {
        this.onBackendClosed(`Could not open WebSocket to backend: ${err.message}`);
      }
      return;
    }

    this.ws.onopen = () => {
      this.isOpen = true;
      if (!document.hidden) {
        this.sendSetDomain(this.activeDomain || 'td');
      }
    };

    this.ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          this._handleTextMessage(msg);
        } catch (err) {
          console.error('Failed to parse backend JSON message:', err, event.data);
        }
      } else if (event.data instanceof ArrayBuffer) {
        this._handleBinaryMessage(event.data);
      }
    };

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
      case 'config':
        if (typeof this.onConfig === 'function') {
          this.onConfig(msg);
        }
        break;

      case 'status':
        if (typeof this.onStatus === 'function') {
          this.onStatus(msg.state, msg.message || '');
        }
        break;

      case 'frames':
        if (typeof this.onFrames === 'function') {
          this.onFrames(msg.items || [], msg.rateAvg, msg.dropped || 0, msg.superseded || 0);
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

  _handleBinaryMessage(buffer) {
    if (buffer.byteLength < 8) return;

    const view = new DataView(buffer);
    const frameNumber = view.getUint32(0, true);
    const kind = view.getUint8(4);
    const flags = view.getUint8(5);
    const pointCount = view.getUint16(6, true);

    if (kind === KIND_TIME_DOMAIN) {
      const expectedBytes = 8 + pointCount * 8; // int32 min/max pairs (8 bytes per column)
      if (buffer.byteLength < expectedBytes) return;

      const payload = new Int32Array(buffer, 8, pointCount * 2);
      if (typeof this.onWaveform === 'function') {
        this.onWaveform(frameNumber, flags, pointCount, payload);
      }
    } else if (kind === KIND_FREQUENCY_DOMAIN) {
      const expectedBytes = 8 + pointCount * 4; // float32 dB values (4 bytes per bin)
      if (buffer.byteLength < expectedBytes) return;

      const payload = new Float32Array(buffer, 8, pointCount);
      if (typeof this.onSpectrum === 'function') {
        this.onSpectrum(frameNumber, flags, pointCount, payload);
      }
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

  /**
   * Request backend waveform domain ("td", "fd", "none").
   * @param {'td' | 'fd' | 'none'} domain
   */
  sendSetDomain(domain) {
    if (domain !== 'none') {
      this.activeDomain = domain;
    }
    if (!this.isOpen || !this.ws) return;
    this.ws.send(JSON.stringify({ type: 'setDomain', domain }));
  }
}

