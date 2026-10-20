import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { Button } from '@/components/ui/button';
import { getErrorMessage, rmq1Request } from '../api';
import { usePageVisibility } from '../hooks/usePageVisibility';

interface Frame {
  image: string;
  width: number;
  height: number;
  frame_id: string;
  session_id: string;
  recovery_required: boolean;
}

interface GestureResult {
  success: boolean;
  error?: string;
  recovery_required: boolean;
}

export function Rmq1Panel({ deviceId }: { deviceId: string }) {
  const [frame, setFrame] = useState<Frame | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [status, setStatus] = useState('正在获取画面…');
  const visible = usePageVisibility();
  const image = useRef<HTMLImageElement>(null);
  const loaded = useRef<{ frame: Frame; at: number } | null>(null);
  const active = useRef(false);
  const busyRef = useRef(false);
  const drag = useRef<{
    point: [number, number];
    observed: Frame;
    pointer: number;
    at: number;
  } | null>(null);

  useEffect(() => {
    active.current = true;
    let cancelled = false;
    let timer: number | undefined;
    loaded.current = null;
    const poll = async () => {
      if (!visible || cancelled) return;
      if (!drag.current && !busyRef.current) {
        const started = performance.now();
        try {
          const next = await rmq1Request<Frame>('observe', {
            device_id: deviceId,
          });
          if (!cancelled && !drag.current && !busyRef.current) {
            if (performance.now() - started > 2000)
              throw new Error('画面传输过慢，请等待新画面');
            if (loaded.current?.frame.image === next.image) {
              loaded.current = { frame: next, at: performance.now() };
            }
            setFrame(next);
            setUncertain(previous => previous || next.recovery_required);
            setStatus('画面已连接');
          }
        } catch (error) {
          if (!cancelled) {
            loaded.current = null;
            setFrame(null);
            setStatus('画面不可用');
            setError(getErrorMessage(error));
          }
        }
      }
      if (!cancelled) timer = window.setTimeout(poll, 500);
    };
    void poll();
    const cancelGesture = () => {
      drag.current = null;
    };
    window.addEventListener('blur', cancelGesture);
    return () => {
      cancelled = true;
      active.current = false;
      loaded.current = null;
      drag.current = null;
      window.clearTimeout(timer);
      window.removeEventListener('blur', cancelGesture);
    };
  }, [deviceId, visible]);

  const point = (clientX: number, clientY: number): [number, number] | null => {
    const bounds = image.current?.getBoundingClientRect();
    if (!bounds || !bounds.width || !bounds.height) return null;
    const x = (clientX - bounds.left) / bounds.width;
    const y = (clientY - bounds.top) / bounds.height;
    return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? [x, y] : null;
  };

  const finish = async (event: PointerEvent<HTMLImageElement>) => {
    const current = drag.current;
    drag.current = null;
    if (
      !current ||
      current.pointer !== event.pointerId ||
      !visible ||
      !document.hasFocus()
    )
      return;
    const end = point(event.clientX, event.clientY);
    if (!end || performance.now() - current.at > 2000) return;
    const moved =
      Math.hypot(end[0] - current.point[0], end[1] - current.point[1]) > 0.015;
    busyRef.current = true;
    setBusy(true);
    setError('');
    loaded.current = null;
    try {
      const result = await rmq1Request<GestureResult>('gesture', {
        device_id: deviceId,
        action_id: crypto.randomUUID(),
        session_id: current.observed.session_id,
        frame_id: current.observed.frame_id,
        start: current.point,
        end: moved ? end : null,
        duration_ms: moved ? 400 : 100,
      });
      if (active.current) {
        setUncertain(result.recovery_required);
        if (!result.success) setError(result.error || '操作结果未知');
        setStatus(result.success ? '已发送操作，正在刷新画面' : '需要核对手机');
      }
    } catch (error) {
      if (active.current) {
        // A lost HTTP response does not prove whether the gesture ran.
        setUncertain(true);
        setError(`${getErrorMessage(error)}。请先核对手机，勿重复操作。`);
      }
    } finally {
      busyRef.current = false;
      if (active.current) setBusy(false);
    }
  };

  return (
    <section
      className="flex min-h-0 w-full flex-col items-center gap-3 rounded-xl border bg-background p-4"
      aria-label="RMQ1 iPhone"
    >
      <h2 className="text-base font-semibold">iPhone · RMQ1</h2>
      <p className="text-sm text-muted-foreground">
        点击画面操作；拖动并松开后执行滑动。AI 任务和键盘输入暂不可用。
      </p>
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden">
        {frame ? (
          <img
            ref={image}
            src={`data:image/png;base64,${frame.image}`}
            alt="iPhone 实时画面"
            draggable={false}
            className="max-h-full max-w-full select-none object-contain"
            style={{
              touchAction: 'none',
              width: 'auto',
              height: 'auto',
              cursor: busy || uncertain ? 'not-allowed' : 'crosshair',
            }}
            onLoad={() => {
              loaded.current = { frame, at: performance.now() };
            }}
            onPointerDown={event => {
              const observed = loaded.current;
              const start = point(event.clientX, event.clientY);
              if (
                event.button !== 0 ||
                !event.isPrimary ||
                busyRef.current ||
                uncertain ||
                !visible ||
                !document.hasFocus() ||
                !observed ||
                !start ||
                performance.now() - observed.at > 1500
              )
                return;
              drag.current = {
                point: start,
                observed: observed.frame,
                pointer: event.pointerId,
                at: performance.now(),
              };
              event.currentTarget.setPointerCapture(event.pointerId);
              event.preventDefault();
            }}
            onPointerUp={event => {
              void finish(event);
            }}
            onPointerCancel={() => {
              drag.current = null;
            }}
            onLostPointerCapture={() => {
              drag.current = null;
            }}
          />
        ) : (
          <p className="p-8 text-muted-foreground">等待 iPhone 画面…</p>
        )}
      </div>
      <p role="status" className="text-xs text-muted-foreground">
        {busy ? '正在操作…' : status}
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {uncertain && frame && (
        <Button
          variant="outline"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            busyRef.current = true;
            try {
              await rmq1Request('recover', {
                device_id: deviceId,
                session_id: frame.session_id,
                confirmed_idle: true,
              });
              loaded.current = null;
              setUncertain(false);
              setError('');
            } catch (error) {
              setError(getErrorMessage(error));
            } finally {
              busyRef.current = false;
              setBusy(false);
            }
          }}
        >
          我已核对手机状态、释放按键，恢复控制
        </Button>
      )}
    </section>
  );
}
