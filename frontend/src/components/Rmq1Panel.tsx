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
import {
  frameSource,
  fullFrame,
  useRmq1Session,
  visibleRect,
} from '../lib/rmq1-session';
import { usePageVisibility } from '../hooks/usePageVisibility';

export function Rmq1Panel({
  deviceId,
  readOnly = false,
}: {
  deviceId: string;
  readOnly?: boolean;
}) {
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
  const visible = usePageVisibility();
  const image = useRef<HTMLImageElement>(null);
  const session = useRmq1Session(deviceId, visible);
  const {
    frame,
    busy,
    uncertain,
    error,
    observeError,
    status,
    cancel,
    displayed,
  } = session;
  const drag = useRef<number | null>(null);
  const release = () => {
    const pointer = drag.current;
    drag.current = null;
    if (pointer !== null && image.current?.hasPointerCapture(pointer))
      image.current.releasePointerCapture(pointer);
  };

  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      cancel();
      setStageSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [cancel]);

  useEffect(() => {
    if (!busy) {
      const pointer = drag.current;
      drag.current = null;
      if (pointer !== null && image.current?.hasPointerCapture(pointer))
        image.current.releasePointerCapture(pointer);
    }
  }, [busy]);

  // Identical pixels may arrive with a new frame ID without an img load event.
  // ACK only after the decoded image and matching geometry reach the display.
  useEffect(() => {
    if (!frame) return;
    const animation = requestAnimationFrame(() => {
      const element = image.current;
      if (
        element?.complete &&
        element.naturalWidth === frame.width &&
        element.naturalHeight === frame.height &&
        element.src === frameSource(frame)
      )
        displayed(frame);
    });
    return () => cancelAnimationFrame(animation);
  }, [frame, displayed]);

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

  const wheelHandler = useRef(session.wheel);
  useEffect(() => {
    wheelHandler.current = session.wheel;
  });
  const hasFrame = !!frame;
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (readOnly || drag.current !== null) return;
      const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY);
      const size = horizontal ? element.clientWidth : element.clientHeight;
      const unit =
        event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size : 1;
      const delta = horizontal ? event.deltaX : event.deltaY;
      if (size > 0)
        wheelHandler.current((delta * unit) / size, horizontal ? 'x' : 'y');
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [hasFrame, readOnly]);

  const finish = (event: PointerEvent<HTMLImageElement>) => {
    if (drag.current !== event.pointerId) return;
    const end = point(event.clientX, event.clientY);
    release();
    if (!end || !visible || !document.hasFocus()) cancel();
    else session.end(end);
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
            cancel();
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
                cancel();
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
                cancel();
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
              src={frameSource(frame)}
              alt="iPhone 实时画面"
              draggable={false}
              className="absolute max-w-none select-none"
              style={{
                touchAction: 'none',
                width: `${100 / rect.width}%`,
                height: `${100 / rect.height}%`,
                left: `${(-100 * rect.x) / rect.width}%`,
                top: `${(-100 * rect.y) / rect.height}%`,
                cursor:
                  readOnly || busy || uncertain ? 'not-allowed' : 'crosshair',
              }}
              onLoad={() => {
                requestAnimationFrame(() => displayed(frame));
              }}
              onPointerDown={event => {
                if (
                  readOnly ||
                  event.button !== 0 ||
                  !event.isPrimary ||
                  drag.current !== null
                )
                  return;
                const start = point(event.clientX, event.clientY);
                if (!start || !session.begin(start)) return;
                drag.current = event.pointerId;
                event.currentTarget.setPointerCapture(event.pointerId);
                event.preventDefault();
              }}
              onPointerMove={event => {
                if (drag.current !== event.pointerId) return;
                const next = point(event.clientX, event.clientY);
                if (next) session.move(next);
                else {
                  release();
                  cancel();
                }
              }}
              onPointerUp={finish}
              onPointerCancel={() => {
                release();
                cancel();
              }}
              onLostPointerCapture={() => {
                if (drag.current !== null) {
                  drag.current = null;
                  cancel();
                }
              }}
              onContextMenu={event => event.preventDefault()}
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
          点击、按住拖动或使用滚轮 / 触控板滚动。单次按住最多 1.5 秒。
        </p>
        {(!session.connected || observeError) && (
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            disabled={busy}
            onClick={session.reconnect}
          >
            重新连接画面与控制
          </Button>
        )}
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
            onClick={() => void session.recover()}
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
          RMQ1 · {session.fps} FPS
        </span>
        <span role="status">
          {busy ? '正在操作…' : uncertain ? '需要核对手机' : status}
        </span>
      </footer>
    </Card>
  );
}
