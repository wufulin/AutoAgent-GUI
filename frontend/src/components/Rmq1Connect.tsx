import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { getErrorMessage, rmq1Request } from '../api';

export function Rmq1Connect({ onConnected }: { onConnected: () => void }) {
  const [host, setHost] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [fingerprint, setFingerprint] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const probe = async () => {
    setBusy(true);
    setError('');
    setFingerprint('');
    setConfirmed(false);
    try {
      const result = await rmq1Request<{ fingerprint: string }>('probe', {
        host,
      });
      setFingerprint(result.fingerprint);
    } catch (error) {
      setError(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const connect = async () => {
    setBusy(true);
    setError('');
    try {
      await rmq1Request('connect', { host, username, password, fingerprint });
      setPassword('');
      onConnected();
    } catch (error) {
      setPassword('');
      setError(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        用 RMQ1 的 USB-C 连接支持视频输出的 iPhone。先在 RMQ1
        控制台设置手机型号、方向和混合鼠标（也支持绝对鼠标），并在 iPhone
        启用辅助触控。当前支持画面预览、点击和松开后执行的滑动。
      </p>
      <div className="space-y-2">
        <Label htmlFor="rmq1-host">RMQ1 局域网地址</Label>
        <Input
          id="rmq1-host"
          placeholder="192.168.1.100"
          value={host}
          disabled={busy}
          onChange={event => {
            setHost(event.target.value);
            setFingerprint('');
            setConfirmed(false);
          }}
        />
        <Button
          variant="outline"
          onClick={probe}
          disabled={busy || !host.trim()}
        >
          {busy ? '正在连接…' : '检查连接和证书'}
        </Button>
      </div>
      {fingerprint && (
        <>
          <p className="break-all rounded bg-muted p-2 text-xs">
            SHA-256：{fingerprint.match(/.{2}/g)?.join(':')}
          </p>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={busy}
              onChange={event => setConfirmed(event.target.checked)}
            />
            我已通过可信的 RMQ1 控制台核对证书指纹
          </label>
          <div className="space-y-2">
            <Label htmlFor="rmq1-user">用户名（默认 admin，无需修改）</Label>
            <Input
              id="rmq1-user"
              autoComplete="username"
              value={username}
              disabled={busy}
              onChange={event => setUsername(event.target.value)}
            />
            <Label htmlFor="rmq1-password">密码（仅用于本次连接）</Label>
            <Input
              id="rmq1-password"
              type="password"
              autoComplete="off"
              value={password}
              disabled={busy}
              onChange={event => setPassword(event.target.value)}
            />
          </div>
          <Button
            className="w-full"
            onClick={connect}
            disabled={busy || !confirmed || !username || !password}
          >
            {busy ? '正在连接…' : '连接 iPhone'}
          </Button>
        </>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        后端重启后需重新连接。当前连接方式暂不支持 AI 任务、键盘输入或 Android
        导航键。
      </p>
    </div>
  );
}
