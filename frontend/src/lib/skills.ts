import type { SkillDefinition, SkillValue } from '../api';

export function skillDefaults(
  skill: SkillDefinition
): Record<string, SkillValue> {
  return Object.fromEntries(
    Object.entries(skill.parameters)
      .filter(([, parameter]) => parameter.default != null)
      .map(([name, parameter]) => [name, parameter.default as SkillValue])
  );
}

export function validSkillParameters(
  skill: SkillDefinition,
  values: Record<string, SkillValue>
): boolean {
  return Object.entries(skill.parameters).every(([name, parameter]) => {
    const value = values[name] ?? parameter.default;
    if (value == null || value === '') return !parameter.required;
    if (
      parameter.type === 'integer' &&
      (typeof value !== 'number' || !Number.isInteger(value))
    )
      return false;
    if (parameter.type === 'boolean' && typeof value !== 'boolean')
      return false;
    if (
      parameter.type === 'string' &&
      (typeof value !== 'string' || !value.trim())
    )
      return false;
    return !parameter.enum || parameter.enum.includes(value);
  });
}

export function textLines(value: string): string[] {
  return value
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean);
}
