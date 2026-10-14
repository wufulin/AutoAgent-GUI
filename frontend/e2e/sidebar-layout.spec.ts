import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('locale', 'zh');
    localStorage.setItem('theme', 'light');
  });
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [] } })
  );
});

test('removes the global footer and preserves appearance controls in navigation', async ({
  page,
}) => {
  await page.goto('/about');
  // Development tools have a separate zero-height footer unrelated to the app bar.
  await expect(page.locator('footer:not(.TanStackRouterDevtools)')).toHaveCount(
    0
  );
  await page.getByRole('button', { name: '界面设置', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Switch language' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Toggle theme' }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.getByRole('button', { name: 'Switch language' }).click();
  await expect(
    page.getByText('Appearance settings', { exact: true })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(
    page.getByRole('button', { name: 'Toggle theme' })
  ).not.toBeVisible();
});

test('short device names fit narrow lists and utility actions do not select devices', async ({
  page,
}) => {
  await page.goto('/about');
  await page.evaluate(async () => {
    const reactUrl = '/node_modules/.vite/deps/react.js';
    const domUrl = '/node_modules/.vite/deps/react-dom_client.js';
    const componentUrl = '/src/components/GroupedDeviceList.tsx';
    const i18nUrl = '/src/lib/i18n-context.tsx';
    const [reactModule, domModule, { GroupedDeviceList }, { I18nProvider }] =
      await Promise.all([
        import(reactUrl),
        import(domUrl),
        import(componentUrl),
        import(i18nUrl),
      ]);
    const React = reactModule.default || reactModule;
    const { createRoot } = domModule.default || domModule;
    const app = document.querySelector('#app');
    if (app instanceof HTMLElement) app.style.display = 'none';
    const root = document.createElement('div');
    root.id = 'sidebar-fixture';
    root.style.cssText = 'position:fixed;left:20px;top:20px;width:216px';
    document.body.appendChild(root);
    function Fixture() {
      const [selected, setSelected] = React.useState('usb');
      const [selections, setSelections] = React.useState(0);
      const devices = [
        {
          id: 'usb',
          serial: 'usb00001',
          model: 'PGP110',
          status: 'device',
          connection_type: 'usb',
          group_id: 'default',
        },
        {
          id: 'wifi',
          serial: 'wifi-id',
          model: '办公手机',
          status: 'device',
          connection_type: 'wifi',
          group_id: 'default',
        },
        {
          id: 'remote',
          serial: 'remote-id',
          model: 'Remote',
          display_name: '这是一台用于验证长设备名称显示效果的远程手机',
          status: 'device',
          connection_type: 'remote',
          group_id: 'default',
        },
      ];
      return React.createElement(
        I18nProvider,
        null,
        React.createElement(GroupedDeviceList, {
          devices,
          groups: [
            { id: 'default', name: '默认', is_default: true, order: 0 },
            { id: 'other', name: '测试分组', order: 1 },
          ],
          currentDeviceId: selected,
          onSelectDevice: (id: string) => {
            setSelected(id);
            setSelections((n: number) => n + 1);
          },
          onConnectWifi: async () => {},
          onDisconnectWifi: async () => {},
        }),
        React.createElement(
          'output',
          { 'data-testid': 'selection-count' },
          selections
        )
      );
    }
    createRoot(root).render(React.createElement(Fixture));
  });
  const cards = page.getByTestId('device-card');
  await expect(cards).toHaveCount(3);
  for (const name of ['PGP110', '办公手机']) {
    const label = page.getByTestId('device-name').filter({ hasText: name });
    await expect(label).toBeVisible();
    expect(
      await label.evaluate(
        e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight
      )
    ).toBe(true);
  }
  const usb = cards.filter({ hasText: 'PGP110' });
  await expect(usb.getByTestId('device-select')).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  await usb.getByRole('button', { name: '移动到分组', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '测试分组' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('selection-count')).toHaveText('0');
  await usb
    .getByRole('button', { name: '编辑设备名称', exact: true })
    .press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByTestId('selection-count')).toHaveText('0');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await usb.getByTestId('device-select').press('Enter');
  await expect(page.getByTestId('selection-count')).toHaveText('1');
});
