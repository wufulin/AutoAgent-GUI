/** One live gesture at a time. Coalesce motion, never retry uncertain writes. */
export type Rmq1Point = [number, number];
export interface Rmq1Result {
  type?: string;
  action_id?: string;
  success: boolean;
  recovery_required: boolean;
  outcome?: 'completed' | 'rejected' | 'unknown';
  error?: string;
}
export class Rmq1Control {
  private action: string | null = null;
  private pending: Rmq1Point | null = null;
  private animation = 0;
  private timeout = 0;
  private ending = false;
  private cancelled = false;
  private disposed = false;

  constructor(
    private send: (message: object) => void,
    private finished: (result: Rmq1Result) => void,
    private onStarted: () => void = () => {}
  ) {}

  begin(session: string, frame: string, point: Rmq1Point) {
    if (this.action || this.disposed) return false;
    this.action = crypto.randomUUID();
    this.ending = false;
    this.timeout = window.setTimeout(() => this.lost(), 4000);
    this.transmit({
      type: 'down',
      action_id: this.action,
      session_id: session,
      frame_id: frame,
      point,
    });
    return !!this.action;
  }

  move(point: Rmq1Point) {
    if (!this.action || this.ending || this.disposed) return;
    this.pending = point;
    if (!this.animation)
      this.animation = requestAnimationFrame(() => {
        this.animation = 0;
        const point = this.pending;
        this.pending = null;
        if (point && this.action && !this.ending)
          this.transmit({ type: 'move', action_id: this.action, point });
      });
  }

  end(point: Rmq1Point) {
    this.finish('up', point);
  }

  cancel() {
    this.finish('cancel');
  }

  private finish(type: 'up' | 'cancel', point?: Rmq1Point) {
    if (
      !this.action ||
      this.disposed ||
      (type === 'up' && this.ending) ||
      (type === 'cancel' && this.cancelled)
    )
      return;
    this.ending = true;
    if (type === 'cancel') this.cancelled = true;
    this.clearMotion();
    this.transmit({ type, action_id: this.action, ...(point && { point }) });
  }

  started(message: { action_id?: string }) {
    if (
      this.action &&
      message.action_id === this.action &&
      !this.ending &&
      !this.disposed
    )
      this.onStarted();
  }

  result(result: Rmq1Result) {
    if (!this.action || result.action_id !== this.action || this.disposed)
      return;
    this.clear();
    this.finished(result);
  }

  lost() {
    if (!this.action || this.disposed) return;
    // Best-effort release before quarantine. This is cleanup, never a replay.
    try {
      this.send({ type: 'cancel', action_id: this.action });
    } catch {
      /* The server watchdog also releases on disconnect. */
    }
    this.clear();
    this.finished({
      success: false,
      outcome: 'unknown',
      recovery_required: true,
      error: '控制连接中断或响应超时，请核对手机后恢复控制',
    });
  }

  dispose() {
    this.cancel();
    this.disposed = true;
    this.clear();
  }

  private transmit(message: object) {
    try {
      this.send(message);
    } catch {
      this.lost();
    }
  }

  private clearMotion() {
    cancelAnimationFrame(this.animation);
    this.animation = 0;
    this.pending = null;
  }

  private clear() {
    this.clearMotion();
    clearTimeout(this.timeout);
    this.action = null;
    this.ending = false;
    this.cancelled = false;
  }
}
