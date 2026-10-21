import { useId } from 'react';
import type { SkillDefinition, SkillValue } from '../api';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { useTranslation } from '../lib/i18n-context';

export function SkillParameters({
  skill,
  values,
  onChange,
  disabled = false,
}: {
  skill: SkillDefinition;
  values: Record<string, SkillValue>;
  onChange: (values: Record<string, SkillValue>) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const t = useTranslation();
  return (
    <div className="space-y-3">
      {Object.entries(skill.parameters).map(([name, parameter]) => {
        const value = values[name] ?? parameter.default ?? '';
        const update = (raw: string) => {
          const next = { ...values };
          if (!raw) delete next[name];
          else
            next[name] =
              parameter.type === 'boolean'
                ? raw === 'true'
                : parameter.type === 'integer'
                  ? Number(raw)
                  : raw;
          onChange(next);
        };
        return (
          <div className="space-y-1" key={name}>
            <Label htmlFor={`${id}-${name}`}>
              {parameter.description || name}{' '}
              {parameter.required && <span className="text-red-500">*</span>}
            </Label>
            {parameter.enum || parameter.type === 'boolean' ? (
              <select
                id={`${id}-${name}`}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                value={String(value)}
                onChange={event => update(event.target.value)}
                disabled={disabled}
              >
                <option value="">— {t.skills.select} —</option>
                {(parameter.enum ?? [true, false]).map(option => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            ) : (
              <Input
                id={`${id}-${name}`}
                type={parameter.type === 'integer' ? 'number' : 'text'}
                step={parameter.type === 'integer' ? 1 : undefined}
                value={String(value)}
                onChange={event => update(event.target.value)}
                disabled={disabled}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
