import { expect, test, type Locator } from '@playwright/test';

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

test('uses frame-bound gestures and blocks repeat input after an unknown result', async ({
  page,
}) => {
  const gestures: Record<string, unknown>[] = [];
  let frame = 0;
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [device] } })
  );
  await page.route('**/api/rmq1/observe', route =>
    route.fulfill({
      json: {
        success: true,
        image:
          'iVBORw0KGgoAAAANSUhEUgAAAMgAAAGQCAIAAABkkLjnAAAEDElEQVR4nO3UMQ3AMBAEQTv8kaUPASMJCW/x0gyAq1a33+8suO25vgjCouKxSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICAthMYfHIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLITFHB6LhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwSAiLhLBICIuEsEgIi4SwEBZzeCwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsIiISwSwiIhLBLCIiEsEsJiFX4iBwWhVW/83AAAAABJRU5ErkJggg==',
        width: 200,
        height: 400,
        session_id: 'a'.repeat(32),
        frame_id: (++frame).toString(16).padStart(32, '0'),
        recovery_required: false,
      },
    })
  );
  await page.route('**/api/rmq1/gesture', route => {
    gestures.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        success: false,
        recovery_required: true,
        error: '操作结果未知',
      },
    });
  });
  let recovered = false;
  await page.route('**/api/rmq1/recover', route => {
    expect(route.request().postDataJSON().confirmed_idle).toBe(true);
    recovered = true;
    return route.fulfill({ json: { success: true } });
  });
  await page.goto(`/chat?serial=${encodeURIComponent(device.serial)}`);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  const image = page.getByRole('img', { name: 'iPhone 实时画面' });
  await expect(image).toBeVisible();
  await expect(
    page.getByRole('textbox', { name: 'iPhone 对话任务' })
  ).toBeDisabled();
  await image.click();
  await expect.poll(() => gestures.length).toBe(1);
  expect(gestures[0]).toMatchObject({
    device_id: device.id,
    session_id: 'a'.repeat(32),
    end: null,
  });
  expect(gestures[0].frame_id).toBeTruthy();
  expect(gestures[0].action_id).toBeTruthy();
  await expect(page.getByRole('alert')).toContainText('操作结果未知');
  // Polling fresh pictures must not clear a locally uncertain operation.
  await expect.poll(() => frame).toBeGreaterThan(2);
  await image.click();
  expect(gestures).toHaveLength(1);
  await page
    .getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
    .click();
  await expect.poll(() => recovered).toBe(true);
  await expect(
    page.getByRole('button', { name: '我已核对手机状态、释放按键，恢复控制' })
  ).not.toBeVisible();
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
    await page.route('**/api/rmq1/observe', route =>
      route.fulfill({ status: 409, json: { detail: '测试：未连接画面' } })
    );
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
    await expect(page.getByRole('alert')).toContainText('测试：未连接画面');
    await page.keyboard.press('Meta+Enter');
    await page.keyboard.press('Control+Enter');
    await expect(page.getByTestId('phone-monitor-status')).toContainText(
      '画面不可用'
    );
    expect(taskRequests).toEqual([]);
  });
}

