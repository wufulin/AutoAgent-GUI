import { Select } from 'radix-ui';
import { Check, ChevronDown } from 'lucide-react';
import type { SkillDefinition } from '../api';
import { groupSkills } from '../lib/skills';
import { useTranslation } from '../lib/i18n-context';
import { SkillIcon } from './SkillIcon';

export function SkillPicker({
  id,
  skills,
  value,
  onChange,
  disabled = false,
}: {
  id: string;
  skills: SkillDefinition[];
  value?: string;
  onChange: (skill: SkillDefinition) => void;
  disabled?: boolean;
}) {
  const t = useTranslation();
  const selected = skills.find(
    skill => `${skill.id}@${skill.version}` === value
  );
  const groups = groupSkills(
    skills.filter(skill => skill.enabled),
    t.skills.general
  );
  return (
    <Select.Root
      value={selected ? value : ''}
      onValueChange={key => {
        const next = skills.find(
          skill => `${skill.id}@${skill.version}` === key
        );
        if (next?.enabled) onChange(next);
      }}
      disabled={disabled}
    >
      <Select.Trigger
        id={id}
        aria-label={t.skills.select}
        className="flex min-h-10 min-w-0 w-full items-center gap-2 rounded-md border bg-background px-3 py-2 text-left text-sm focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50"
      >
        {selected && <SkillIcon skill={selected} />}
        <span className="min-w-0 flex-1 truncate">
          {selected
            ? `${selected.name} · v${selected.version} · ${selected.builtin ? t.skills.builtin : t.skills.custom}`
            : t.skills.select}
        </span>
        <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0" />
      </Select.Trigger>
      <Select.Portal>
        <Select.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-[var(--radix-select-content-available-height)] w-[var(--radix-select-trigger-width)] min-w-0 overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md"
        >
          <Select.Viewport className="max-h-80 p-1">
            {groups.length === 0 && (
              <p className="p-3 text-sm text-muted-foreground">
                {t.skills.noSkills}
              </p>
            )}
            {groups.map(group => (
              <Select.Group key={group.key}>
                <Select.Label className="px-2 py-2 text-xs font-semibold text-muted-foreground">
                  {group.name}
                </Select.Label>
                {group.skills.map(skill => (
                  <Select.Item
                    key={`${skill.id}@${skill.version}`}
                    value={`${skill.id}@${skill.version}`}
                    textValue={skill.name}
                    className="relative flex cursor-pointer items-center gap-3 rounded-sm py-2 pl-2 pr-8 text-sm outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground"
                  >
                    <SkillIcon skill={skill} />
                    <Select.ItemText className="min-w-0 flex-1">
                      <span className="break-words">
                        {skill.name} · v{skill.version}
                        <span className="ml-2 text-xs text-muted-foreground">
                          {skill.builtin ? t.skills.builtin : t.skills.custom}
                        </span>
                      </span>
                    </Select.ItemText>
                    <Select.ItemIndicator className="absolute right-2">
                      <Check aria-hidden="true" className="h-4 w-4" />
                    </Select.ItemIndicator>
                  </Select.Item>
                ))}
              </Select.Group>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
