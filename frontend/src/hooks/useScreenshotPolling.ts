import { useEffect, useState } from 'react';
import {
  getErrorMessage,
  getScreenshot,
  type ScreenshotResponse,
} from '../api';
import { usePageVisibility } from './usePageVisibility';

interface UseScreenshotPollingOptions {
  deviceId: string;
  enabled: boolean;
  pollDelayMs: number;
}

interface UseScreenshotPollingResult {
  screenshot: ScreenshotResponse | null;
}

export function useScreenshotPolling({
  deviceId,
  enabled,
  pollDelayMs,
}: UseScreenshotPollingOptions): UseScreenshotPollingResult {
  const isPageVisible = usePageVisibility();
  const [result, setResult] = useState<{
    deviceId: string;
    screenshot: ScreenshotResponse;
  } | null>(null);

  useEffect(() => {
    if (!deviceId || !enabled || !isPageVisible) {
      return;
    }

    let isCancelled = false;
    let timeoutId: number | undefined;
    let controller: AbortController | undefined;

    const pollScreenshots = async () => {
      controller = new AbortController();
      const requestController = controller;
      const requestTimeout = window.setTimeout(() => {
        requestController.abort();
      }, 15000);

      try {
        const data = await getScreenshot(deviceId, requestController.signal);
        if (!isCancelled) {
          setResult({
            deviceId,
            screenshot: data.success
              ? data
              : { ...data, error: data.error || 'Screenshot capture failed' },
          });
        }
      } catch (error) {
        if (!isCancelled) {
          setResult({
            deviceId,
            screenshot: {
              success: false,
              image: '',
              width: 0,
              height: 0,
              is_sensitive: false,
              error: requestController.signal.aborted
                ? 'Screenshot request timed out'
                : getErrorMessage(error),
            },
          });
        }
      } finally {
        window.clearTimeout(requestTimeout);
      }

      if (!isCancelled) {
        timeoutId = window.setTimeout(() => {
          void pollScreenshots();
        }, pollDelayMs);
      }
    };

    void pollScreenshots();

    return () => {
      isCancelled = true;
      window.clearTimeout(timeoutId);
      controller?.abort();
    };
  }, [deviceId, enabled, isPageVisible, pollDelayMs]);

  return {
    screenshot: result?.deviceId === deviceId ? result.screenshot : null,
  };
}
