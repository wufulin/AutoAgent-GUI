import { expect, test, type Locator } from '@playwright/test';
import type { Page, WebSocketRoute } from '@playwright/test';

async function bounds(locator: Locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('Expected a visible layout region');
  return box;
}

const device = {
  id: 'rmq1:192.168.1.20',
  serial: 'rmq1:192.168.1.20',
  model: 'iPhone · RMQ1',
  status: 'device',
  connection_type: 'rmq1',
  platform: 'ios',
  state: 'online',
  is_available_only: false,
  display_name: null,
  group_id: 'default',
  agent: null,
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('locale', 'zh');
  });
  // The full suite shares a backend whose earlier tests save model settings.
  // Give this connection-only workflow a deterministic first-run fixture.
  await page.route('**/api/config', route =>
    route.fulfill({
      json: { base_url: '', model_name: '', api_key: '', source: 'fixture' },
    })
  );
});

test('connects only after the certificate is confirmed and clears credentials', async ({
  page,
}) => {
  let added = false;
  let connection: Record<string, unknown> | null = null;
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: added ? [device] : [] } })
  );
  await page.route('**/api/rmq1/probe', route =>
    route.fulfill({ json: { fingerprint: 'a'.repeat(64) } })
  );
  await page.route('**/api/rmq1/connect', route => {
    connection = route.request().postDataJSON();
    added = true;
    return route.fulfill({ json: { success: true, serial: device.serial } });
  });
  await page.goto('/chat');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '添加设备', exact: true }).click();
  await page.getByRole('tab', { name: 'RMQ1 / iPhone' }).click();
  await page.getByLabel('RMQ1 局域网地址').fill('192.168.1.20');
  await page.getByRole('button', { name: '检查连接和证书' }).click();
  await page.getByLabel('密码（仅用于本次连接）').fill('fixture-password');
  const connect = page.getByRole('button', {
    name: '连接 iPhone',
    exact: true,
  });
  await expect(connect).toBeDisabled();
  await page.getByRole('checkbox').check();
  await connect.click();
  await expect
    .poll(() => connection)
    .toMatchObject({
      host: '192.168.1.20',
      fingerprint: 'a'.repeat(64),
      password: 'fixture-password',
    });
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const storage = await page.evaluate(() =>
    JSON.stringify({ ...localStorage, ...sessionStorage })
  );
  expect(storage).not.toContain('fixture-password');
});

interface LiveMessage {
  type: string;
  action_id: string;
  frame_id?: string;
  point?: [number, number];
}

