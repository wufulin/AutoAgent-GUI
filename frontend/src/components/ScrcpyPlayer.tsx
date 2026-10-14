import type { PointerEvent, WheelEvent } from 'react';
import { useEffect, useRef, useState } from 'react';
import { io } from 'socket.io-client';
import { useTranslation } from '../lib/i18n-context';
import { ScrcpyVideoCodecId } from '@yume-chan/scrcpy';
import {
  BitmapVideoFrameRenderer,
  WebCodecsVideoDecoder,
  WebGLVideoFrameRenderer,
} from '@yume-chan/scrcpy-decoder-webcodecs';
import { detectWebCodecsUnavailabilityReason } from '../lib/webcodecs-utils';
import {
  FrameGeometry,
  mapVideoPoint,
  PhoneControl,
} from '../lib/phone-control';

export type VideoQuality = 'balanced' | 'smooth' | 'data-saver';
export interface PlaybackStats {
  status: 'connecting' | 'video' | 'stalled' | 'error';
  fps: number;
  rtt: number;
}
const PROFILES = {
  balanced: { maxSize: 1280, bitRate: 4_000_000, maxFps: 20 },
  smooth: { maxSize: 1280, bitRate: 4_000_000, maxFps: 30 },
  'data-saver': { maxSize: 800, bitRate: 1_000_000, maxFps: 15 },
};
interface ScrcpyPlayerProps {
  deviceId: string;
  className?: string;
  onFallback?: (reason?: string) => void;
  fallbackTimeout?: number;
  enableControl?: boolean;
  onTapSuccess?: () => void;
  onTapError?: (error: string) => void;
  onSwipeSuccess?: () => void;
  onSwipeError?: (error: string) => void;
  onStreamReady?: (stream: { close: () => void } | null) => void;
  isVisible?: boolean;
  quality?: VideoQuality;
  onPlaybackStats?: (stats: PlaybackStats) => void;
}
interface VideoMetadata {
  codec?: number;
  session: string;
  recoveryRequired?: boolean;
}
interface VideoPacket {
  type: 'configuration' | 'data';
  data: ArrayBuffer | Uint8Array;
  keyframe?: boolean;
  pts?: number;
  epoch: number;
}

