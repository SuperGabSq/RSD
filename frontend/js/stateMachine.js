/**
 * Connection State Machine & UI Binding.
 *
 * States:
 * - uninitialized: /stream handshake pending (Connect button disabled)
 * - idle: /stream open and ready (Connect enabled, Disconnect disabled)
 * - connecting: uC connection in progress (Connect disabled, Disconnect enabled)
 * - connected: streaming active (Connect disabled, Disconnect enabled)
 * - disconnected: uC dropped connection (triggers drop modal; re-enables Connect on dismissal)
 * - error: uC connection failed or single-client rejected (triggers error modal; re-enables Connect on dismissal)
 * - backend_offline: the /stream socket itself died -- a different failure from the uC
 *   dropping, and unrecoverable without a page reload, so it is a distinct state rather
 *   than a reuse of `error`
 */

export const ConnectionState = Object.freeze({
  UNINITIALIZED: 'uninitialized',
  IDLE: 'idle',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  DISCONNECTED: 'disconnected',
  ERROR: 'error',
  BACKEND_OFFLINE: 'backend_offline',
});

export class StateMachine {
  /**
   * @param {HTMLInputElement} urlInput
   * @param {HTMLButtonElement} connectBtn
   * @param {HTMLButtonElement} disconnectBtn
   * @param {HTMLElement} statusDot
   * @param {HTMLElement} statusText
   * @param {import('./modal.js').ModalController} modal
   */
  constructor(urlInput, connectBtn, disconnectBtn, statusDot, statusText, modal) {
    this.urlInput = urlInput;
    this.connectBtn = connectBtn;
    this.disconnectBtn = disconnectBtn;
    this.statusDot = statusDot;
    this.statusText = statusText;
    this.modal = modal;

    this.currentState = ConnectionState.UNINITIALIZED;
    this._applyState(ConnectionState.UNINITIALIZED, 'Connecting to backend...');
  }

  /**
   * Transition state from incoming backend status message.
   * @param {string} stateName
   * @param {string} [message]
   */
  transition(stateName, message = '') {
    const normalized = stateName.toLowerCase();
    this._applyState(normalized, message);
  }

  /**
   * The brief requires Connect to be usable again once the popup is closed -- but not
   * if the backend socket has since died, in which case there is nothing to connect
   * through and an enabled button would only produce a second failure.
   */
  _reenableConnect() {
    if (this.isBackendOffline) return;
    this.connectBtn.disabled = false;
  }

  _applyState(state, message) {
    this.currentState = state;

    // Reset status dot classes
    this.statusDot.className = 'status-dot';

    switch (state) {
      case ConnectionState.UNINITIALIZED:
        this.statusDot.classList.add('status-dot--idle');
        this.statusText.textContent = 'CONNECTING BACKEND...';
        this.urlInput.disabled = true;
        this.connectBtn.disabled = true;
        this.disconnectBtn.disabled = true;
        break;

      case ConnectionState.IDLE:
        this.statusDot.classList.add('status-dot--idle');
        this.statusText.textContent = 'READY';
        this.urlInput.disabled = false;
        this.connectBtn.disabled = false;
        this.disconnectBtn.disabled = true;
        break;

      case ConnectionState.CONNECTING:
        this.statusDot.classList.add('status-dot--connecting');
        this.statusText.textContent = 'CONNECTING...';
        this.urlInput.disabled = true;
        this.connectBtn.disabled = true;
        this.disconnectBtn.disabled = false;
        break;

      case ConnectionState.CONNECTED:
        this.statusDot.classList.add('status-dot--connected');
        this.statusText.textContent = 'STREAMING';
        this.urlInput.disabled = true;
        this.connectBtn.disabled = true;
        this.disconnectBtn.disabled = false;
        break;

      case ConnectionState.DISCONNECTED:
        this.statusDot.classList.add('status-dot--disconnected');
        this.statusText.textContent = 'DISCONNECTED';
        this.urlInput.disabled = false;
        this.connectBtn.disabled = true;
        this.disconnectBtn.disabled = true;

        // Show drop modal; re-enable Connect button on dismissal
        this.modal.show(
          'Connection Dropped',
          message || 'The connection to the microcontroller was lost.',
          () => this._reenableConnect()
        );
        break;

      case ConnectionState.ERROR:
        this.statusDot.classList.add('status-dot--error');
        this.statusText.textContent = 'ERROR';
        this.urlInput.disabled = false;
        this.connectBtn.disabled = true;
        this.disconnectBtn.disabled = true;

        // Show error modal; re-enable Connect button on dismissal
        this.modal.show(
          'Connection Error',
          message || 'Could not establish connection to the specified URL.',
          () => this._reenableConnect()
        );
        break;

      default:
        console.warn('Unknown state:', state);
    }
  }

  /**
   * Handle unexpected disconnection from the Flask /stream backend socket.
   * @param {string} errorMessage
   */
  handleBackendDisconnect(errorMessage) {
    this.currentState = ConnectionState.BACKEND_OFFLINE;
    this.statusDot.className = 'status-dot status-dot--error';
    this.statusText.textContent = 'BACKEND OFFLINE';
    this.urlInput.disabled = true;
    this.connectBtn.disabled = true;
    this.disconnectBtn.disabled = true;

    // If a dialog is already up it is carrying a more specific diagnosis than "the
    // socket closed" -- the backend refusing a second browser, say, which sends its
    // reason and then closes. Do not paint over it.
    this.modal.show(
      'Backend Disconnected',
      errorMessage || 'Lost WebSocket connection to the SignalScope backend (/stream).'
    );
  }

  /**
   * True once the /stream socket is gone. There is no way back without reloading, so
   * the caller stops reporting further failures rather than stacking dialogs.
   */
  get isBackendOffline() {
    return this.currentState === ConnectionState.BACKEND_OFFLINE;
  }
}