async function mockLive(page: Page) {
  const capture = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 1920;
    canvas.height = 1080;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas unavailable');
    context.fillStyle = '#111';
    context.fillRect(0, 0, 1920, 1080);
    context.fillStyle = '#e2e8f0';
    context.fillRect(690, 0, 540, 1080);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  });
  const fixture = {
    messages: [] as LiveMessage[],
    observations: 0,
    acknowledgements: 0,
    ready: 0,
    unknown: false,
    holdReady: false,
    omitRect: false,
    geometryId: '540:1080:0',
    activeMarker: false,
    holdResult: false,
    holdStarted: false,
    socket: null as WebSocketRoute | null,
    result(message: LiveMessage) {
      this.socket?.send(
        JSON.stringify({
          type: 'result',
          action_id: message.action_id,
          success: !this.unknown,
          outcome: this.unknown ? 'unknown' : 'completed',
          recovery_required: this.unknown,
          ...(this.unknown && { error: '操作结果未知' }),
        })
      );
    },
  };
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [device] } })
  );
  await page.routeWebSocket(/\/api\/rmq1\/preview\?/, ws => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const send = () => {
      if (closed) return;
      fixture.observations++;
      ws.send(
        JSON.stringify({
          type: 'frame',
          image: capture,
          mime: 'image/jpeg',
          width: 1920,
          height: 1080,
          ...(!fixture.omitRect && {
            content_rect: { x: 690 / 1920, y: 0, width: 540 / 1920, height: 1 },
          }),
          session_id: 'a'.repeat(32),
          frame_id: fixture.observations.toString(16).padStart(32, '0'),
          recovery_required: fixture.activeMarker,
          input_active: fixture.activeMarker,
          geometry_id: fixture.geometryId,
        })
      );
    };
    ws.onMessage(raw => {
      const message = JSON.parse(String(raw));
      if (message.type !== 'ack') return;
      fixture.acknowledgements++;
      if (!fixture.holdReady) {
        ws.send(JSON.stringify({ type: 'ready', frame_id: message.frame_id }));
        fixture.ready++;
      }
      timer = setTimeout(send, 50);
    });
    ws.onClose(() => {
      closed = true;
      clearTimeout(timer);
    });
    send();
  });
  await page.routeWebSocket(/\/api\/rmq1\/control\?/, ws => {
    fixture.socket = ws;
    ws.onMessage(raw => {
      const message = JSON.parse(String(raw)) as LiveMessage;
      fixture.messages.push(message);
      if (message.type === 'down' && !fixture.holdStarted)
        ws.send(
          JSON.stringify({ type: 'started', action_id: message.action_id })
        );
      if (
        (message.type === 'up' || message.type === 'cancel') &&
        !fixture.holdResult
      )
        fixture.result(message);
    });
  });
  return fixture;
}

async function openPhone(page: Page) {
  await page.goto(`/chat?serial=${encodeURIComponent(device.serial)}`);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  const image = page.getByRole('img', { name: 'iPhone 实时画面' });
  await expect(image).toBeVisible();
  return page.getByTestId('rmq1-phone-viewport');
}

test('sends down before release and keeps rendering during a live drag', async ({
  page,
}) => {
  const live = await mockLive(page);
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  const box = await bounds(phone);
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.75);
  await page.mouse.down();
  await expect
    .poll(() => live.messages.map(message => message.type))
    .toContain('down');
  expect(live.messages.some(message => message.type === 'up')).toBe(false);
  const count = live.acknowledgements;
  await expect.poll(() => live.acknowledgements).toBeGreaterThan(count + 1);
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.25, {
    steps: 4,
  });
  await page.mouse.up();
  await expect.poll(() => live.messages.at(-1)?.type).toBe('up');
  const down = live.messages[0];
  const up = live.messages.at(-1);
  if (!up?.point || !down.point) throw new Error('Missing pointer input');
  expect(down.frame_id).toBeTruthy();
  expect(up.action_id).toBe(down.action_id);
  expect(down.point[0]).toBeCloseTo((690 + 540 * 0.6) / 1920, 2);
  expect(down.point[1]).toBeCloseTo(0.75, 2);
  expect(up.point[1]).toBeCloseTo(0.25, 2);
  await expect(
    page.getByRole('textbox', { name: 'iPhone 对话任务' })
  ).toBeDisabled();
});

test('waits for render authorization and blocks repeat input after an unknown result', async ({
  page,
}) => {
  const live = await mockLive(page);
  live.holdReady = true;
  const phone = await openPhone(page);
  await expect.poll(() => live.acknowledgements).toBeGreaterThan(1);
  await phone.click();
  expect(live.messages).toHaveLength(0);
  await expect(page.getByRole('alert')).toContainText('画面尚未就绪');
  live.holdReady = false;
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  live.unknown = true;
  await phone.click();
  await expect(page.getByRole('alert')).toContainText('操作结果未知');
  const count = live.messages.length;
  await expect.poll(() => live.acknowledgements).toBeGreaterThan(5);
  await phone.click();
  expect(live.messages).toHaveLength(count);
  let recovered = false;
  await page.route('**/api/rmq1/recover', route => {
    expect(route.request().postDataJSON().confirmed_idle).toBe(true);
    recovered = true;
    live.unknown = false;
    return route.fulfill({ json: { success: true } });
  });
  await page
    .getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
    .click();
  await expect.poll(() => recovered).toBe(true);
  await expect(
    page.getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
  ).not.toBeVisible();
});

