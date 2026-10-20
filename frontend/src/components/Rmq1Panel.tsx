import {
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent,
} from 'react';
import { Circle, Loader2, Monitor, Settings2 } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { useLocalStorage } from '../hooks/useLocalStorage';
import { useTranslation } from '../lib/i18n-context';
import { Button } from '@/components/ui/button';
import { getErrorMessage, rmq1Request } from '../api';
import { usePageVisibility } from '../hooks/usePageVisibility';

interface ContentRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

const fullFrame: ContentRect = { x: 0, y: 0, width: 1, height: 1 };

function visibleRect(frame: Frame | null): ContentRect {
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

interface Frame {
  image: string;
  width: number;
  height: number;
  content_rect?: ContentRect;
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
  const t = useTranslation();
  const settingsId = useId();
  const [showSettings, setShowSettings] = useState(false);
  const [hideBars, setHideBars] = useState(true);
  const [panelWidth, setPanelWidth] = useLocalStorage<number | 'auto'>(
    'device-monitor-width',
    320
  );
  const stage = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 });
  const [observeError, setObserveError] = useState('');
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
    const element = stage.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      drag.current = null;
      setStageSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

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
            const decoded = image.current;
            if (
              decoded?.complete &&
              decoded.naturalWidth === next.width &&
              decoded.naturalHeight === next.height &&
              decoded.getAttribute('src') ===
                `data:image/png;base64,${next.image}`
            ) {
              loaded.current = { frame: next, at: performance.now() };
            }
            setFrame(next);
            setUncertain(previous => previous || next.recovery_required);
            setStatus('画面已连接');
            setObserveError('');
          }
        } catch (error) {
          if (!cancelled) {
            loaded.current = null;
            setFrame(null);
            setStatus('画面不可用');
            setObserveError(getErrorMessage(error));
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
    const visibleBounds = viewport.current?.getBoundingClientRect();
    if (!bounds || !bounds.width || !bounds.height || !visibleBounds)
      return null;
    // The image extends beyond the clipped phone region. Captured pointers must
    // still end inside that visible region before a gesture can be submitted.
    if (
      clientX < visibleBounds.left ||
      clientX > visibleBounds.right ||
      clientY < visibleBounds.top ||
      clientY > visibleBounds.bottom
    )
      return null;
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

  const rect = hideBars ? visibleRect(frame) : fullFrame;
  const aspect = frame
    ? (frame.width * rect.width) / (frame.height * rect.height)
    : 0.5;
  const monitorWidth =
    panelWidth === 'auto'
      ? Math.min(640, Math.max(240, stageSize.height * aspect))
      : typeof panelWidth === 'number' && Number.isFinite(panelWidth)
        ? Math.min(640, Math.max(240, panelWidth))
        : 320;
  const displayWidth = Math.max(
    0,
    Math.min(stageSize.width, stageSize.height * aspect)
  );

  return (
    <Card
      role="region"
      aria-label="RMQ1 iPhone"
      data-testid="phone-monitor"
      className="relative flex min-h-0 min-w-0 w-[var(--rmq1-monitor-width)] max-w-[calc(100%_-_336px)] shrink-0 flex-col overflow-hidden bg-background @max-[720px]:h-[560px] @max-[720px]:w-full @max-[720px]:max-w-full"
      style={{ '--rmq1-monitor-width': `${monitorWidth}px` } as CSSProperties}
    >
      <header
        data-testid="phone-monitor-header"
        className="flex min-h-12 shrink-0 items-center justify-between gap-2 border-b px-3 py-2"
      >
        <div className="flex min-w-0 items-center gap-2 text-sm font-semibold">
          <Monitor
            aria-hidden="true"
            className="size-4 shrink-0 text-slate-600 dark:text-slate-300"
          />
          <span className="truncate">{t.deviceMonitor.screenTitle}</span>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0 gap-1.5 px-2 text-xs shadow-none"
          onClick={() => {
            drag.current = null;
            setShowSettings(value => !value);
          }}
          aria-expanded={showSettings}
          aria-controls={settingsId}
        >
          <Settings2 aria-hidden="true" className="size-3.5" />
          {t.deviceMonitor.displaySettings}
        </Button>
      </header>
      {showSettings && (
        <section
          id={settingsId}
          aria-label={t.deviceMonitor.displaySettings}
          data-testid="phone-monitor-settings"
          className="max-h-[45%] shrink-0 space-y-3 overflow-y-auto border-b bg-muted/30 p-3"
        >
          <label className="block space-y-1 text-xs text-muted-foreground">
            <span>{t.deviceMonitor.widthLabel}</span>
            <select
              aria-label={t.deviceMonitor.widthLabel}
              value={panelWidth}
              className="h-8 w-full min-w-0 rounded-md border bg-background px-2 text-xs"
              onChange={event => {
                drag.current = null;
                setPanelWidth(
                  event.target.value === 'auto'
                    ? 'auto'
                    : Number(event.target.value)
                );
              }}
            >
              {typeof panelWidth === 'number' &&
                ![320, 400, 480].includes(panelWidth) && (
                  <option value={panelWidth}>{panelWidth} px</option>
                )}
              <option value={320}>{t.deviceMonitor.compact}</option>
              <option value={400}>{t.deviceMonitor.standard}</option>
              <option value={480}>{t.deviceMonitor.wide}</option>
              <option value="auto">{t.deviceMonitor.auto}</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={hideBars}
              onChange={event => {
                drag.current = null;
                setHideBars(event.target.checked);
              }}
            />
            隐藏画面黑边
          </label>
        </section>
      )}
      <div
        ref={stage}
        data-testid="phone-monitor-stage"
        className="flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-muted/10"
      >
        {frame ? (
          <div
            ref={viewport}
            data-testid="rmq1-phone-viewport"
            className="relative shrink-0 overflow-hidden"
            style={{ width: displayWidth, height: displayWidth / aspect }}
          >
            <img
              ref={image}
              src={`data:image/png;base64,${frame.image}`}
              alt="iPhone 实时画面"
              draggable={false}
              className="absolute max-w-none select-none"
              style={{
                touchAction: 'none',
                width: `${100 / rect.width}%`,
                height: `${100 / rect.height}%`,
                left: `${(-100 * rect.x) / rect.width}%`,
                top: `${(-100 * rect.y) / rect.height}%`,
                cursor: busy || uncertain ? 'not-allowed' : 'crosshair',
              }}
              onLoad={() => {
                if (active.current)
                  loaded.current = { frame, at: performance.now() };
              }}
              onError={() => {
                loaded.current = null;
                drag.current = null;
                setObserveError('画面加载失败，请等待新画面');
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
          </div>
        ) : (
          <div className="p-4 text-center text-sm text-muted-foreground">
            <Loader2
              aria-hidden="true"
              className="mx-auto mb-2 size-6 animate-spin"
            />
            等待 iPhone 画面…
          </div>
        )}
      </div>
      <div className="max-h-[40%] shrink-0 overflow-y-auto border-t px-3 py-2">
        <p className="text-xs text-muted-foreground">
          点击操作；拖动并松开后滑动。
        </p>
        {(error || observeError) && (
          <p role="alert" className="mt-2 text-xs text-destructive">
            {error || observeError}
          </p>
        )}
        {uncertain && frame && (
          <Button
            variant="outline"
            className="h-auto w-full whitespace-normal text-xs"
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
      </div>
      <footer
        data-testid="phone-monitor-status"
        className="flex min-h-9 shrink-0 flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-xs text-muted-foreground"
      >
        <span className="flex items-center gap-2">
          <Circle
            aria-hidden="true"
            className={`size-2.5 shrink-0 fill-current ${frame && !uncertain && !observeError ? 'text-emerald-500' : 'text-amber-500'}`}
          />
          RMQ1 · 手动控制
        </span>
        <span role="status">
          {busy ? '正在操作…' : uncertain ? '需要核对手机' : status}
        </span>
      </footer>
    </Card>
  );
}
