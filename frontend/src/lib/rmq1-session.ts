import { useCallback, useEffect, useRef, useState } from 'react';
import { getErrorMessage, rmq1Request } from '../api';
import { Rmq1Control, type Rmq1Point, type Rmq1Result } from './rmq1-control';

export interface Rmq1Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface Rmq1Frame {
  image: string;
  mime?: string;
  width: number;
  height: number;
  content_rect?: Rmq1Rect;
  frame_id: string;
  session_id: string;
  recovery_required: boolean;
  geometry_id?: string;
  input_active?: boolean;
}
export const fullFrame: Rmq1Rect = { x: 0, y: 0, width: 1, height: 1 };
export function visibleRect(frame: Rmq1Frame | null): Rmq1Rect {
  const rect = frame?.content_rect;
  if (
    !rect ||
    ![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) ||
    rect.x < 0 ||
    rect.y < 0 ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    rect.x + rect.width > 1.000001 ||
    rect.y + rect.height > 1.000001
  )
    return fullFrame;
  return rect;
}
export const frameSource = (frame: Rmq1Frame) =>
  `data:${frame.mime === 'image/jpeg' ? 'image/jpeg' : 'image/png'};base64,${frame.image}`;
const socketUrl = (path: string, device: string) => {
  const url = new URL(`/api/rmq1/${path}`, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('device_id', device);
  return url.toString();
};
const geometry = (frame: Rmq1Frame) =>
  JSON.stringify([
    frame.session_id,
    frame.geometry_id,
    frame.width,
    frame.height,
    visibleRect(frame),
  ]);

export function useRmq1Session(deviceId: string, visible: boolean) {
  const [focused, setFocused] = useState(document.hasFocus());
  const [frame, setFrame] = useState<Rmq1Frame | null>(null);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState('');
  const [observeError, setObserveError] = useState('');
  const [status, setStatus] = useState('正在获取画面…');
  const [connected, setConnected] = useState(false);
  const [fps, setFps] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const control = useRef<Rmq1Control | null>(null);
  const loaded = useRef<{ frame: Rmq1Frame; at: number } | null>(null);
  const pending = useRef<Rmq1Frame | null>(null);
  const presented = useRef<Rmq1Frame | null>(null);
  const authorized = useRef(
    new Map<string, { frame: Rmq1Frame; at: number }>()
  );
  const identity = useRef(deviceId);
  const preview = useRef<WebSocket | null>(null);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const uncertainRef = useRef(false);
  const waitingStart = useRef<{
    frame: Rmq1Frame;
    point: Rmq1Point;
    latest?: Rmq1Point;
    end?: Rmq1Point;
  } | null>(null);
  const startTimer = useRef(0);
  const wheelPending = useRef({ delta: 0, at: 0, axis: 'y' as 'x' | 'y' });
  const wheelTimer = useRef(0);
  const wheelMotion = useRef(0);
  const wheelPlan = useRef<{ start: Rmq1Point; end: Rmq1Point } | null>(null);
  const flushWheel = useRef<() => void>(() => {});

  const cancel = useCallback(() => {
    wheelPending.current = { delta: 0, at: 0, axis: 'y' };
    window.clearTimeout(wheelTimer.current);
    window.clearInterval(wheelMotion.current);
    wheelPlan.current = null;
    if (waitingStart.current) {
      waitingStart.current = null;
      window.clearTimeout(startTimer.current);
      busyRef.current = false;
      setBusy(false);
    } else control.current?.cancel();
  }, []);

  const result = useCallback((next: Rmq1Result) => {
    waitingStart.current = null;
    window.clearTimeout(startTimer.current);
    window.clearInterval(wheelMotion.current);
    wheelPlan.current = null;
    busyRef.current = false;
    loaded.current = null;
    authorized.current.clear();
    setBusy(false);
    const unknown = next.outcome === 'unknown' || next.recovery_required;
    if (unknown) {
      uncertainRef.current = true;
      setUncertain(true);
    }
    if (!next.success) {
      wheelPending.current = { delta: 0, at: 0, axis: 'y' };
      setError(
        next.error || (unknown ? '操作结果未知' : '操作未执行，请等待新画面')
      );
    } else setError('');
    setStatus(unknown ? '需要核对手机' : '操作已释放，等待新画面');
  }, []);

  const startReady = useCallback(() => {
    const waiting = waitingStart.current;
    const current = waiting
      ? authorized.current.get(waiting.frame.frame_id)
      : null;
    if (!waiting || !current || performance.now() - current.at > 1000) return;
    waitingStart.current = null;
    window.clearTimeout(startTimer.current);
    if (
      control.current?.begin(
        waiting.frame.session_id,
        waiting.frame.frame_id,
        waiting.point
      )
    ) {
      if (waiting.end) control.current.end(waiting.end);
      else if (waiting.latest) control.current.move(waiting.latest);
    } else {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    const current = ++generation.current;
    const authorization = authorized.current;
    const socket = new WebSocket(socketUrl('control', deviceId));
    const controller = new Rmq1Control(
      message => {
        if (socket.bufferedAmount > 16384) {
          socket.close();
          throw new Error('控制发送队列已满');
        }
        if (socket.readyState !== WebSocket.OPEN)
          throw new Error('控制连接不可用');
        socket.send(JSON.stringify(message));
      },
      next => {
        if (generation.current === current) {
          result(next);
          if (next.outcome === 'unknown' || next.recovery_required)
            socket.close();
        }
      },
      () => {
        const plan = wheelPlan.current;
        if (!plan || generation.current !== current) return;
        let step = 0;
        window.clearInterval(wheelMotion.current);
        wheelMotion.current = window.setInterval(() => {
          step++;
          if (step >= 4) {
            window.clearInterval(wheelMotion.current);
            controller.end(plan.end);
          } else
            controller.move([
              plan.start[0] + ((plan.end[0] - plan.start[0]) * step) / 4,
              plan.start[1] + ((plan.end[1] - plan.start[1]) * step) / 4,
            ]);
        }, 30);
      }
    );
    control.current = controller;
    busyRef.current = false;
    const switched = identity.current !== deviceId;
    identity.current = deviceId;
    if (switched) uncertainRef.current = false;
    queueMicrotask(() => {
      if (generation.current !== current) return;
      setBusy(false);
      if (switched) setUncertain(false);
      setError('');
      setConnected(false);
    });
    socket.onopen = () => {
      if (generation.current === current) setConnected(true);
    };
    socket.onmessage = event => {
      if (generation.current !== current) return;
      try {
        const next = JSON.parse(String(event.data));
        if (next.type === 'result') controller.result(next);
        else if (next.type === 'started') controller.started(next);
      } catch {
        controller.lost();
        socket.close();
      }
    };
    socket.onclose = () => {
      if (generation.current !== current) return;
      controller.lost();
      setConnected(false);
      if (!uncertainRef.current) setError('控制连接已断开，请重新连接');
    };
    return () => {
      generation.current = current + 1;
      controller.dispose();
      control.current = null;
      loaded.current = null;
      authorization.clear();
      waitingStart.current = null;
      window.clearTimeout(startTimer.current);
      wheelPending.current = { delta: 0, at: 0, axis: 'y' };
      window.clearTimeout(wheelTimer.current);
      window.clearInterval(wheelMotion.current);
      wheelPlan.current = null;
      socket.close();
    };
  }, [deviceId, attempt, result]);

  useEffect(() => {
    const blur = () => {
      cancel();
      loaded.current = null;
      setFocused(false);
    };
    const focus = () => setFocused(true);
    window.addEventListener('blur', blur);
    window.addEventListener('focus', focus);
    return () => {
      window.removeEventListener('blur', blur);
      window.removeEventListener('focus', focus);
    };
  }, [cancel]);

  useEffect(() => {
    loaded.current = null;
    pending.current = null;
    presented.current = null;
    authorized.current.clear();
    if (!visible || !focused) {
      cancel();
      return;
    }
    let disposed = false;
    let lastGeometry = '';
    let rendered = 0;
    let failure = '';
    const socket = new WebSocket(socketUrl('preview', deviceId));
    preview.current = socket;
    queueMicrotask(() => {
      if (!disposed) {
        setFrame(null);
        setStatus('正在获取画面…');
      }
    });
    const metrics = window.setInterval(() => {
      setFps(rendered);
      rendered = 0;
    }, 1000);
    socket.onmessage = async event => {
      try {
        const next = JSON.parse(String(event.data));
        if (next.type === 'ready') {
          const shown = presented.current;
          if (shown && shown.frame_id === next.frame_id && !pending.current) {
            loaded.current = { frame: shown, at: performance.now() };
            authorized.current.set(shown.frame_id, loaded.current);
            while (authorized.current.size > 4) {
              const first = authorized.current.keys().next().value;
              if (first) authorized.current.delete(first);
            }
            startReady();
            flushWheel.current();
          }
          return;
        }
        if (next.type === 'error') throw new Error(next.error || '画面不可用');
        if (next.type !== 'frame') return;
        const decoded = new Image();
        decoded.src = frameSource(next);
        await decoded.decode();
        if (disposed || socket.readyState !== WebSocket.OPEN) return;
        if (
          decoded.naturalWidth !== next.width ||
          decoded.naturalHeight !== next.height
        )
          throw new Error('画面尺寸不匹配');
        const nextGeometry = geometry(next);
        if (lastGeometry && lastGeometry !== nextGeometry) {
          cancel();
          authorized.current.clear();
        }
        lastGeometry = nextGeometry;
        loaded.current = null;
        pending.current = next;
        setFrame(next);
        if (next.recovery_required && !next.input_active) {
          uncertainRef.current = true;
          setUncertain(true);
        }
        setObserveError('');
        setStatus('实时画面已连接');
        rendered++;
      } catch (error) {
        if (!disposed) {
          cancel();
          loaded.current = null;
          failure = getErrorMessage(error);
          setObserveError(failure);
          setStatus('画面不可用');
          socket.close();
        }
      }
    };
    socket.onclose = () => {
      if (disposed) return;
      cancel();
      loaded.current = null;
      setObserveError(failure || '实时画面已断开，请重新连接');
      setStatus('画面不可用');
    };
    return () => {
      disposed = true;
      window.clearInterval(metrics);
      loaded.current = null;
      pending.current = null;
      if (preview.current === socket) preview.current = null;
      socket.close();
    };
  }, [deviceId, visible, focused, attempt, cancel, startReady]);

  const displayed = useCallback((next: Rmq1Frame) => {
    if (
      pending.current !== next ||
      preview.current?.readyState !== WebSocket.OPEN
    )
      return;
    pending.current = null;
    presented.current = next;
    preview.current.send(
      JSON.stringify({ type: 'ack', frame_id: next.frame_id })
    );
  }, []);

  const begin = (point: Rmq1Point) => {
    if (
      !visible ||
      !focused ||
      !document.hasFocus() ||
      !connected ||
      busyRef.current ||
      uncertainRef.current ||
      !frame ||
      preview.current?.readyState !== WebSocket.OPEN
    )
      return false;
    const current = authorized.current.get(frame.frame_id);
    if (current && performance.now() - current.at > 1000) return false;
    wheelPending.current = { delta: 0, at: 0, axis: 'y' };
    busyRef.current = true;
    setBusy(true);
    // Keep the error area's height stable until input is released. Clearing
    // it here resizes the phone stage and cancels this very gesture.
    waitingStart.current = { frame, point };
    // A click during the render ACK round trip waits briefly for that exact
    // displayed frame, rather than silently dropping the user's press.
    startTimer.current = window.setTimeout(() => {
      if (!waitingStart.current) return;
      waitingStart.current = null;
      busyRef.current = false;
      setBusy(false);
      setError('画面尚未就绪，请等待新画面');
    }, 200);
    startReady();
    return true;
  };

  useEffect(() => {
    flushWheel.current = () => {
      const current = loaded.current;
      const queued = wheelPending.current;
      if (
        !queued.delta ||
        busyRef.current ||
        uncertainRef.current ||
        !current ||
        !visible ||
        !focused ||
        !connected
      )
        return;
      if (
        performance.now() - queued.at > 250 ||
        performance.now() - current.at > 1000
      ) {
        queued.delta = 0;
        return;
      }
      const rect = visibleRect(current.frame);
      const distance = Math.min(0.35, Math.abs(queued.delta));
      const delta = Math.sign(queued.delta) * distance;
      const center: Rmq1Point = [
        rect.x + rect.width / 2,
        rect.y + rect.height / 2,
      ];
      const start: Rmq1Point = [...center];
      const end: Rmq1Point = [...center];
      const axis = queued.axis === 'x' ? 0 : 1;
      const scale = axis === 0 ? rect.width : rect.height;
      start[axis] += (delta * scale) / 2;
      end[axis] -= (delta * scale) / 2;
      queued.delta = 0;
      busyRef.current = true;
      setBusy(true);
      wheelPlan.current = { start, end };
      if (
        !control.current?.begin(
          current.frame.session_id,
          current.frame.frame_id,
          start
        )
      ) {
        wheelPlan.current = null;
        busyRef.current = false;
        setBusy(false);
      }
    };
  }, [visible, focused, connected]);

  const wheel = (delta: number, axis: 'x' | 'y') => {
    if (
      !visible ||
      !focused ||
      !document.hasFocus() ||
      !connected ||
      uncertainRef.current ||
      !frame ||
      delta === 0 ||
      !Number.isFinite(delta)
    )
      return;
    const plan = wheelPlan.current;
    if (plan) {
      const activeAxis = plan.start[0] === plan.end[0] ? 'y' : 'x';
      const index = activeAxis === 'x' ? 0 : 1;
      if (
        axis !== activeAxis ||
        Math.sign(delta) !== Math.sign(plan.start[index] - plan.end[index])
      ) {
        // Release the old drag instead of finishing motion in a direction
        // the user has already reversed. Queue only the new wheel intent.
        cancel();
      }
    }
    const pending = wheelPending.current;
    if (
      performance.now() - pending.at > 250 ||
      pending.axis !== axis ||
      Math.sign(pending.delta) !== Math.sign(delta)
    )
      pending.delta = 0;
    pending.axis = axis;
    pending.delta = Math.max(-0.35, Math.min(0.35, pending.delta + delta));
    pending.at = performance.now();
    window.clearTimeout(wheelTimer.current);
    wheelTimer.current = window.setTimeout(() => flushWheel.current(), 24);
  };

  const recover = async () => {
    if (!frame || busyRef.current) return;
    const current = generation.current;
    busyRef.current = true;
    setBusy(true);
    try {
      await rmq1Request('recover', {
        device_id: deviceId,
        session_id: frame.session_id,
        confirmed_idle: true,
      });
      if (generation.current !== current) return;
      loaded.current = null;
      uncertainRef.current = false;
      setUncertain(false);
      setError('');
      setAttempt(value => value + 1);
    } catch (error) {
      if (generation.current === current) setError(getErrorMessage(error));
    } finally {
      if (generation.current === current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  const reconnect = async () => {
    if (busyRef.current || uncertainRef.current) return;
    const current = generation.current;
    busyRef.current = true;
    setBusy(true);
    setError('');
    loaded.current = null;
    authorized.current.clear();
    wheelPending.current = { delta: 0, at: 0, axis: 'y' };
    window.clearTimeout(wheelTimer.current);
    try {
      await rmq1Request('reconnect', { device_id: deviceId });
      if (generation.current !== current) return;
      setObserveError('');
      setAttempt(value => value + 1);
    } catch (error) {
      if (generation.current === current) setError(getErrorMessage(error));
    } finally {
      if (generation.current === current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  return {
    frame,
    busy,
    uncertain,
    error,
    observeError,
    status,
    connected,
    fps,
    displayed,
    begin,
    move: (point: Rmq1Point) => {
      if (waitingStart.current) waitingStart.current.latest = point;
      else control.current?.move(point);
    },
    end: (point: Rmq1Point) => {
      if (waitingStart.current) waitingStart.current.end = point;
      else control.current?.end(point);
    },
    cancel,
    wheel,
    recover,
    reconnect,
  };
}
