import { Layers, Send } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '../lib/i18n-context';
import { Rmq1Panel } from './Rmq1Panel';
import { ChatKitPanel } from './ChatKitPanel';

/** Mount the shared task UI only when the separate iPhone model is configured. */
export function Rmq1ChatPanel({
  deviceId,
  deviceName,
  configured = false,
}: {
  deviceId: string;
  deviceName: string;
  configured?: boolean;
}) {
  const t = useTranslation();
  if (configured)
    return (
      <div className="@container flex min-h-0 min-w-0 flex-1">
        <ChatKitPanel
          deviceId={deviceId}
          deviceSerial={deviceId}
          deviceName={deviceName}
          deviceConnectionType="rmq1"
          isVisible
          plannerConfigured
        />
      </div>
    );
  return (
    <div className="@container min-h-0 min-w-0 flex-1">
      <div
        data-testid="rmq1-workspace"
        className="flex h-full min-h-0 gap-4 p-4 items-stretch justify-center @max-[720px]:flex-col @max-[720px]:justify-start @max-[720px]:overflow-y-auto"
      >
        <Card
          role="region"
          aria-label="iPhone AI 助手"
          className="flex min-h-0 min-w-0 max-w-2xl flex-1 flex-col overflow-hidden @max-[720px]:min-h-72 @max-[720px]:max-w-none @max-[720px]:flex-none"
        >
          <header className="flex shrink-0 items-center justify-between gap-3 border-b p-4">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-purple-500/10">
                <Layers aria-hidden="true" className="size-5 text-purple-500" />
              </div>
              <div className="min-w-0">
                <h2 className="font-bold">{t.chatkit.title}</h2>
                <p className="truncate text-xs text-muted-foreground">
                  {deviceName}
                </p>
              </div>
            </div>
            <Badge
              variant="secondary"
              className="shrink-0 bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300"
            >
              AI 未配置
            </Badge>
          </header>
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto p-6 text-center">
            <div className="mb-4 flex size-16 shrink-0 items-center justify-center rounded-full bg-purple-100 dark:bg-purple-900/30">
              <Layers aria-hidden="true" className="size-8 text-purple-500" />
            </div>
            <h3 className="font-medium">通过对话操作 iPhone</h3>
            <p
              id="rmq1-ai-availability"
              className="mt-2 max-w-xs text-sm text-muted-foreground"
            >
              请在设置中启用并配置 iPhone 独立视觉模型。
              当前可在手机画面中手动点击和滑动。
            </p>
          </div>
          <div className="shrink-0 border-t p-4">
            <div className="flex items-end gap-3">
              <Textarea
                disabled
                aria-label="iPhone 对话任务"
                aria-describedby="rmq1-ai-availability"
                placeholder="iPhone AI 自动操作尚未启用"
                className="min-h-10 flex-1 resize-none"
                rows={1}
              />
              <Button
                disabled
                size="icon"
                aria-label="发送 iPhone 任务（尚未启用）"
                className="size-10 shrink-0 rounded-full bg-purple-600"
              >
                <Send aria-hidden="true" className="size-4" />
              </Button>
            </div>
          </div>
        </Card>
        <Rmq1Panel deviceId={deviceId} />
      </div>
    </div>
  );
}
