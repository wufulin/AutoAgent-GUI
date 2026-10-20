import React, { useState } from 'react';
import {
  Edit,
  Loader2,
  Server,
  Smartphone,
  Trash2,
  Wifi,
  WifiOff,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ConfirmDialog } from './ConfirmDialog';
import { useTranslation } from '../lib/i18n-context';
import { removeRemoteDevice, rmq1Request, updateDeviceName } from '../api';
import type { AgentStatus } from '../api';
import type { ToastType } from './Toast';

interface DeviceCardProps {
  id: string;
  serial: string;
  model: string;
  status: string;
  connectionType?: string;
  displayName?: string | null;
  agent?: AgentStatus | null;
  isActive: boolean;
  onClick: () => void;
  onConnectWifi?: () => Promise<void>;
  onDisconnectWifi?: () => Promise<void>;
  onNameUpdated?: () => void;
  showToast?: (message: string, type: ToastType) => void;
  groupAction?: React.ReactNode;
}

export function DeviceCard({
  id: _id,
  serial,
  model,
  status,
  connectionType,
  displayName,
  agent,
  isActive,
  onClick,
  onConnectWifi,
  onDisconnectWifi,
  onNameUpdated,
  showToast,
  groupAction,
}: DeviceCardProps) {
  const t = useTranslation();
  const isOnline = status === 'device';
  const isUsb = connectionType === 'usb';
  const isWifi = connectionType === 'wifi';
  const isRmq1 = connectionType === 'rmq1';
  const isRemote = connectionType === 'remote' || isRmq1;
  const [loading, setLoading] = useState(false);
  const [showWifiConfirm, setShowWifiConfirm] = useState(false);
  const [showDisconnectConfirm, setShowDisconnectConfirm] = useState(false);
  const [showEditDialog, setShowEditDialog] = useState(false);
  const [editingName, setEditingName] = useState('');
  const [saving, setSaving] = useState(false);

  const actualDisplayName = displayName || model || t.deviceCard.unknownDevice;

  // Determine agent status indicator class and tooltip
  const getAgentStatusClass = () => {
    if (!isOnline) return 'status-agent-none';
    if (!agent) return 'status-agent-none';
    switch (agent.state) {
      case 'idle':
        return 'status-agent-idle';
      case 'busy':
        return 'status-agent-busy';
      case 'error':
        return 'status-agent-error';
      case 'initializing':
        return 'status-agent-initializing';
      default:
        return 'status-agent-none';
    }
  };

  const getCurrentStatusText = () => {
    if (!isOnline) return t.deviceCard.statusTooltip.none;
    if (!agent) return t.deviceCard.statusTooltip.none;
    switch (agent.state) {
      case 'idle':
        return t.deviceCard.statusTooltip.idle;
      case 'busy':
        return t.deviceCard.statusTooltip.busy;
      case 'error':
        return t.deviceCard.statusTooltip.error;
      case 'initializing':
        return t.deviceCard.statusTooltip.initializing;
      default:
        return t.deviceCard.statusTooltip.none;
    }
  };

  const handleWifiClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (loading || !onConnectWifi) return;
    setShowWifiConfirm(true);
  };

  const handleDisconnectClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (loading || !onDisconnectWifi) return;
    setShowDisconnectConfirm(true);
  };

  const handleConfirmWifi = async () => {
    setShowWifiConfirm(false);
    setLoading(true);
    try {
      if (onConnectWifi) {
        await onConnectWifi();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleConfirmDisconnect = async () => {
    setShowDisconnectConfirm(false);
    setLoading(true);
    try {
      if (onDisconnectWifi) {
        await onDisconnectWifi();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleEditClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingName(displayName || model || '');
    setShowEditDialog(true);
  };

  const handleSaveName = async () => {
    try {
      setSaving(true);
      const trimmedName = editingName.trim();
      const response = await updateDeviceName(serial, trimmedName || null);

      if (!response.success) {
        if (showToast) {
          showToast(response.error || t.deviceCard.saveNameError, 'error');
        }
        return;
      }

      setShowEditDialog(false);
      if (onNameUpdated) {
        onNameUpdated();
      }
      if (showToast) {
        showToast(t.deviceCard.saveNameSuccess, 'success');
      }
    } catch (error) {
      console.error('Failed to update device name:', error);
      if (showToast) {
        showToast(t.deviceCard.saveNameError, 'error');
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div
        data-testid="device-card"
        className={`
          group relative w-full text-left p-3 rounded-xl transition-all duration-200 cursor-pointer
          border
          ${
            isActive
              ? 'bg-sky-50/70 border-sky-300 dark:bg-sky-950/25 dark:border-sky-700'
              : 'bg-white border-transparent hover:border-slate-200 dark:bg-slate-900 dark:hover:border-slate-700'
          }
        `}
      >
        <button
          type="button"
          data-testid="device-select"
          aria-label={actualDisplayName}
          aria-pressed={isActive}
          title={`${actualDisplayName}\n${serial}`}
          onClick={onClick}
          className="absolute inset-0 rounded-xl cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#1d9bf0]"
        />
        <div className="pointer-events-none flex items-center gap-2">
          <div className="relative size-4 shrink-0 self-start mt-0.5">
            <Smartphone
              className={`w-4 h-4 flex-shrink-0 ${
                isActive
                  ? 'text-[#1d9bf0]'
                  : 'text-slate-400 dark:text-slate-500'
              }`}
            />

            {/* Agent status indicator with tooltip */}
            <Tooltip>
              <TooltipTrigger asChild>
                <div
                  className={`pointer-events-auto z-10 ${getAgentStatusClass()} absolute -bottom-0.5 -right-0.5 w-2 h-2 ring-2 ring-white dark:ring-slate-950 rounded-full transition-all cursor-help ${
                    isActive ? 'scale-110' : ''
                  }`}
                />
              </TooltipTrigger>
              <TooltipContent side="right" sideOffset={8} className="max-w-xs">
                <div className="space-y-1.5">
                  <p className="font-medium">
                    {t.deviceCard.statusTooltip.title}
                    {getCurrentStatusText()}
                  </p>
                  <div className="text-xs opacity-80 space-y-0.5">
                    <p>{t.deviceCard.statusTooltip.legend.green}</p>
                    <p>{t.deviceCard.statusTooltip.legend.yellow}</p>
                    <p>{t.deviceCard.statusTooltip.legend.red}</p>
                    <p>{t.deviceCard.statusTooltip.legend.gray}</p>
                  </div>
                </div>
              </TooltipContent>
            </Tooltip>
          </div>
          {/* Device icon and info */}
          <div className="flex-1 min-w-0 flex flex-col justify-center gap-0.5">
            <div className="flex items-center gap-2">
              <span
                data-testid="device-name"
                className={`min-w-0 flex-1 font-medium text-sm leading-5 line-clamp-2 break-words [overflow-wrap:anywhere] ${
                  isActive
                    ? 'text-slate-900 dark:text-slate-100'
                    : 'text-slate-700 dark:text-slate-300'
                }`}
                title={actualDisplayName}
              >
                {actualDisplayName}
              </span>
            </div>
          </div>

          {/* Right column: Connection type badges */}
          <div className="flex-shrink-0 flex flex-col items-end gap-1">
            {/* Connection type badge */}
            {(() => {
              if (isRemote) {
                return (
                  <Badge
                    variant="outline"
                    className="px-1.5 py-0 text-[10px] border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-400"
                  >
                    <Server className="w-2.5 h-2.5 mr-1" />
                    {isRmq1 ? 'RMQ1 · iPhone' : t.deviceCard.remote || 'Remote'}
                  </Badge>
                );
              } else if (isWifi) {
                return (
                  <Badge
                    variant="outline"
                    className="px-1.5 py-0 text-[10px] border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-400"
                  >
                    <Wifi className="w-2.5 h-2.5 mr-1" />
                    {t.deviceCard.wifi || 'WiFi'}
                  </Badge>
                );
              } else if (isUsb) {
                return (
                  <Badge
                    variant="outline"
                    className="px-1.5 py-0 text-[10px] border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-400"
                  >
                    USB
                  </Badge>
                );
              }
              return null;
            })()}
          </div>
        </div>
        <div className="pointer-events-none mt-1.5 flex items-center gap-2 pl-6">
          <span
            title={serial}
            className={`min-w-0 flex-1 text-[11px] font-mono truncate ${
              isActive
                ? 'text-slate-500 dark:text-slate-400'
                : 'text-slate-400 dark:text-slate-500'
            }`}
          >
            {serial}
          </span>
          {/* Action buttons */}
          <div className="pointer-events-auto relative z-10 flex items-center gap-0.5 flex-shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t.deviceCard.editName}
                  onClick={handleEditClick}
                  className="h-6 w-6 text-slate-400 hover:text-[#1d9bf0]"
                >
                  <Edit className="w-3 h-3" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>{t.deviceCard.editName}</p>
              </TooltipContent>
            </Tooltip>
            {groupAction}
            {onConnectWifi && isUsb && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t.deviceCard.connectViaWifi}
                    onClick={handleWifiClick}
                    disabled={loading}
                    className="h-6 w-6 text-slate-400 hover:text-[#1d9bf0]"
                  >
                    {loading ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Wifi className="w-3.5 h-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  <p>{t.deviceCard.connectViaWifi}</p>
                </TooltipContent>
              </Tooltip>
            )}
            {onDisconnectWifi && isWifi && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t.deviceCard.disconnectWifi}
                    onClick={handleDisconnectClick}
                    disabled={loading}
                    className="h-6 w-6 text-slate-400 hover:text-orange-500"
                  >
                    {loading ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <WifiOff className="w-3.5 h-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  <p>{t.deviceCard.disconnectWifi}</p>
                </TooltipContent>
              </Tooltip>
            )}
            {isRemote && (
              <Button
                variant="ghost"
                size="icon"
                onClick={async e => {
                  e.stopPropagation();
                  setLoading(true);
                  try {
                    if (isRmq1) {
                      await rmq1Request('disconnect', { device_id: serial });
                      onNameUpdated?.();
                    } else {
                      await removeRemoteDevice(serial);
                    }
                    // Refresh will happen via polling
                  } catch (error) {
                    console.error('Failed to remove remote device:', error);
                  } finally {
                    setLoading(false);
                  }
                }}
                disabled={loading}
                className="h-6 w-6 text-slate-400 hover:text-red-500"
                title={t.deviceCard.removeRemote || '移除远程设备'}
              >
                {loading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Trash2 className="w-3.5 h-3.5" />
                )}
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* WiFi Connection Confirmation Dialog */}
      <ConfirmDialog
        isOpen={showWifiConfirm}
        title={t.deviceCard.connectWifiTitle}
        content={t.deviceCard.connectWifiContent}
        onConfirm={handleConfirmWifi}
        onCancel={() => setShowWifiConfirm(false)}
      />

      {/* WiFi Disconnect Confirmation Dialog */}
      <ConfirmDialog
        isOpen={showDisconnectConfirm}
        title={t.deviceCard.disconnectWifiTitle}
        content={t.deviceCard.disconnectWifiContent}
        onConfirm={handleConfirmDisconnect}
        onCancel={() => setShowDisconnectConfirm(false)}
      />

      {/* Device Name Edit Dialog */}
      <Dialog open={showEditDialog} onOpenChange={setShowEditDialog}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t.deviceCard.editNameDialogTitle}</DialogTitle>
            <DialogDescription>
              {t.deviceCard.editNameDialogDescription}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="device-name">
                {t.deviceCard.deviceNameLabel}
              </Label>
              <Input
                id="device-name"
                value={editingName}
                onChange={e => setEditingName(e.target.value)}
                placeholder={t.deviceCard.deviceNamePlaceholder}
                maxLength={100}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !saving) {
                    handleSaveName();
                  }
                }}
              />
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {t.deviceCard.deviceSerialLabel}: {serial}
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowEditDialog(false)}
              disabled={saving}
            >
              {t.common.cancel}
            </Button>
            <Button onClick={handleSaveName} disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t.common.loading}
                </>
              ) : (
                t.common.save
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
