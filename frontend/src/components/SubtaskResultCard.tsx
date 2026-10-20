import { Loader2 } from 'lucide-react';
import type { SubtaskResult, SubtaskStatus } from '../api';
import { useTranslation } from '../lib/i18n-context';
import { ResultStatusBadge } from './ResultStatusBadge';

export interface SubtaskView {
  subtask_id: string;
  goal: string;
  phase?: 'executing' | 'verifying';
  total_steps?: number;
  result?: SubtaskResult;
  terminal_status?: SubtaskStatus;
  terminal_message?: string;
}

export function SubtaskResultCard({ subtask }: { subtask: SubtaskView }) {
  const t = useTranslation();
  const result = subtask.result;
  const status = subtask.terminal_status ?? result?.status;
  const checks = Array.isArray(result?.verification?.criteria)
    ? result.verification.criteria
    : [];
  const facts = Array.isArray(result?.verification?.facts)
    ? result.verification.facts
    : [];
  return (
    <div
      className="rounded-lg border border-slate-200 dark:border-slate-700 p-3 space-y-2"
      data-testid="subtask-card"
      data-subtask-id={subtask.subtask_id}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{subtask.goal}</p>
        {subtask.phase ? (
          <span className="flex items-center gap-1 text-xs text-blue-600">
            <Loader2 className="w-3 h-3 animate-spin" />
            {t.chatkit.phase[subtask.phase]}
          </span>
        ) : (
          status && <ResultStatusBadge status={status} />
        )}
      </div>
      <p className="text-xs text-slate-500">
        {t.chatkit.totalSteps.replace(
          '{count}',
          String(subtask.total_steps ?? result?.total_steps ?? 0)
        )}
      </p>
      {(subtask.terminal_message || result?.message) && (
        <p className="text-sm">{subtask.terminal_message || result?.message}</p>
      )}
      {status === 'needs_user' && (
        <p className="text-sm text-blue-600">
          {t.chatkit.newTaskAfterTakeover}
        </p>
      )}
      {status === 'unknown' && (
        <p className="text-sm text-amber-700">{t.chatkit.unknownOutcomeHint}</p>
      )}
      {result && (
        <details className="text-xs">
          <summary className="cursor-pointer text-slate-500">
            {t.chatkit.verificationDetails}
          </summary>
          <div className="mt-2 space-y-2">
            {checks.length > 0 ? (
              <ul className="space-y-2">
                {checks.map((check, index) => (
                  <li key={index}>
                    <span className="font-medium">{check.criterion}</span>
                    {' — '}
                    {t.chatkit.verdict[check.verdict] ??
                      t.chatkit.verdict.unknown}
                    {check.reason && <p>{check.reason}</p>}
                  </li>
                ))}
              </ul>
            ) : (
              <p>{t.chatkit.noVerification}</p>
            )}
            {facts.map((fact, index) => (
              <p key={index}>{fact}</p>
            ))}
            {result.verification?.observed_at && (
              <p>
                {t.chatkit.observedAt}: {result.verification.observed_at}
              </p>
            )}
            {result.stop_reason && (
              <p>
                {t.chatkit.stopReason}: {result.stop_reason}
              </p>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