test('cancels outside the crop, on resize and on blur without replay', async ({
  page,
}) => {
  const live = await mockLive(page);
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  const box = await bounds(phone);
  await phone.hover();
  await page.mouse.down();
  await expect
    .poll(() => live.messages.filter(message => message.type === 'down').length)
    .toBe(1);
  await page.mouse.move(box.x - 10, box.y + box.height / 2);
  await page.mouse.up();
  await expect.poll(() => live.messages.at(-1)?.type).toBe('cancel');
  const previous = live.ready;
  await expect.poll(() => live.ready).toBeGreaterThan(previous + 1);
  await phone.hover();
  await page.mouse.down();
  await expect
    .poll(() => live.messages.filter(message => message.type === 'down').length)
    .toBe(2);
  await page.setViewportSize({ width: 900, height: 900 });
  await page.mouse.up();
  await expect
    .poll(
      () => live.messages.filter(message => message.type === 'cancel').length
    )
    .toBe(2);
  await expect.poll(() => live.ready).toBeGreaterThan(previous + 3);
  await phone.hover();
  await page.mouse.down();
  await expect
    .poll(() => live.messages.filter(message => message.type === 'down').length)
    .toBe(3);
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await page.mouse.up();
  await expect
    .poll(
      () => live.messages.filter(message => message.type === 'cancel').length
    )
    .toBe(3);
  await expect(
    page.getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
  ).not.toBeVisible();
  expect(live.messages.some(message => message.type === 'up')).toBe(false);
});

test('quarantines a disconnected active gesture and never reconnects or retries it', async ({
  page,
}) => {
  const live = await mockLive(page);
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  await phone.hover();
  await page.mouse.down();
  await expect.poll(() => live.messages.length).toBe(1);
  if (!live.socket) throw new Error('Control socket unavailable');
  live.socket.close();
  await expect(
    page.getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
  ).toBeVisible();
  await page.mouse.up();
  await phone.click();
  expect(live.messages).toHaveLength(1);
});

test('bounds and coalesces wheel input on the cancellable live control channel', async ({
  page,
}) => {
  const live = await mockLive(page);
  live.holdResult = true;
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  await phone.dispatchEvent('wheel', { deltaY: 20, deltaMode: 1 });
  await expect
    .poll(() => live.messages.filter(message => message.type === 'up').length)
    .toBe(1);
  await phone.evaluate(element => {
    for (let i = 0; i < 30; i++)
      element.dispatchEvent(
        new WheelEvent('wheel', {
          deltaY: 4000,
          deltaMode: 2,
          bubbles: true,
          cancelable: true,
        })
      );
  });
  expect(live.messages.filter(message => message.type === 'down')).toHaveLength(
    1
  );
  live.holdResult = false;
  const firstUp = live.messages.find(message => message.type === 'up');
  if (!firstUp) throw new Error('Missing wheel release');
  live.result(firstUp);
  await expect
    .poll(() => live.messages.filter(message => message.type === 'up').length)
    .toBe(2);
  for (const message of live.messages.filter(
    message => message.type === 'down' || message.type === 'up'
  )) {
    if (!message.point) throw new Error('Missing wheel coordinates');
    expect(message.point[0]).toBeCloseTo(0.5, 2);
    expect(message.point[1]).toBeGreaterThanOrEqual(0.325);
    expect(message.point[1]).toBeLessThanOrEqual(0.675);
  }
  const observations = live.observations;
  await expect.poll(() => live.observations).toBeGreaterThan(observations + 4);
  expect(live.messages.filter(message => message.type === 'down')).toHaveLength(
    2
  );
  await phone.dispatchEvent('wheel', { deltaX: 60, deltaY: 1, deltaMode: 0 });
  await expect
    .poll(() => live.messages.filter(message => message.type === 'up').length)
    .toBe(3);
  const horizontal = live.messages
    .filter(message => message.type === 'down' || message.type === 'up')
    .slice(-2);
  expect(horizontal[0].point?.[1]).toBeCloseTo(0.5, 2);
  expect(horizontal[1].point?.[1]).toBeCloseTo(0.5, 2);
  expect(horizontal[0].point?.[0]).toBeGreaterThan(
    horizontal[1].point?.[0] || 0
  );
});

