/**
 * Modal dialog controller using native HTML5 <dialog>.
 *
 * Handles accessible focus trapping, backdrop display, and ESC-key dismissal natively.
 */

export class ModalController {
  /**
   * @param {HTMLDialogElement} dialogEl
   * @param {HTMLElement} titleEl
   * @param {HTMLElement} messageEl
   * @param {HTMLButtonElement} dismissBtn
   */
  constructor(dialogEl, titleEl, messageEl, dismissBtn) {
    this.dialogEl = dialogEl;
    this.titleEl = titleEl;
    this.messageEl = messageEl;
    this.dismissBtn = dismissBtn;
    this._onDismiss = null;

    this.dismissBtn.addEventListener('click', () => this.hide());
    this.dialogEl.addEventListener('cancel', () => this._handleClose());
    this.dialogEl.addEventListener('close', () => this._handleClose());
  }

  /**
   * Display modal with a custom title and verbatim message.
   *
   * First writer wins. A second show() while a dialog is open used to overwrite the
   * title, the message AND the pending dismiss callback -- which silently dropped the
   * "re-enable Connect" action and replaced the real diagnosis with whatever arrived
   * next. That is exactly what happened on the single-client path: the backend refuses
   * the second browser with a readable reason and then closes the socket, so the close
   * handler's generic message landed on top of it.
   *
   * @param {string} title
   * @param {string} message
   * @param {(() => void) | null} onDismiss
   * @returns {boolean} false if a dialog was already open and this one was suppressed
   */
  show(title, message, onDismiss = null) {
    if (this.dialogEl.open) {
      console.warn('Modal already open; suppressing:', title, message);
      return false;
    }
    this._onDismiss = onDismiss;
    this.titleEl.textContent = title;
    this.messageEl.textContent = message;
    this.dialogEl.showModal();
    return true;
  }

  /**
   * True while a dialog is on screen, so callers can decide not to compete for it.
   */
  get isOpen() {
    return this.dialogEl.open;
  }

  /**
   * Hide the modal dialog.
   */
  hide() {
    if (this.dialogEl.open) {
      this.dialogEl.close();
    }
  }

  _handleClose() {
    if (typeof this._onDismiss === 'function') {
      const cb = this._onDismiss;
      this._onDismiss = null;
      cb();
    }
  }
}
