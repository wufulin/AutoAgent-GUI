import { expect, test } from '@playwright/test';

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
  await expect(page.locator('textarea')).toHaveCount(0);
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
