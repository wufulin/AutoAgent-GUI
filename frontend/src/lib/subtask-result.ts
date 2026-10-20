import type {
  SubtaskResult,
  SubtaskStatus,
  TaskEventRecordResponse,
  TaskRunResponse,
} from '../api';

const statuses: SubtaskStatus[] = [
  'completed',
  'partial',
  'blocked',
  'failed',
  'cancelled',
  'needs_user',
  'unknown',
];

export function isSubtaskStatus(value: unknown): value is SubtaskStatus {
  return typeof value === 'string' && statuses.includes(value as SubtaskStatus);
}

export function parseSubtaskResult(value: unknown): SubtaskResult | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const result = value as Record<string, unknown>;
  if (
    typeof result.subtask_id !== 'string' ||
    typeof result.goal !== 'string' ||
    !isSubtaskStatus(result.status)
  ) {
    return undefined;
  }
  return result as unknown as SubtaskResult;
}

export function taskResultStatus(
  task: TaskRunResponse,
  events: TaskEventRecordResponse[]
): SubtaskStatus | undefined {
  if (task.status === 'QUEUED' || task.status === 'RUNNING') return undefined;
  if (task.status === 'CANCELLED') return 'cancelled';
  if (task.status === 'SUCCEEDED') {
    return events.some(
      event =>
        (event.event_type === 'done' &&
          event.payload.result_status === 'completed') ||
        (event.event_type === 'tool_result' &&
          parseSubtaskResult(event.payload.result)?.status === 'completed')
    )
      ? 'completed'
      : undefined;
  }

  for (const event of [...events].reverse()) {
    if (
      ['done', 'error', 'cancelled', 'takeover'].includes(event.event_type) &&
      isSubtaskStatus(event.payload.result_status)
    ) {
      return event.payload.result_status === 'completed'
        ? 'failed'
        : event.payload.result_status;
    }
    if (event.event_type === 'takeover') return 'needs_user';
    if (event.event_type === 'tool_result') {
      const result = parseSubtaskResult(event.payload.result);
      if (result)
        return result.status === 'completed' ? 'failed' : result.status;
    }
  }
  return task.stop_reason === 'takeover' ? 'needs_user' : 'failed';
}