test('cancels a queued quick tap after up and a wheel gesture before started', async ({
  page,
}) => {
  const live = await mockLive(page);
  live.holdResult = true;
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  await phone.click();
  await expect.poll(() => live.messages.at(-1)?.type).toBe('up');
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect.poll(() => live.messages.at(-1)?.type).toBe('cancel');
  const cancelled = live.messages.at(-1);
  if (!cancelled) throw new Error('Missing cancellation');
  live.result(cancelled);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  const ready = live.ready;
  await expect.poll(() => live.ready).toBeGreaterThan(ready + 1);
  live.holdResult = false;
  live.holdStarted = true;
  await phone.dispatchEvent('wheel', { deltaY: 100, deltaMode: 0 });
  await expect
    .poll(() => live.messages.filter(message => message.type === 'down').length)
    .toBe(2);
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect
    .poll(
      () => live.messages.filter(message => message.type === 'cancel').length
    )
    .toBe(2);
  expect(live.messages.filter(message => message.type === 'up')).toHaveLength(
    1
  );
});

test('ignores an active marker and cancels same-size orientation changes', async ({
  page,
}) => {
  const live = await mockLive(page);
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  await phone.hover();
  await page.mouse.down();
  await expect.poll(() => live.messages.at(-1)?.type).toBe('down');
  live.activeMarker = true;
  const ready = live.ready;
  await expect.poll(() => live.ready).toBeGreaterThan(ready + 1);
  await page.mouse.up();
  await expect.poll(() => live.messages.at(-1)?.type).toBe('up');
  live.activeMarker = false;
  await expect(
    page.getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
  ).not.toBeVisible();
  await expect.poll(() => live.ready).toBeGreaterThan(ready + 3);
  await phone.hover();
  await page.mouse.down();
  await expect
    .poll(() => live.messages.filter(message => message.type === 'down').length)
    .toBe(2);
  live.geometryId = '540:1080:180';
  await expect.poll(() => live.messages.at(-1)?.type).toBe('cancel');
  await page.mouse.up();
});

test('coalesces live movement and suppresses callbacks after disposal', async ({
  page,
}) => {
  await page.goto('/about');
  const output = await page.evaluate(async () => {
    const url = '/src/lib/rmq1-control.ts';
    const { Rmq1Control } = await import(url);
    const sent: { type: string; action_id: string; point: number[] }[] = [];
    let callbacks = 0;
    const controller = new Rmq1Control(
      (message: { type: string; action_id: string; point: number[] }) =>
        sent.push(message),
      () => {
        callbacks++;
      }
    );
    controller.begin('session', 'frame', [0.1, 0.1]);
    for (let i = 1; i <= 100; i++) controller.move([i / 100, 0.4]);
    await new Promise(requestAnimationFrame);
    controller.move([0.3, 0.3]);
    controller.end([0.5, 0.5]);
    const action = sent[0].action_id;
    controller.dispose();
    controller.result({
      action_id: action,
      success: true,
      recovery_required: false,
    });
    controller.lost();
    await new Promise(requestAnimationFrame);
    return { sent, callbacks };
  });
  expect(output.sent.map(message => message.type)).toEqual([
    'down',
    'move',
    'up',
    'cancel',
  ]);
  expect(output.sent[1].point).toEqual([1, 0.4]);
  expect(output.sent[2].point).toEqual([0.5, 0.5]);
  expect(output.callbacks).toBe(0);
});

