import React, { useRef, useEffect, useCallback, useId, useState } from 'react';
import {
  ScrcpyPlayer,
  type PlaybackStats,
  type VideoQuality,
} from './ScrcpyPlayer';
import { ResizableHandle } from './ResizableHandle';
import { useLocalStorage } from '../hooks/useLocalStorage';
import { useScreenshotPolling } from '../hooks/useScreenshotPolling';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useTranslation } from '../lib/i18n-context';
import {
  Circle,
  Monitor,
  Settings2,
  AlertCircle,
  Loader2,
  X,
} from 'lucide-react';
import {
  shouldShowWebCodecsWarning,
  dismissWebCodecsWarning,
} from '../lib/webcodecs-utils';

interface DeviceMonitorProps {
  deviceId: string;
  serial?: string;
  connectionType?: string;
  isVisible?: boolean;
  className?: string;
}

export function DeviceMonitor({
  deviceId,
  serial: _serial,
  connectionType,
  isVisible = true,
  className = '',
}: DeviceMonitorProps) {
  const t = useTranslation();
  const settingsId = useId();
  const isRemoteDevice = connectionType === 'remote';
  const [useVideoStream, setUseVideoStream] = useState(!isRemoteDevice);
  const [videoStreamFailed, setVideoStreamFailed] = useState(false);
  const [displayMode, setDisplayMode] = useState<
    'auto' | 'video' | 'screenshot'
  >(isRemoteDevice ? 'screenshot' : 'auto');
  const [showSettings, setShowSettings] = useState(false);
  const [panelWidth, setPanelWidth] = useLocalStorage<number | 'auto'>(
    'device-monitor-width',
    320
  );
  const [quality, setQuality] = useLocalStorage<VideoQuality>(
    'device-video-quality',
    'balanced'
  );
  const [videoReady, setVideoReady] = useState(false);
  const [stats, setStats] = useState<PlaybackStats>({
    status: 'connecting',
    fps: 0,
    rtt: 0,
  });
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);
  const [showWebCodecsWarning, setShowWebCodecsWarning] = useState(false);
  const videoStreamRef = useRef<{ close: () => void } | null>(null);
  const showVideo =
    displayMode === 'video' ||
    (displayMode === 'auto' && useVideoStream && !videoStreamFailed);
  const { screenshot } = useScreenshotPolling({
    deviceId,
    enabled: isVisible && !showVideo,
    pollDelayMs: displayMode === 'screenshot' ? 750 : 1200,
  });

  const handleResize = (deltaX: number) => {
    if (typeof panelWidth !== 'number') return;
    setPanelWidth(Math.min(640, Math.max(240, panelWidth + deltaX)));
  };
  const handleVideoStreamReady = useCallback(
    (stream: { close: () => void } | null) => {
      videoStreamRef.current = stream;
      setVideoReady(!!stream);
    },
    []
  );
  const handleFallback = useCallback(
    (reason?: string) => {
      setVideoStreamFailed(true);
      setUseVideoStream(false);
      setFallbackReason(reason || null);
      if (displayMode === 'video' && reason && shouldShowWebCodecsWarning()) {
        setShowWebCodecsWarning(true);
      }
    },
    [displayMode]
  );
  const toggleDisplayMode = (mode: 'auto' | 'video' | 'screenshot') => {
    setDisplayMode(mode);
    if (mode !== 'screenshot') {
      setUseVideoStream(true);
      setVideoStreamFailed(false);
    }
  };
  useEffect(() => () => videoStreamRef.current?.close(), []);

  const getReasonMessage = (reason: string): string => {
    const messages: Record<string, string> = {
      insecure_context: t.deviceMonitor.requireHttpsOrLocalhost,
      browser_unsupported: t.deviceMonitor.browserNotSupported,
      decoder_error: t.deviceMonitor.decoderInitFailed,
      decoder_unsupported: t.deviceMonitor.codecNotSupported,
    };
    return messages[reason] || t.deviceMonitor.unknownError;
  };
  const modeLabel =
    displayMode === 'auto'
      ? `${t.devicePanel.auto} · ${showVideo ? (videoReady ? t.devicePanel.video : '…') : t.devicePanel.image}`
      : displayMode === 'video'
        ? t.devicePanel.video
        : t.devicePanel.image;
  const selectClass =
    'h-8 min-w-0 w-full rounded-md border bg-background px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';

  return (
    <Card
      data-testid="phone-monitor"
      className={`flex flex-col flex-shrink-0 relative min-h-0 overflow-hidden bg-background ${className}`}
      style={{
        width: typeof panelWidth === 'number' ? `${panelWidth}px` : 'auto',
        minWidth: typeof panelWidth === 'number' ? undefined : '240px',
        maxWidth: typeof panelWidth === 'number' ? undefined : '640px',
      }}
    >
      {typeof panelWidth === 'number' && (
        <ResizableHandle
          onResize={handleResize}
          minWidth={240}
          maxWidth={640}
        />
      )}
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
          onClick={() => setShowSettings(value => !value)}
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
          <div
            role="group"
            aria-label={t.deviceMonitor.displayMode}
            className="flex flex-wrap gap-1"
          >
            {(['auto', 'video', 'screenshot'] as const)
              .filter(mode => !isRemoteDevice || mode === 'screenshot')
              .map(mode => (
                <Button
                  key={mode}
                  variant={displayMode === mode ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-7 px-3 text-xs"
                  aria-pressed={displayMode === mode}
                  onClick={() => toggleDisplayMode(mode)}
                >
                  {mode === 'auto'
                    ? t.devicePanel.auto
                    : mode === 'video'
                      ? t.devicePanel.video
                      : t.devicePanel.image}
                </Button>
              ))}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {!isRemoteDevice && (
              <label className="min-w-0 space-y-1 text-xs text-muted-foreground">
                <span>{t.deviceMonitor.qualityLabel}</span>
                <select
                  aria-label={t.deviceMonitor.qualityLabel}
                  className={selectClass}
                  value={quality}
                  onChange={event =>
                    setQuality(event.target.value as VideoQuality)
                  }
                >
                  <option value="balanced">
                    {t.deviceMonitor.qualityBalanced}
                  </option>
                  <option value="smooth">
                    {t.deviceMonitor.qualitySmooth}
                  </option>
                  <option value="data-saver">
                    {t.deviceMonitor.qualityDataSaver}
                  </option>
                </select>
              </label>
            )}
            <label className="min-w-0 space-y-1 text-xs text-muted-foreground">
              <span>{t.deviceMonitor.widthLabel}</span>
              <select
                aria-label={t.deviceMonitor.widthLabel}
                className={selectClass}
                value={panelWidth}
                onChange={event =>
                  setPanelWidth(
                    event.target.value === 'auto'
                      ? 'auto'
                      : Number(event.target.value)
                  )
                }
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
          </div>
        </section>
      )}
      {showWebCodecsWarning && fallbackReason && (
        <div
          role="alert"
          className="shrink-0 border-b bg-amber-50 p-3 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2 font-medium">
              <AlertCircle className="size-4" />
              {t.deviceMonitor.videoUnavailableWarning}
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="size-6"
              aria-label={t.deviceMonitor.dismissNotice}
              onClick={() => {
                setShowWebCodecsWarning(false);
                dismissWebCodecsWarning();
              }}
            >
              <X className="size-3" />
            </Button>
          </div>
          <p className="mt-1">{getReasonMessage(fallbackReason)}</p>
          {fallbackReason === 'insecure_context' && (
            <Button
              variant="outline"
              size="sm"
              className="mt-2 h-7 text-xs"
              onClick={() =>
                window.open(
                  'https://github.com/suyiiyii/AutoGLM-GUI/releases',
                  '_blank',
                  'noopener,noreferrer'
                )
              }
            >
              {t.deviceMonitor.downloadElectron}
            </Button>
          )}
        </div>
      )}
      {!showVideo && screenshot?.is_sensitive && (
        <p
          role="status"
          className="shrink-0 border-b bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          {t.devicePanel.sensitiveContent}
        </p>
      )}
      <div
        data-testid="phone-monitor-stage"
        className="min-h-0 flex-1 overflow-hidden bg-muted/10 px-1"
      >
        {showVideo ? (
          <ScrcpyPlayer
            deviceId={deviceId}
            quality={quality}
            className="w-full h-full"
            enableControl
            onFallback={handleFallback}
            onStreamReady={handleVideoStreamReady}
            onPlaybackStats={setStats}
            fallbackTimeout={20000}
            isVisible={isVisible}
          />
        ) : (
          <div className="flex h-full min-h-0 w-full items-center justify-center">
            {screenshot?.success ? (
              <img
                src={`data:image/png;base64,${screenshot.image}`}
                alt="Device Screenshot"
                className="h-full w-full object-contain"
              />
            ) : screenshot?.error ? (
              <div
                role="alert"
                className="p-3 text-center text-sm text-destructive"
              >
                <AlertCircle className="mx-auto mb-2 size-6" />
                {t.devicePanel.screenshotFailed}
                <p className="mt-1 text-xs">{screenshot.error}</p>
              </div>
            ) : (
              <div className="p-3 text-center text-sm text-muted-foreground">
                <Loader2 className="mx-auto mb-2 size-6 animate-spin" />
                {t.devicePanel.loading}
              </div>
            )}
          </div>
        )}
      </div>
      <footer
        data-testid="phone-monitor-status"
        className="flex min-h-9 shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 border-t px-3 py-2 text-xs text-muted-foreground"
      >
        <span className="flex min-w-0 items-center gap-2" aria-live="polite">
          <Circle
            aria-hidden="true"
            className={`size-2.5 shrink-0 fill-current ${showVideo ? (stats.status === 'video' ? 'text-emerald-500' : 'text-amber-500') : screenshot?.success ? 'text-emerald-500' : screenshot?.error ? 'text-destructive' : 'text-amber-500'}`}
          />
          <span>{modeLabel}</span>
        </span>
        {showVideo ? (
          stats.status === 'video' ? (
            <span
              className="flex items-center gap-3 whitespace-nowrap tabular-nums"
              title={t.deviceMonitor.controlMetricHelp}
            >
              <span>{stats.fps} FPS</span>
              <span aria-label={t.deviceMonitor.controlMetric}>
                P95 {Math.round(stats.rtt)} ms
              </span>
            </span>
          ) : (
            <span role="status">
              {stats.status === 'stalled'
                ? t.deviceMonitor.controlStalled
                : stats.status === 'error'
                  ? t.deviceMonitor.controlDisconnected
                  : t.deviceMonitor.controlConnecting}
            </span>
          )
        ) : (
          <span>{t.deviceMonitor.readOnly}</span>
        )}
      </footer>
    </Card>
  );
}