test('fits the phone beside chat, preserves cropped gesture coordinates and adapts to narrow windows', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const capture = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 1920;
    canvas.height = 1080;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas context unavailable');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, 1920, 1080);
    ctx.fillStyle = '#e2e8f0';
    ctx.fillRect(690, 0, 540, 1080);
    ctx.fillStyle = '#0f172a';
    ctx.font = '32px sans-serif';
    ctx.fillText('iPhone · 预览测试', 735, 100);
    ctx.font = '24px sans-serif';
    ctx.fillText('模拟画面，不连接真机', 735, 160);
    for (let i = 0; i < 5; i++) {
      ctx.fillStyle = '#fff';
      ctx.fillRect(720, 240 + i * 120, 480, 80);
      ctx.fillStyle = '#334155';
      ctx.fillText(`测试项目 ${i + 1}`, 750, 290 + i * 120);
    }
    return canvas.toDataURL('image/png').split(',')[1];
  });
  let observations = 0;
  let omitRect = false;
  const gestures: { start: number[]; end: number[] | null }[] = [];
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [device] } })
  );
  await page.route('**/api/rmq1/observe', route =>
    route.fulfill({
      json: {
        success: true,
        image: capture,
        width: 1920,
        height: 1080,
        ...(!omitRect && {
          content_rect: { x: 690 / 1920, y: 0, width: 540 / 1920, height: 1 },
        }),
        session_id: 'b'.repeat(32),
        frame_id: (++observations).toString(16).padStart(32, '0'),
        recovery_required: false,
      },
    })
  );
  await page.route('**/api/rmq1/gesture', route => {
    gestures.push(route.request().postDataJSON());
    return route.fulfill({ json: { success: true, recovery_required: false } });
  });
  await page.goto(`/chat?serial=${encodeURIComponent(device.serial)}`);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  const phone = page.getByTestId('rmq1-phone-viewport');
  const image = page.getByRole('img', { name: 'iPhone 实时画面' });
  await expect
    .poll(() => image.evaluate(el => (el as HTMLImageElement).naturalWidth))
    .toBe(1920);
  await expect(phone).toBeVisible();
  const phoneBox = await bounds(phone);
  const chatBox = await bounds(
    page.getByLabel('iPhone AI 助手', { exact: true })
  );
  expect(chatBox.x + chatBox.width).toBeLessThan(phoneBox.x);
  expect(phoneBox.width / phoneBox.height).toBeCloseTo(0.5, 2);
  expect(phoneBox.width).toBeGreaterThan(250);
  await page.screenshot({ path: testInfo.outputPath('rmq1-desktop.png') });

  const nextObservation = async () => {
    const previous = observations;
    await expect.poll(() => observations).toBeGreaterThan(previous);
  };
  await phone.click({
    position: { x: phoneBox.width * 0.2, y: phoneBox.height * 0.3 },
  });
  await expect.poll(() => gestures.length).toBe(1);
  expect(gestures[0].start[0]).toBeCloseTo((690 + 540 * 0.2) / 1920, 2);
  expect(gestures[0].start[1]).toBeCloseTo(0.3, 2);
  expect(gestures[0].end).toBeNull();
  // A new frame ID can have identical pixels after an operation.
  await nextObservation();
  await page.mouse.move(
    phoneBox.x + phoneBox.width * 0.6,
    phoneBox.y + phoneBox.height * 0.75
  );
  await page.mouse.down();
  await page.mouse.move(
    phoneBox.x + phoneBox.width * 0.6,
    phoneBox.y + phoneBox.height * 0.25,
    { steps: 4 }
  );
  await page.mouse.up();
  await expect.poll(() => gestures.length).toBe(2);
  const swipeEnd = gestures[1].end;
  if (!swipeEnd) throw new Error('Expected a swipe endpoint');
  expect(swipeEnd[0]).toBeCloseTo((690 + 540 * 0.6) / 1920, 2);
  expect(swipeEnd[1]).toBeCloseTo(0.25, 2);

  await nextObservation();
  await page.mouse.move(
    phoneBox.x + phoneBox.width / 2,
    phoneBox.y + phoneBox.height / 2
  );
  await page.mouse.down();
  await page.mouse.move(phoneBox.x - 20, phoneBox.y + phoneBox.height / 2);
  await page.mouse.up();
  await nextObservation();
  expect(gestures).toHaveLength(2); // Captured pointer ended in the clipped black bar.

  await page.getByRole('button', { name: '显示设置', exact: true }).click();
  const settingsBox = await bounds(page.getByTestId('phone-monitor-settings'));
  const resizedPhone = await bounds(phone);
  expect(settingsBox.y + settingsBox.height).toBeLessThanOrEqual(
    resizedPhone.y + 1
  );
  await page.getByLabel('隐藏画面黑边').uncheck();
  await expect
    .poll(async () => {
      const box = await bounds(phone);
      return box.width / box.height;
    })
    .toBeCloseTo(1920 / 1080, 2);
  await page.getByLabel('隐藏画面黑边').check();
  const widthSelect = page
    .getByTestId('phone-monitor-settings')
    .getByRole('combobox');
  await widthSelect.selectOption('480');
  await expect
    .poll(async () => (await bounds(page.getByTestId('phone-monitor'))).width)
    .toBe(480);
  await page.setViewportSize({ width: 1200, height: 1000 });
  await expect
    .poll(
      async () =>
        (await bounds(page.getByLabel('iPhone AI 助手', { exact: true }))).width
    )
    .toBeGreaterThanOrEqual(310);
  await expect
    .poll(async () => (await bounds(page.getByTestId('phone-monitor'))).width)
    .toBeLessThan(480);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await widthSelect.selectOption('auto');
  await expect
    .poll(async () => {
      const stageBox = await bounds(page.getByTestId('phone-monitor-stage'));
      const monitorBox = await bounds(page.getByTestId('phone-monitor'));
      return Math.abs(
        monitorBox.width - Math.min(640, Math.max(240, stageBox.height * 0.5))
      );
    })
    .toBeLessThan(1);
  await widthSelect.selectOption('320');
  await page.getByRole('button', { name: '显示设置', exact: true }).click();

  await nextObservation();
  await phone.hover();
  await page.mouse.down();
  await page.setViewportSize({ width: 900, height: 900 });
  await expect
    .poll(async () => (await bounds(phone)).width)
    .not.toBeCloseTo(phoneBox.width, 0);
  await page.mouse.up();
  await nextObservation();
  expect(gestures).toHaveLength(2); // Resizing cancels the unsubmitted gesture.
  const narrowChat = await bounds(
    page.getByLabel('iPhone AI 助手', { exact: true })
  );
  const narrowMonitor = await bounds(page.getByTestId('phone-monitor'));
  expect(narrowChat.y + narrowChat.height).toBeLessThan(narrowMonitor.y);
  expect(narrowMonitor.x + narrowMonitor.width).toBeLessThanOrEqual(900);
  await page.getByTestId('phone-monitor-status').scrollIntoViewIfNeeded();
  await expect(page.getByTestId('phone-monitor-status')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('rmq1-narrow.png') });

  omitRect = true;
  await expect
    .poll(async () => {
      const box = await bounds(phone);
      return box.width / box.height;
    })
    .toBeCloseTo(1920 / 1080, 2);
});
