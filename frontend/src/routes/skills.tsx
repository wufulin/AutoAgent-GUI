import { createFileRoute } from '@tanstack/react-router';
import { useEffect, useId, useState } from 'react';
import {
  createSkill,
  deleteSkill,
  getErrorMessage,
  listSkills,
  updateSkill,
  type SkillDefinition,
  type SkillParameter,
  type SkillStep,
  type SkillValue,
} from '../api';
import { useTranslation } from '../lib/i18n-context';
import { textLines } from '../lib/skills';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Plus, Copy, Pencil, Trash2, ArrowUp, Loader2 } from 'lucide-react';

export const Route = createFileRoute('/skills')({ component: SkillsComponent });

const newStep = (): SkillStep => ({
  id: `step-${crypto.randomUUID().slice(0, 8)}`,
  goal: '',
  success_criteria: [],
  constraints: [],
  max_steps: 8,
  max_resumes: 0,
  effect: 'write',
});
const newSkill = (): SkillDefinition => ({
  id: '',
  version: 1,
  name: '',
  description: '',
  goal: '',
  parameters: {},
  preconditions: [],
  constraints: [],
  steps: [newStep()],
  success_criteria: [],
  max_steps: 60,
  max_seconds: 300,
  enabled: true,
  builtin: false,
});
interface ParameterDraft {
  key: string;
  name: string;
  type: SkillParameter['type'];
  description: string;
  required: boolean;
  defaultText: string;
  enumText: string;
}
const parameterDrafts = (skill: SkillDefinition): ParameterDraft[] =>
  Object.entries(skill.parameters).map(([name, p]) => ({
    key: crypto.randomUUID(),
    name,
    type: p.type,
    description: p.description,
    required: p.required,
    defaultText: p.default == null ? '' : String(p.default),
    enumText: p.enum?.map(String).join('\n') ?? '',
  }));
function parameterScalar(
  value: string,
  type: SkillParameter['type']
): SkillValue {
  if (type === 'string') return value;
  if (type === 'boolean' && ['true', 'false'].includes(value))
    return value === 'true';
  if (type === 'integer' && value.trim() && Number.isSafeInteger(Number(value)))
    return Number(value);
  throw new Error(`Invalid ${type}: ${value}`);
}

