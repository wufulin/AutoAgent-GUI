import { expect, test } from '@playwright/test';
import type { Device } from '../src/api';

test('refresh discovers a USB device without wireless pairing', async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('locale', 'zh');
    localStorage.setItem('theme', 'light');
  });

  let devices: Device[] = [];
  let wirelessRequests = 0;
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices } })
  );
  await page.route('**/api/device-groups', route =>
    route.fulfill({
      json: {
        groups: [{ id: 'default', name: '默认', is_default: true, order: 0 }],
      },
    })
  );
  await page.route('**/api/devices/discover_mdns', route => {
    wirelessRequests += 1;
    return route.fulfill({ json: { success: true, devices: [] } });
  });

  await page.goto('/chat');
  await expect(page.getByText('未连接设备', { exact: true })).toBeVisible();
  // Freeze automatic polling so only the refresh button can discover the phone.
  await page.clock.pauseAt(new Date());
  devices = [
    {
      id: 'usb-test-phone',
      serial: 'usb-test-phone',
      model: 'PGP110',
      status: 'device',
      connection_type: 'usb',
      state: 'online',
      is_available_only: false,
      display_name: null,
      group_id: 'default',
      agent: null,
    },
    {
      id: 'usb-disconnected',
      serial: 'usb-disconnected',
      model: 'Old phone',
      status: 'device',
      connection_type: 'usb',
      state: 'disconnected',
      is_available_only: false,
      display_name: null,
      group_id: 'default',
      agent: null,
    },
  ];

  await page.getByRole('button', { name: '刷新设备', exact: true }).click();
  const card = page.getByTestId('device-card');
  await expect(card).toHaveCount(1);
  await expect(card.getByTestId('device-name')).toHaveText('PGP110');
  await expect(card.getByText('USB', { exact: true })).toBeVisible();
  await expect(card.getByTestId('device-select')).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  expect(wirelessRequests).toBe(0);

  await page.getByRole('button', { name: '添加设备', exact: true }).click();
  await expect(
    page.getByText('USB 连接（自动识别）', { exact: true })
  ).toBeVisible();
  await expect(page.getByRole('tab', { name: '无线连接' })).toBeVisible();
});
