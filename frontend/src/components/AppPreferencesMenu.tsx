import { Globe, Settings2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { useLocale, useTranslation } from '../lib/i18n-context';
import { useFooterVersionInfo } from '../hooks/useFooterVersionInfo';
import { ThemeToggle } from './ThemeToggle';

export function AppPreferencesMenu() {
  const t = useTranslation();
  const { locale, setLocale, localeName } = useLocale();
  const buildVersion = __BACKEND_VERSION__ || 'unknown';
  const { backendVersion, updateInfo, showUpdateBadge, versionMismatch } =
    useFooterVersionInfo(buildVersion);
  const versionTitle =
    versionMismatch && backendVersion
      ? t.footer.versionMismatchDetail
          .replace('{frontend}', buildVersion)
          .replace('{backend}', backendVersion)
      : t.footer.buildVersion.replace('{version}', buildVersion);

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="size-10 rounded-lg text-slate-500"
          aria-label={t.footer.preferences}
          title={t.footer.preferences}
        >
          <Settings2 className="size-5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="right"
        align="end"
        sideOffset={12}
        className="w-64 space-y-4"
        aria-label={t.footer.preferences}
      >
        <p className="text-sm font-semibold">{t.footer.preferences}</p>
        <div className="flex items-center justify-between">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setLocale(locale === 'en' ? 'zh' : 'en')}
            aria-label="Switch language"
          >
            <Globe className="size-4" />
            {localeName}
          </Button>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {t.footer.theme}
            <ThemeToggle />
          </div>
        </div>
        <div className="space-y-2 border-t pt-3 text-xs text-muted-foreground">
          <p title={versionTitle}>
            v{backendVersion ?? buildVersion}
            {__GIT_HASH__ !== 'unknown' && (
              <span className="ml-2 font-mono">{__GIT_HASH__}</span>
            )}
          </p>
          {versionMismatch && (
            <Badge variant="warning">{t.footer.versionMismatch}</Badge>
          )}
          {showUpdateBadge && updateInfo?.latest_version && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => {
                if (updateInfo.release_url)
                  window.open(
                    updateInfo.release_url,
                    '_blank',
                    'noopener,noreferrer'
                  );
              }}
              title={t.footer.updateAvailable.replace(
                '{version}',
                updateInfo.latest_version
              )}
            >
              {t.footer.newVersion}
            </Button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