function Field({
  label,
  value,
  onChange,
  multiline = false,
  type = 'text',
  disabled = false,
}: {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  multiline?: boolean;
  type?: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {multiline ? (
        <Textarea
          id={id}
          value={value}
          onChange={e => onChange(e.target.value)}
          rows={3}
          disabled={disabled}
        />
      ) : (
        <Input
          id={id}
          value={value}
          type={type}
          onChange={e => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </div>
  );
}

export function SkillsComponent() {
  const t = useTranslation();
  const [skills, setSkills] = useState<SkillDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<SkillDefinition | null>(null);
  const [parameters, setParameters] = useState<ParameterDraft[]>([]);
  const [editing, setEditing] = useState<SkillDefinition | null>(null);
  const [saving, setSaving] = useState(false);
  const load = async () => {
    try {
      setSkills((await listSkills()).skills);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    queueMicrotask(() => {
      void load();
    });
  }, []);
  const open = (skill?: SkillDefinition, copy = false) => {
    setError(null);
    setEditing(skill && !copy ? skill : null);
    const next = skill ? structuredClone(skill) : newSkill();
    if (copy) {
      next.id = `${next.id.slice(0, 50)}-copy-${crypto.randomUUID().slice(0, 6)}`;
      next.version = 1;
      next.builtin = false;
    }
    setDraft(next);
    setParameters(parameterDrafts(next));
  };
  const update = <K extends keyof SkillDefinition>(
    key: K,
    value: SkillDefinition[K]
  ) => setDraft(current => (current ? { ...current, [key]: value } : null));
  const updateStep = (index: number, changes: Partial<SkillStep>) => {
    if (draft)
      update(
        'steps',
        draft.steps.map((step, i) =>
          i === index ? { ...step, ...changes } : step
        )
      );
  };
  const save = async () => {
    if (!draft) return;
    try {
      setError(null);
      setSaving(true);
      const params: Record<string, SkillParameter> = {};
      for (const param of parameters) {
        if (
          !/^[a-z][a-z0-9_]{0,63}$/.test(param.name) ||
          Object.hasOwn(params, param.name)
        )
          throw new Error(t.skills.validation);
        params[param.name] = {
          type: param.type,
          description: param.description,
          required: param.required,
          default:
            param.defaultText === ''
              ? null
              : parameterScalar(param.defaultText, param.type),
          enum: param.enumText.trim()
            ? textLines(param.enumText).map(value =>
                parameterScalar(value, param.type)
              )
            : null,
        };
      }
      const payload = {
        ...draft,
        name: draft.name.trim(),
        parameters: params,
        preconditions: textLines(draft.preconditions.join('\n')),
        constraints: textLines(draft.constraints.join('\n')),
        success_criteria: textLines(draft.success_criteria.join('\n')),
        steps: draft.steps.map(step => ({
          ...step,
          goal: step.goal.trim(),
          success_criteria: textLines(step.success_criteria.join('\n')),
          constraints: textLines(step.constraints.join('\n')),
        })),
      };
      if (
        !/^[a-z][a-z0-9_-]{0,63}$/.test(payload.id) ||
        !payload.name ||
        !payload.goal.trim() ||
        !payload.success_criteria.length ||
        !payload.steps.length ||
        payload.steps.some(step => !step.goal || !step.success_criteria.length)
      )
        throw new Error(t.skills.validation);
      if (editing) await updateSkill(editing.id, editing.version, payload);
      else await createSkill(payload);
      setDraft(null);
      await load();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };
  const remove = async (skill: SkillDefinition) => {
    if (!window.confirm(t.skills.deleteConfirm)) return;
    try {
      await deleteSkill(skill.id, skill.version);
      await load();
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };
  return (
    <div className="container mx-auto max-w-6xl p-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">{t.skills.title}</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {t.skills.description}
          </p>
        </div>
        <Button onClick={() => open()}>
          <Plus className="mr-2 h-4 w-4" />
          {t.skills.create}
        </Button>
      </div>
      {error && !draft && (
        <p
          role="alert"
          className="rounded-lg border border-red-300 p-3 text-sm text-red-600"
        >
          {error}
        </p>
      )}
      {loading ? (
        <Loader2 className="mx-auto my-12 animate-spin" />
      ) : !skills.length ? (
        <p className="py-12 text-center text-muted-foreground">
          {t.skills.empty}
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {skills.map(skill => (
            <Card key={skill.id}>
              <CardHeader>
                <div className="flex items-center justify-between gap-2">
                  <Badge variant="secondary">
                    {skill.builtin ? t.skills.builtin : t.skills.custom}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    v{skill.version}
                  </span>
                </div>
                <CardTitle className="mt-2 text-lg">{skill.name}</CardTitle>
                {!skill.enabled && (
                  <Badge variant="outline">{t.skills.disabled}</Badge>
                )}
              </CardHeader>
              <CardContent className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  {skill.description || skill.goal}
                </p>
                <ol className="space-y-1 text-sm">
                  {skill.steps.map((step, i) => (
                    <li key={step.id} className="line-clamp-2">
                      {i + 1}. {step.goal}
                    </li>
                  ))}
                </ol>
                <p className="text-xs text-muted-foreground">
                  {t.skills.parameters}:{' '}
                  {Object.keys(skill.parameters).join(', ') || '—'} ·{' '}
                  {t.skills.maxSteps}: {skill.max_steps}
                </p>
                <div className="flex gap-2">
                  {!skill.builtin && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => open(skill)}
                    >
                      <Pencil className="mr-1 h-3 w-3" />
                      {t.common.edit}
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => open(skill, true)}
                  >
                    <Copy className="mr-1 h-3 w-3" />
                    {t.skills.copy}
                  </Button>
                  {!skill.builtin && (
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`${t.common.delete} ${skill.name}`}
                      onClick={() => void remove(skill)}
                    >
                      <Trash2 className="h-4 w-4 text-red-600" />
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
      <Dialog
        open={Boolean(draft)}
        onOpenChange={open => {
          if (!open && !saving) setDraft(null);
        }}
      >
        <DialogContent className="sm:max-w-3xl max-h-[90vh] flex flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle>
              {editing ? t.skills.edit : t.skills.create}
            </DialogTitle>
          </DialogHeader>
          {draft && (
            <div className="min-h-0 overflow-y-auto space-y-5 px-1 pb-2">
              {error && (
                <p role="alert" className="text-sm text-red-600">
                  {error}
                </p>
              )}
              <p className="rounded-lg bg-muted p-3 text-xs text-muted-foreground">
                {t.skills.templateHint}
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label={t.skills.name}
                  value={draft.name}
                  onChange={value => update('name', value)}
                />
                <Field
                  label={t.skills.identifier}
                  value={draft.id}
                  onChange={value => update('id', value)}
                  disabled={Boolean(editing)}
                />
              </div>
              <Field
                label={t.skills.detail}
                value={draft.description}
                onChange={value => update('description', value)}
              />
              <Field
                label={t.skills.goal}
                value={draft.goal}
                onChange={value => update('goal', value)}
                multiline
              />
              <section className="rounded-lg border p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold">{t.skills.parameters}</h3>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      setParameters(prev => [
                        ...prev,
                        {
                          key: crypto.randomUUID(),
                          name: '',
                          description: '',
                          type: 'string',
                          required: true,
                          defaultText: '',
                          enumText: '',
                        },
                      ])
                    }
                  >
                    {t.skills.addParameter}
                  </Button>
                </div>
                {parameters.map((parameter, index) => {
                  const change = (changes: Partial<ParameterDraft>) =>
                    setParameters(prev =>
                      prev.map((item, i) =>
                        i === index ? { ...item, ...changes } : item
                      )
                    );
                  return (
                    <div
                      className="rounded-md bg-muted/40 p-3 space-y-3"
                      key={parameter.key}
                    >
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field
                          label={t.skills.parameterName}
                          value={parameter.name}
                          onChange={name => change({ name })}
                        />
                        <label className="space-y-1.5 text-sm font-medium">
                          {t.skills.parameterType}
                          <select
                            className="block w-full rounded-md border bg-background p-2"
                            value={parameter.type}
                            onChange={e =>
                              change({
                                type: e.target.value as SkillParameter['type'],
                              })
                            }
                          >
                            {['string', 'integer', 'boolean'].map(type => (
                              <option key={type}>{type}</option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <Field
                        label={t.skills.parameterDescription}
                        value={parameter.description}
                        onChange={description => change({ description })}
                      />
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field
                          label={t.skills.defaultValue}
                          value={parameter.defaultText}
                          onChange={defaultText => change({ defaultText })}
                        />
                        <Field
                          label={t.skills.enumValues}
                          value={parameter.enumText}
                          onChange={enumText => change({ enumText })}
                          multiline
                        />
                      </div>
                      <div className="flex items-center justify-between">
                        <label className="text-sm flex gap-2 items-center">
                          <input
                            type="checkbox"
                            checked={parameter.required}
                            onChange={e =>
                              change({ required: e.target.checked })
                            }
                          />
                          {t.skills.required}
                        </label>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            setParameters(prev =>
                              prev.filter((_, i) => i !== index)
                            )
                          }
                        >
                          {t.skills.remove}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </section>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label={t.skills.preconditions}
                  value={draft.preconditions.join('\n')}
                  onChange={value => update('preconditions', value.split('\n'))}
                  multiline
                />
                <Field
                  label={t.skills.constraints}
                  value={draft.constraints.join('\n')}
                  onChange={value => update('constraints', value.split('\n'))}
                  multiline
                />
              </div>
              <section className="space-y-3">
                <div className="flex justify-between items-center">
                  <h3 className="font-semibold">{t.skills.steps}</h3>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => update('steps', [...draft.steps, newStep()])}
                  >
                    {t.skills.addStep}
                  </Button>
                </div>
                {draft.steps.map((step, index) => (
                  <div
                    className="rounded-xl border p-4 space-y-3"
                    key={step.id}
                  >
                    <div className="flex items-center justify-between">
                      <strong className="text-sm">
                        {t.skills.steps} {index + 1}
                      </strong>
                      <div className="flex gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t.skills.moveUp}
                          disabled={!index}
                          onClick={() => {
                            const steps = [...draft.steps];
                            [steps[index], steps[index - 1]] = [
                              steps[index - 1],
                              steps[index],
                            ];
                            update('steps', steps);
                          }}
                        >
                          <ArrowUp className="h-4 w-4" />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t.skills.remove}
                          disabled={draft.steps.length === 1}
                          onClick={() =>
                            update(
                              'steps',
                              draft.steps.filter((_, i) => i !== index)
                            )
                          }
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                    <Field
                      label={t.skills.stepGoal}
                      value={step.goal}
                      onChange={goal => updateStep(index, { goal })}
                    />
                    <Field
                      label={t.skills.stepCriteria}
                      value={step.success_criteria.join('\n')}
                      onChange={value =>
                        updateStep(index, {
                          success_criteria: value.split('\n'),
                        })
                      }
                      multiline
                    />
                    <Field
                      label={t.skills.constraints}
                      value={step.constraints.join('\n')}
                      onChange={value =>
                        updateStep(index, { constraints: value.split('\n') })
                      }
                      multiline
                    />
                    <div className="grid gap-3 sm:grid-cols-3">
                      <label className="space-y-1.5 text-sm font-medium">
                        {t.skills.effect}
                        <select
                          className="block w-full rounded-md border bg-background p-2"
                          value={step.effect}
                          onChange={e =>
                            updateStep(index, {
                              effect: e.target.value as SkillStep['effect'],
                              ...(e.target.value === 'write'
                                ? { max_resumes: 0 }
                                : {}),
                            })
                          }
                        >
                          {(['write', 'navigation', 'read'] as const).map(
                            effect => (
                              <option key={effect} value={effect}>
                                {t.skills[effect]}
                              </option>
                            )
                          )}
                        </select>
                      </label>
                      <Field
                        label={t.skills.maxSteps}
                        value={step.max_steps}
                        type="number"
                        onChange={value =>
                          updateStep(index, { max_steps: Number(value) })
                        }
                      />
                      <Field
                        label={t.skills.maxResumes}
                        value={step.max_resumes}
                        type="number"
                        onChange={value =>
                          updateStep(index, { max_resumes: Number(value) })
                        }
                        disabled={step.effect === 'write'}
                      />
                    </div>
                  </div>
                ))}
              </section>
              <Field
                label={t.skills.criteria}
                value={draft.success_criteria.join('\n')}
                onChange={value =>
                  update('success_criteria', value.split('\n'))
                }
                multiline
              />
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label={t.skills.maxSteps}
                  value={draft.max_steps}
                  type="number"
                  onChange={value => update('max_steps', Number(value))}
                />
                <Field
                  label={t.skills.maxSeconds}
                  value={draft.max_seconds}
                  type="number"
                  onChange={value => update('max_seconds', Number(value))}
                />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={e => update('enabled', e.target.checked)}
                />
                {t.skills.enabled}
              </label>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={saving}
              onClick={() => setDraft(null)}
            >
              {t.common.cancel}
            </Button>
            <Button disabled={saving} onClick={() => void save()}>
              {saving ? t.common.loading : t.common.save}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
