import { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useTranslation } from '../lib/i18n-context';

export function CopyButton({
  text,
  label,
  showLabel = false,
}: {
  text: string;
  label: string;
  showLabel?: boolean;
}) {
  const t = useTranslation();
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    clearTimeout(timer.current);
    setBusy(true);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const previous = document.activeElement;
        const input = document.createElement('textarea');
        input.value = text;
        input.style.position = 'fixed';
        input.style.opacity = '0';
        // Keep the fallback inside the active dialog's focus boundary.
        const container = previous?.closest('[role="dialog"]') || document.body;
        container.appendChild(input);
        try {
          input.focus();
          input.select();
          if (!document.execCommand('copy')) throw new Error('Copy failed');
        } finally {
          input.remove();
          if (previous instanceof HTMLElement) previous.focus();
        }
      }
      setStatus('copied');
    } catch {
      setStatus('failed');
    } finally {
      setBusy(false);
      timer.current = setTimeout(() => setStatus('idle'), 2500);
    }
  };
  const feedback =
    status === 'copied'
      ? t.historyPage.copied
      : status === 'failed'
        ? t.historyPage.copyFailed
        : '';
  return (
    <span className="inline-flex items-center gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={busy}
        aria-label={label}
        title={label}
        onClick={event => {
          event.stopPropagation();
          void copy();
        }}
      >
        {status === 'copied' ? (
          <Check className="h-4 w-4" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
        {showLabel && <span>{label}</span>}
      </Button>
      <span
        role="status"
        className={status === 'failed' ? 'text-xs text-red-600' : 'sr-only'}
      >
        {feedback}
      </span>
    </span>
  );
}