test('fits the phone beside chat and preserves display settings on narrow windows', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const live = await mockLive(page);
  const phone = await openPhone(page);
  await expect.poll(() => live.ready).toBeGreaterThan(1);
  const phoneBox = await bounds(phone);
  const chatBox = await bounds(
    page.getByLabel('iPhone AI 助手', { exact: true })
  );
  expect(chatBox.x + chatBox.width).toBeLessThan(phoneBox.x);
  expect(phoneBox.width / phoneBox.height).toBeCloseTo(0.5, 2);
  await page.screenshot({ path: testInfo.outputPath('rmq1-desktop.png') });
  await page.getByRole('button', { name: '显示设置', exact: true }).click();
  await page.getByLabel('隐藏画面黑边').uncheck();
  await expect
    .poll(async () => {
      const box = await bounds(phone);
      return box.width / box.height;
    })
    .toBeCloseTo(1920 / 1080, 2);
  await page.getByLabel('隐藏画面黑边').check();
  const width = page
    .getByTestId('phone-monitor-settings')
    .getByRole('combobox');
  await width.selectOption('480');
  await expect
    .poll(async () => (await bounds(page.getByTestId('phone-monitor'))).width)
    .toBe(480);
  await width.selectOption('auto');
  await expect
    .poll(async () => {
      const stage = await bounds(page.getByTestId('phone-monitor-stage'));
      const monitor = await bounds(page.getByTestId('phone-monitor'));
      return Math.abs(
        monitor.width - Math.min(640, Math.max(240, stage.height * 0.5))
      );
    })
    .toBeLessThan(1);
  await page.setViewportSize({ width: 900, height: 900 });
  const narrowChat = await bounds(
    page.getByLabel('iPhone AI 助手', { exact: true })
  );
  const monitor = await bounds(page.getByTestId('phone-monitor'));
  expect(narrowChat.y + narrowChat.height).toBeLessThan(monitor.y);
  expect(monitor.x + monitor.width).toBeLessThanOrEqual(900);
  await page.getByTestId('phone-monitor-status').scrollIntoViewIfNeeded();
  await expect(page.getByTestId('phone-monitor-status')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('rmq1-narrow.png') });
  live.omitRect = true;
  await expect
    .poll(async () => {
      const box = await bounds(phone);
      return box.width / box.height;
    })
    .toBeCloseTo(1920 / 1080, 2);
});

for (const mode of ['chatkit', 'classic']) {
  test(`keeps the iPhone chat entry disabled in ${mode} mode`, async ({
    page,
  }) => {
    const taskRequests: string[] = [];
    page.on('request', request => {
      if (
        /\/api\/(task-sessions|tasks|chat|init|chatkit)(\/|\?|$)/.test(
          request.url()
        )
      )
        taskRequests.push(request.url());
    });
    await page.route('**/api/devices', route =>
      route.fulfill({ json: { devices: [device] } })
    );
    await page.routeWebSocket(/\/api\/rmq1\/preview\?/, ws =>
      ws.send(JSON.stringify({ type: 'error', error: '测试：未连接画面' }))
    );
    await page.routeWebSocket(/\/api\/rmq1\/control\?/, () => {});
    await page.goto(
      `/chat?serial=${encodeURIComponent(device.serial)}&mode=${mode}`
    );
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: '通过对话操作 iPhone' })
    ).toBeVisible();
    await expect(
      page.getByRole('textbox', { name: 'iPhone 对话任务' })
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: '发送 iPhone 任务（尚未启用）' })
    ).toBeDisabled();
    await expect(page.getByTestId('phone-monitor-status')).toContainText(
      '画面不可用'
    );
    await page.keyboard.press('Meta+Enter');
    await page.keyboard.press('Control+Enter');
    expect(taskRequests).toEqual([]);
  });
}
