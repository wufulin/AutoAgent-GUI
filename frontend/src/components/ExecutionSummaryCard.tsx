import type { ExecutionRoute, ExecutionSummary } from '../api';
import { isSubtaskStatus } from '../lib/subtask-result';
import { useTranslation } from '../lib/i18n-context';

export function ExecutionSummaryCard({
  route,
  summary,
}: {
  route?: ExecutionRoute;
  summary?: ExecutionSummary | null;
}) {
  const t = useTranslation();
  if (!route && !summary) return null;
  const selectedRoute = summary?.route ?? route?.route;
  const routeReason = route?.reason
    ? (t.skills.routeReasons as Record<string, string>)[route.reason]
    : undefined;
  const stopReason = summary?.stop_reason
    ? (t.skills.stopReasons as Record<string, string>)[summary.stop_reason]
    : undefined;
  return (
    <section
      className="rounded-xl border bg-muted/30 px-4 py-3 text-sm space-y-2"
      data-testid="execution-summary"
    >
      <div className="flex flex-wrap items-center gap-2">
        <strong>{summary ? t.skills.summary : t.skills.route}</strong>
        <span className="rounded bg-background px-2 py-0.5 text-xs">
          {selectedRoute ? t.skills[selectedRoute] : ''}
        </span>
        {route?.skill_name && (
          <span>
            {route.skill_name} · v{route.skill_version}
          </span>
        )}
      </div>
      {route?.reason && (
        <p className="text-xs text-muted-foreground">
          {routeReason || t.skills.routeSelected}
        </p>
      )}
      {summary && (
        <>
          <dl className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2 text-xs">
            {[
              [t.skills.elapsed, `${(summary.elapsed_ms / 1000).toFixed(1)}s`],
              [t.skills.actions, summary.steps],
              [t.skills.subtasks, summary.subtasks],
              [t.skills.continuations, summary.continuations],
              [t.skills.plannerCalls, summary.planner_calls],
              [t.skills.executorCalls, summary.executor_calls],
              [t.skills.verifierCalls, summary.verifier_calls],
              [t.skills.inputTokens, summary.input_tokens ?? '—'],
              [t.skills.outputTokens, summary.output_tokens ?? '—'],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="font-medium tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="text-xs text-muted-foreground">
            {t.skills.status}:{' '}
            {isSubtaskStatus(summary.status)
              ? t.chatkit.status[summary.status]
              : t.chatkit.status.unknown}{' '}
            · {t.skills.stopReason}: {stopReason || t.skills.reviewDetails}
          </p>
          {summary.calls_complete === false && (
            <p className="text-xs text-amber-600">{t.skills.unknownCalls}</p>
          )}
          {!summary.usage_complete && (
            <p className="text-xs text-amber-600">{t.skills.unknownUsage}</p>
          )}
          {(!stopReason || (route?.reason && !routeReason)) && (
            <details className="text-xs text-muted-foreground">
              <summary>{t.skills.technicalDetails}</summary>
              <code>
                {[route?.reason, summary.status, summary.stop_reason]
                  .filter(Boolean)
                  .join(' · ')}
              </code>
            </details>
          )}
        </>
      )}
    </section>
  );
}
