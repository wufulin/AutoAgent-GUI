import { AlertCircle, CheckCircle2 } from 'lucide-react';
import type { SubtaskStatus } from '../api';
import { useTranslation } from '../lib/i18n-context';
import { isSubtaskStatus } from '../lib/subtask-result';
import { Badge } from './ui/badge';

export function ResultStatusBadge({
  status,
  success = false,
}: {
  status?: SubtaskStatus | string | null;
  success?: boolean;
}) {
  const t = useTranslation();
  const outcome = isSubtaskStatus(status)
    ? status
    : success
      ? 'completed'
      : 'failed';
  const Icon = outcome === 'completed' ? CheckCircle2 : AlertCircle;
  return (
    <Badge
      variant={
        outcome === 'completed'
          ? 'success'
          : outcome === 'failed'
            ? 'destructive'
            : 'secondary'
      }
      data-result-status={outcome}
    >
      <Icon className="w-3 h-3 mr-1" />
      {isSubtaskStatus(status)
        ? t.chatkit.status[outcome]
        : success
          ? t.history.success
          : t.history.failed}
    </Badge>
  );
}