export function ScrcpyPlayer({
  deviceId,
  className,
  onFallback,
  fallbackTimeout = 20000,
  enableControl = false,
  onTapError,
  onStreamReady,
  isVisible = true,
  quality = 'balanced',
  onPlaybackStats,
}: ScrcpyPlayerProps) {
  const text = useTranslation().deviceMonitor;
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const controlRef = useRef<PhoneControl | null>(null);
  const geometryRef = useRef<FrameGeometry | null>(null);
  const pointerRef = useRef<number | null>(null);
  const callbacks = useRef({ onFallback, onTapError, onStreamReady, text });
  const recoveryRequestedRef = useRef(false);
  const [status, setStatus] = useState<PlaybackStats['status']>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [metrics, setMetrics] = useState({ fps: 0, rtt: 0 });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    onPlaybackStats?.({ status, ...metrics });
  }, [onPlaybackStats, status, metrics]);

  useEffect(() => {
    callbacks.current = { onFallback, onTapError, onStreamReady, text };
  }, [onFallback, onTapError, onStreamReady, text]);

  useEffect(() => {
    if (!isVisible) return;
    let disposed = false;
    let decoder: WebCodecsVideoDecoder | null = null;
    let writer: WritableStreamDefaultWriter<VideoPacket> | null = null;
    let initializing: Promise<void> = Promise.resolve();
    let renderedAck: (() => void) | null = null;
    let packetEpoch = 0;
    let rendered = 0;
    let ready = false;
    let previousRendered = 0;
    let lastFrameAt = 0;
    const latencies: number[] = [];
    const socket = io({
      path: '/socket.io',
      transports: ['websocket'],
      reconnectionAttempts: 3,
      timeout: 10000,
    });
    queueMicrotask(() => {
      setStatus('connecting');
      setError(null);
      setMetrics({ fps: 0, rtt: 0 });
    });

    const releaseDecoder = () => {
      controlRef.current?.dispose();
      controlRef.current = null;
      geometryRef.current = null;
      pointerRef.current = null;
      renderedAck = null;
      ready = false;
      writer = null;
      decoder?.dispose();
      decoder = null;
      canvasRef.current?.remove();
      canvasRef.current = null;
      callbacks.current.onStreamReady?.(null);
    };
    const fail = (message: string, unknown = false) => {
      if (disposed) return;
      controlRef.current?.dispose();
      controlRef.current = null;
      setError(message);
      setUncertain(unknown);
      setStatus('error');
      callbacks.current.onTapError?.(message);
      // Disconnect forces the server to release the active gesture. Never replay it.
      socket.disconnect();
    };
    const startup = window.setTimeout(() => {
      if (!lastFrameAt && !disposed) {
        fail(callbacks.current.text.controlTimeout);
        callbacks.current.onFallback?.('stream_timeout');
      }
    }, fallbackTimeout);

    socket.on('connect', () => {
      releaseDecoder();
      socket.emit('connect-device', {
        device_id: deviceId,
        control: true,
        ...(PROFILES[quality] || PROFILES.balanced),
      });
    });
    socket.on('video-metadata', (metadata: VideoMetadata) => {
      initializing = (async () => {
        releaseDecoder();
        const reason = detectWebCodecsUnavailabilityReason();
        if (reason) {
          callbacks.current.onFallback?.(reason);
          socket.disconnect();
          return;
        }
        if (!metadata.session)
          throw new Error(callbacks.current.text.controlBackendOld);
        const renderer = WebGLVideoFrameRenderer.isSupported
          ? new WebGLVideoFrameRenderer()
          : new BitmapVideoFrameRenderer();
        const canvas = renderer.canvas as HTMLCanvasElement;
        canvas.style.cssText =
          'max-width:100%;max-height:100%;object-fit:contain;touch-action:none';
        canvasRef.current = canvas;
        containerRef.current?.appendChild(canvas);
        controlRef.current = new PhoneControl(
          metadata.session,
          input => socket.timeout(3000).emitWithAck('control-input', input),
          fail,
          ms => {
            latencies.push(ms);
            if (latencies.length > 100) latencies.shift();
          }
        );
        decoder = new WebCodecsVideoDecoder({
          codec: (metadata.codec ||
            ScrcpyVideoCodecId.H264) as ScrcpyVideoCodecId,
          renderer: {
            setSize: (width, height) => renderer.setSize(width, height),
            draw: async frame => {
              await renderer.draw(frame);
              if (disposed) return;
              const geometry = {
                width: frame.displayWidth,
                height: frame.displayHeight,
                epoch: packetEpoch,
              };
              const old = geometryRef.current;
              if (
                old &&
                (old.epoch !== geometry.epoch ||
                  old.width !== geometry.width ||
                  old.height !== geometry.height)
              ) {
                pointerRef.current = null;
                controlRef.current?.cancel(geometry);
              }
              geometryRef.current = geometry;
              rendered++;
              lastFrameAt = performance.now();
              setStatus('video');
              if (!ready) {
                ready = true;
                callbacks.current.onStreamReady?.({
                  close: () => socket.disconnect(),
                });
              }
              renderedAck?.();
              renderedAck = null;
            },
          },
        });
        writer = (decoder.writable as WritableStream<VideoPacket>).getWriter();
        // A user-confirmed recovery may clear a previous session's quarantine.
        if (recoveryRequestedRef.current) {
          const result = await socket
            .timeout(3000)
            .emitWithAck('control-recover', {
              session: metadata.session,
              confirmed: true,
            });
          if (result.status !== 'recovered')
            throw new Error(callbacks.current.text.controlRecoveryFailed);
          recoveryRequestedRef.current = false;
          setUncertain(false);
        } else if (metadata.recoveryRequired) {
          setUncertain(true);
          setError(callbacks.current.text.controlUnknown);
          controlRef.current?.dispose();
          controlRef.current = null;
        }
      })();
      initializing.catch(err => fail(String(err)));
    });
    socket.on('video-data', async (packet: VideoPacket, ack?: () => void) => {
      try {
        await initializing;
        if (disposed || !writer) return;
        packetEpoch = packet.epoch;
        if (packet.type === 'configuration') {
          // Stop accepting input until a frame from the new geometry is displayed.
          geometryRef.current = geometryRef.current
            ? { ...geometryRef.current, epoch: -1 }
            : null;
          await writer.write({ ...packet, data: new Uint8Array(packet.data) });
          ack?.();
          return;
        }
        renderedAck = ack || null;
        await writer.write({ ...packet, data: new Uint8Array(packet.data) });
      } catch (err) {
        fail(String(err));
      }
    });
    socket.on(
      'control-error',
      (event: { message: string; uncertain?: boolean }) =>
        fail(event.message, !!event.uncertain)
    );
    socket.on('error', (event: { message?: string }) =>
      fail(event.message || callbacks.current.text.controlDisconnected)
    );
    socket.on('disconnect', () => {
      releaseDecoder();
      if (!disposed)
        setStatus(current => (current === 'error' ? current : 'connecting'));
    });
    socket.on('connect_error', () => {
      if (!disposed) setStatus('connecting');
    });
    socket.io.on('reconnect_failed', () => {
      fail(callbacks.current.text.controlReconnectFailed);
      callbacks.current.onFallback?.('stream_error');
    });

    const heartbeat = window.setInterval(
      () => controlRef.current?.heartbeat(),
      750
    );
    const stats = window.setInterval(() => {
      const sorted = [...latencies].sort((a, b) => a - b);
      setMetrics({
        fps: rendered - previousRendered,
        rtt: sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)] || 0,
      });
      previousRendered = rendered;
      if (lastFrameAt && performance.now() - lastFrameAt > 3000)
        setStatus(current => (current === 'video' ? 'stalled' : current));
    }, 1000);
    const cancel = () => {
      pointerRef.current = null;
      controlRef.current?.cancel(geometryRef.current || undefined);
    };
    const visibility = () => {
      if (document.hidden) cancel();
    };
    window.addEventListener('blur', cancel);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      disposed = true;
      clearTimeout(startup);
      clearInterval(heartbeat);
      clearInterval(stats);
      window.removeEventListener('blur', cancel);
      document.removeEventListener('visibilitychange', visibility);
      socket.disconnect();
      releaseDecoder();
    };
  }, [deviceId, isVisible, quality, attempt, fallbackTimeout]);

  const point = (clientX: number, clientY: number, clamp = false) => {
    const canvas = canvasRef.current;
    const geometry = geometryRef.current;
    if (
      !enableControl ||
      status !== 'video' ||
      !canvas ||
      !geometry ||
      geometry.epoch < 0
    )
      return null;
    const coords = mapVideoPoint(
      clientX,
      clientY,
      canvas.getBoundingClientRect(),
      geometry,
      clamp
    );
    return coords ? { ...geometry, ...coords } : null;
  };
  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || pointerRef.current !== null) return;
    const coords = point(event.clientX, event.clientY);
    if (!coords) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerRef.current = event.pointerId;
    controlRef.current?.input({ ...coords, action: 'down' });
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (pointerRef.current !== event.pointerId) return;
    const coords = point(event.clientX, event.clientY, true);
    if (coords) controlRef.current?.input({ ...coords, action: 'move' });
  };
  const up = (event: PointerEvent<HTMLDivElement>) => {
    if (pointerRef.current !== event.pointerId) return;
    pointerRef.current = null;
    const coords = point(event.clientX, event.clientY, true);
    if (coords) controlRef.current?.input({ ...coords, action: 'up' });
    else controlRef.current?.cancel();
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const cancel = () => {
    if (pointerRef.current === null) return;
    pointerRef.current = null;
    controlRef.current?.cancel();
  };
  const wheel = (event: WheelEvent<HTMLDivElement>) => {
    if (pointerRef.current !== null) return;
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const coords = point(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2
    );
    if (!coords) return;
    event.preventDefault();
    const delta =
      event.deltaY *
      (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);
    controlRef.current?.input({
      ...coords,
      action: 'scroll',
      scroll: Math.max(-8, Math.min(8, -delta / 100)),
    });
  };
  return (
    <div className={`flex w-full h-full min-h-0 flex-col ${className || ''}`}>
      <div
        ref={containerRef}
        data-testid="phone-video-surface"
        className="w-full min-h-0 flex-1 overflow-hidden flex items-center justify-center touch-none select-none"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={cancel}
        onLostPointerCapture={cancel}
        onWheel={wheel}
        onContextMenu={event => event.preventDefault()}
      />
      {error && (
        <div
          className="shrink-0 border-t bg-background p-3 text-sm"
          role="alert"
        >
          <p>{error}</p>
          <button
            className="mt-2 underline"
            onClick={() => {
              if (uncertain && !window.confirm(text.controlConfirm)) return;
              recoveryRequestedRef.current = uncertain;
              setAttempt(value => value + 1);
            }}
          >
            {uncertain ? text.controlRecover : text.controlReconnect}
          </button>
        </div>
      )}
    </div>
  );
}
