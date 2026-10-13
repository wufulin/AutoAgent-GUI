/** Ordered input over one session; never replay a timed-out command. */
export interface FrameGeometry {
  width: number;
  height: number;
  epoch: number;
}
export type ControlAction =
  | 'down'
  | 'move'
  | 'up'
  | 'cancel'
  | 'scroll'
  | 'heartbeat';
export interface ControlInput extends FrameGeometry {
  action: ControlAction;
  x: number;
  y: number;
  scroll?: number;
}
export interface ControlAck {
  status: 'sent' | 'active' | 'rejected' | 'unknown';
  sequence?: number;
  error?: string;
}
export type ControlSend = (
  input: ControlInput & { session: string; sequence: number }
) => Promise<ControlAck>;

export function mapVideoPoint(
  clientX: number,
  clientY: number,
  rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>,
  frame: FrameGeometry,
  clamp = false
): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = (clientX - rect.left) / rect.width;
  const y = (clientY - rect.top) / rect.height;
  if (!clamp && (x < 0 || x > 1 || y < 0 || y > 1)) return null;
  return {
    x: Math.max(0, Math.min(frame.width - 1, Math.floor(x * frame.width))),
    y: Math.max(0, Math.min(frame.height - 1, Math.floor(y * frame.height))),
  };
}

export class PhoneControl {
  private queue: ControlInput[] = [];
  private sending = false;
  private sequence = 0;
  private disposed = false;
  private failed = false;
  private last: ControlInput | null = null;
  private pressed = false;

  constructor(
    private session: string,
    private send: ControlSend,
    private onError: (error: string, uncertain: boolean) => void,
    private onLatency: (ms: number) => void
  ) {}

  input(input: ControlInput) {
    if (this.disposed || this.failed) return;
    if (input.action === 'down') {
      if (this.pressed) return;
      this.pressed = true;
    } else if (['move', 'up', 'heartbeat'].includes(input.action)) {
      if (!this.pressed) return;
    }
    if (input.action === 'up' || input.action === 'cancel')
      this.pressed = false;
    this.last = input;
    const tail = this.queue.at(-1);
    if (
      tail?.action === input.action &&
      ['move', 'heartbeat', 'scroll'].includes(input.action)
    ) {
      this.queue[this.queue.length - 1] = input;
    } else {
      if (this.queue.length >= 32) {
        this.cancel(input);
        return;
      }
      this.queue.push(input);
    }
    void this.flush();
  }

  cancel(frame?: FrameGeometry) {
    if (this.disposed || this.failed || !this.last) return;
    this.pressed = false;
    this.queue = [];
    const geometry = frame || this.last;
    const input: ControlInput = {
      ...geometry,
      action: 'cancel',
      x: Math.min(geometry.width - 1, this.last.x),
      y: Math.min(geometry.height - 1, this.last.y),
    };
    this.last = input;
    this.queue.push(input);
    void this.flush();
  }

  heartbeat() {
    if (this.pressed && this.last)
      this.input({ ...this.last, action: 'heartbeat' });
  }

  dispose() {
    this.disposed = true;
    this.queue = [];
    this.pressed = false;
  }

  private async flush() {
    if (this.sending || this.disposed || this.failed) return;
    this.sending = true;
    try {
      while (this.queue.length && !this.disposed && !this.failed) {
        const input = this.queue.shift();
        if (!input) break;
        const start = performance.now();
        const ack = await this.send({
          ...input,
          session: this.session,
          sequence: ++this.sequence,
        });
        if (this.disposed) return;
        if (ack.status !== 'sent' && ack.status !== 'active') {
          this.failed = true;
          this.queue = [];
          this.onError(ack.error || 'Input rejected', ack.status === 'unknown');
          return;
        }
        this.onLatency(performance.now() - start);
      }
    } catch (error) {
      if (!this.disposed) {
        this.failed = true;
        this.queue = [];
        this.onError(String(error), true);
      }
    } finally {
      this.sending = false;
    }
  }
}
