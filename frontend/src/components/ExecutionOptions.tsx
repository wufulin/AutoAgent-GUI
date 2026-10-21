import { useId } from 'react';
import type { SkillDefinition, TaskExecution } from '../api';
import { useTranslation } from '../lib/i18n-context';
import { skillDefaults } from '../lib/skills';
import { SkillParameters } from './SkillParameters';
import { Label } from './ui/label';
import { Textarea } from './ui/textarea';

export function ExecutionOptions({
  value,
  onChange,
  skills,
  disabled,
  plannerConfigured = true,
  allowedRoutes,
  defaultMaxSeconds = 300,
}: {
  value: TaskExecution;
  onChange: (value: TaskExecution) => void;
  skills: SkillDefinition[];
  disabled: boolean;
  plannerConfigured?: boolean;
  allowedRoutes?: TaskExecution['route'][];
  defaultMaxSeconds?: number;
}) {
  const t = useTranslation();
  const id = useId();
  const skill = skills.find(
    item => item.id === value.skill_id && item.version === value.skill_version
  );
  return (
    <div
      className="mb-3 rounded-lg border bg-muted/30 p-3 space-y-3"
      data-testid="execution-options"
    >
      <div className="flex flex-wrap items-center gap-3">
        <Label htmlFor={`${id}-route`}>{t.skills.route}</Label>
        <select
          id={`${id}-route`}
          className="rounded-md border bg-background px-2 py-1 text-sm"
          value={value.route}
          disabled={disabled}
          onChange={event =>
            onChange({
              ...value,
              route: event.target.value as TaskExecution['route'],
              skill_id: undefined,
              skill_version: undefined,
              parameters: {},
              success_criteria: [],
              constraints: [],
              max_steps: 60,
              max_seconds: defaultMaxSeconds,
            })
          }
        >
          {(
            allowedRoutes || (['auto', 'planner', 'direct', 'skill'] as const)
          ).map(route => (
            <option
              value={route}
              key={route}
              disabled={
                !plannerConfigured && (route === 'planner' || route === 'auto')
              }
            >
              {t.skills[route]}
            </option>
          ))}
        </select>
        {value.route === 'skill' && (
          <>
            <Label htmlFor={`${id}-skill`}>{t.skills.select}</Label>
            <select
              id={`${id}-skill`}
              className="min-w-0 max-w-full rounded-md border bg-background px-2 py-1 text-sm"
              value={value.skill_id || ''}
              disabled={disabled}
              onChange={event => {
                const selected = skills.find(
                  item => item.id === event.target.value
                );
                onChange({
                  ...value,
                  skill_id: selected?.id,
                  skill_version: selected?.version,
                  parameters: selected ? skillDefaults(selected) : {},
                  max_steps: selected?.max_steps ?? 60,
                  max_seconds: selected?.max_seconds ?? defaultMaxSeconds,
                });
              }}
            >
              <option value="">{t.skills.select}</option>
              {skills
                .filter(item => item.enabled)
                .map(item => (
                  <option value={item.id} key={item.id}>
                    {item.name} · v{item.version}
                  </option>
                ))}
            </select>
          </>
        )}
      </div>
      {(value.route === 'auto' ||
        value.route === 'direct' ||
        value.route === 'skill') && (
        <p className="text-xs text-muted-foreground">
          {t.skills[`${value.route}Hint`]}
        </p>
      )}
      {value.route === 'skill' &&
        (skill ? (
          <SkillParameters
            skill={skill}
            values={value.parameters}
            onChange={parameters => onChange({ ...value, parameters })}
            disabled={disabled}
          />
        ) : (
          <p className="text-xs text-amber-600">{t.skills.noSkills}</p>
        ))}
      {value.route === 'direct' && (
        <div className="space-y-1">
          <Label htmlFor={`${id}-criteria`}>{t.skills.criteria} *</Label>
          <Textarea
            id={`${id}-criteria`}
            value={value.success_criteria.join('\n')}
            onChange={event =>
              onChange({
                ...value,
                success_criteria: event.target.value.split('\n'),
              })
            }
            disabled={disabled}
            rows={2}
          />
        </div>
      )}
    </div>
  );
}
