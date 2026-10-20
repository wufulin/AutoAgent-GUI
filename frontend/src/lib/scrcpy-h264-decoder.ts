import { h264ParseConfiguration } from '@yume-chan/scrcpy';

interface Packet {
  type: 'configuration' | 'data';
  data: ArrayBuffer | Uint8Array;
  keyframe?: boolean;
  epoch: number;
}

/** Correlate every decoded frame with its own render ACK and input geometry. */
export class ScrcpyH264Decoder {
  private decoder: VideoDecoder;
  private configuration: Uint8Array | undefined;
  private sequence = 0;
  private pending = new Map<number, { epoch: number; ack?: () => void }>();
  private drawing = Promise.resolve();
  private disposed = false;

  constructor(
    private draw: (frame: VideoFrame, epoch: number) => Promise<void>,
    private onError: (error: unknown) => void
  ) {
    this.decoder = new VideoDecoder({
      output: frame => {
        // Keep all (at most four) frames ordered. No ACK can be lost when
        // decoder output is faster than rendering or assigned to a later frame.
        this.drawing = this.drawing
          .then(async () => {
            if (this.disposed) return;
            const packet = this.pending.get(frame.timestamp);
            if (!packet) throw new Error('Unmatched decoded video frame');
            await this.draw(frame, packet.epoch);
            if (this.disposed) return;
            this.pending.delete(frame.timestamp);
            packet.ack?.();
          })
          .catch(error => {
            if (!this.disposed) {
              this.dispose();
              this.onError(error);
            }
          })
          .finally(() => frame.close());
      },
      error: error => {
        if (!this.disposed) {
          this.dispose();
          this.onError(error);
        }
      },
    });
  }

  write(packet: Packet, ack?: () => void) {
    if (this.disposed) return;
    let data = new Uint8Array(packet.data);
    if (packet.type === 'configuration') {
      if (this.pending.size)
        throw new Error('Video configuration changed before rendering');
      const { profileIndex, constraintSet, levelIndex } =
        h264ParseConfiguration(data);
      const codec = [profileIndex, constraintSet, levelIndex]
        .map(value => value.toString(16).padStart(2, '0'))
        .join('');
      this.decoder.configure({
        codec: `avc1.${codec}`,
        optimizeForLatency: true,
      });
      this.configuration = data;
      ack?.();
      return;
    }
    if (this.pending.size >= 4) throw new Error('Video render window exceeded');
    if (this.configuration) {
      const combined = new Uint8Array(this.configuration.length + data.length);
      combined.set(this.configuration);
      combined.set(data, this.configuration.length);
      data = combined;
      this.configuration = undefined;
    }
    const timestamp = ++this.sequence;
    this.pending.set(timestamp, { epoch: packet.epoch, ack });
    this.decoder.decode(
      new EncodedVideoChunk({
        type: packet.keyframe === false ? 'delta' : 'key',
        timestamp,
        data,
      })
    );
  }

  dispose() {
    this.disposed = true;
    this.pending.clear();
    if (this.decoder.state !== 'closed') this.decoder.close();
  }
}
