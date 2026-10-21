import { AppWindow, Sparkles } from 'lucide-react';
import { skillIcons } from '../lib/skill-icons';
import type { SkillDefinition } from '../api';

export function SkillIcon({
  skill,
  className = '',
}: {
  skill: Pick<SkillDefinition, 'app' | 'icon'>;
  className?: string;
}) {
  const FunctionIcon =
    skill.icon && Object.hasOwn(skillIcons, skill.icon)
      ? skillIcons[skill.icon as keyof typeof skillIcons]
      : Sparkles;
  return (
    <span
      aria-hidden="true"
      data-app-icon={skill.app?.id || 'general'}
      className={`relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted ${className}`}
    >
      {skill.app?.id === 'wechat' ? (
        <svg viewBox="0 0 40 40" className="h-8 w-8">
          <rect width="40" height="40" rx="9" fill="#07c160" />
          <path
            d="M17 9C9 9 6 14 6 18c0 3 2 6 5 7l-1 4 5-3c1 1 2 1 3 1 7 0 12-4 12-9S24 9 17 9Z"
            fill="white"
          />
          <path
            d="M27 19c-6 0-10 3-10 7 0 5 5 8 11 8l4 2-1-3c3-2 4-4 4-7 0-4-4-7-8-7Z"
            fill="white"
            stroke="#07c160"
          />
          <g fill="#15803d">
            <circle cx="13" cy="16" r="1.2" />
            <circle cx="21" cy="16" r="1.2" />
            <circle cx="24" cy="25" r="1" />
            <circle cx="30" cy="25" r="1" />
          </g>
        </svg>
      ) : skill.app?.id === 'dianping' ? (
        <svg viewBox="0 0 40 40" className="h-8 w-8">
          <rect width="40" height="40" rx="9" fill="#ff6633" />
          <path d="M8 9h24v20H21l-6 5v-5H8Z" fill="white" />
          <text
            x="20"
            y="23"
            textAnchor="middle"
            fontSize="12"
            fontWeight="bold"
            fill="#f63"
          >
            点评
          </text>
        </svg>
      ) : skill.app ? (
        <AppWindow className="h-6 w-6 text-muted-foreground" />
      ) : (
        <FunctionIcon className="h-6 w-6 text-muted-foreground" />
      )}
      {skill.app && (
        <span className="absolute -bottom-1 -right-1 rounded-full border bg-background p-0.5">
          <FunctionIcon className="h-3 w-3" />
        </span>
      )}
    </span>
  );
}
