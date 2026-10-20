import { expect, test, type Page } from '@playwright/test';

const image =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1kAAAAASUVORK5CYII=';
const success = {
  success: true,
  image,
  width: 1,
  height: 1,
  is_sensitive: false,
};
const failure = {
  ...success,
  success: false,
  image: '',
  error: 'Device usb-one not found or offline',
};

async function mountMonitor(page: Page) {
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [] } })
  );
  await page.routeWebSocket(/\/socket\.io\//, () => {});
  await page.goto('/about');
  await page.evaluate(async () => {
    const reactUrl = '/node_modules/.vite/deps/react.js';
    const domUrl = '/node_modules/.vite/deps/react-dom_client.js';
    const monitorUrl = '/src/components/DeviceMonitor.tsx';
    const i18nUrl = '/src/lib/i18n-context.tsx';
    const [reactModule, domModule, { DeviceMonitor }, { I18nProvider }] =
      await Promise.all([
        import(reactUrl),
        import(domUrl),
        import(monitorUrl),
        import(i18nUrl),
      ]);
    const React = reactModule.default || reactModule;
    const { createRoot } = domModule.default || domModule;
    const app = document.querySelector('#app, #root');
    if (app instanceof HTMLElement) app.style.display = 'none';
    const root = document.createElement('div');
    root.style.cssText = 'position:fixed;inset:20px;display:flex';
    document.body.appendChild(root);
    function Fixture() {
      const [deviceId, setDeviceId] = React.useState('usb-one');
      return React.createElement(
        I18nProvider,
        null,
        React.createElement(
          'button',
          { onClick: () => setDeviceId('usb-two') },
          'Switch device'
        ),
        React.createElement(DeviceMonitor, { deviceId, connectionType: 'usb' })
      );
    }
    createRoot(root).render(
      React.createElement(React.StrictMode, null, React.createElement(Fixture))
    );
  });
  await page.getByRole('button', { name: 'Display settings' }).click();
  await page.getByRole('button', { name: 'Image', exact: true }).click();
}

for (const kind of ['offline', 'http', 'network', 'empty-error']) {
  test(`shows ${kind} failure and automatically recovers the USB preview`, async ({
    page,
  }) => {
    let recovered = false;
    await page.route('**/api/screenshot', route => {
      if (recovered) return route.fulfill({ json: success });
      if (kind === 'network') return route.abort('failed');
      if (kind === 'http')
        return route.fulfill({
          status: 503,
          json: { detail: 'Screenshot service unavailable' },
        });
      return route.fulfill({
        json: kind === 'empty-error' ? { ...failure, error: '' } : failure,
      });
    });
    await mountMonitor(page);
    const monitor = page.getByTestId('phone-monitor');
    const message =
      kind === 'offline'
        ? failure.error
        : kind === 'http'
          ? 'Screenshot service unavailable'
          : kind === 'empty-error'
            ? 'Screenshot capture failed'
            : 'Failed to fetch';
    await expect(monitor.getByRole('alert')).toContainText(message);
    await expect(
      monitor.getByTestId('phone-monitor-status').locator('svg')
    ).toHaveClass(/text-destructive/);
    await expect(monitor.getByText('Loading...')).toHaveCount(0);
    recovered = true;
    await expect(monitor.getByAltText('Device Screenshot')).toBeVisible();
    await expect(monitor.getByRole('alert')).toHaveCount(0);
    await expect(
      monitor.getByTestId('phone-monitor-status').locator('svg')
    ).toHaveClass(/text-emerald-500/);
  });
}

test('times out a hung screenshot request and retries successfully', async ({
  page,
}) => {
  let recovered = false;
  let started = false;
  await page.route('**/api/screenshot', route => {
    started = true;
    if (recovered) return route.fulfill({ json: success });
    // Leave the response pending to exercise fetch cancellation.
  });
  await page.clock.install();
  await mountMonitor(page);
  await expect.poll(() => started).toBe(true);
  await page.clock.runFor(15000);
  await expect(page.getByRole('alert')).toContainText(
    'Screenshot request timed out'
  );
  recovered = true;
  await page.clock.runFor(751);
  await expect(page.getByAltText('Device Screenshot')).toBeVisible();
});

test('switching devices cancels a pending request and hides the old frame', async ({
  page,
}) => {
  let oldRequestPending = false;
  let releaseOld: (() => Promise<void>) | undefined;
  let releaseNew: (() => Promise<void>) | undefined;
  await page.route('**/api/screenshot', route => {
    const id = route.request().postDataJSON().device_id;
    if (id === 'usb-two') {
      releaseNew = () =>
        route.fulfill({ json: { ...failure, error: 'New device offline' } });
    } else if (!oldRequestPending) {
      oldRequestPending = true;
      return route.fulfill({ json: success });
    } else {
      releaseOld = () => route.fulfill({ json: success });
    }
  });
  await mountMonitor(page);
  await expect(page.getByAltText('Device Screenshot')).toBeVisible();
  await expect.poll(() => !!releaseOld).toBe(true);
  await page.getByRole('button', { name: 'Switch device' }).click();
  await expect(page.getByAltText('Device Screenshot')).toHaveCount(0);
  await expect.poll(() => !!releaseNew).toBe(true);
  if (!releaseNew || !releaseOld)
    throw new Error('Expected both pending requests');
  await releaseNew();
  await expect(page.getByRole('alert')).toContainText('New device offline');
  await releaseOld();
  await expect(page.getByAltText('Device Screenshot')).toHaveCount(0);
  await expect(page.getByRole('alert')).toContainText('New device offline');
});

test('automatic video fallback shows the USB screenshot error', async ({
  page,
}) => {
  await page.clock.install();
  await page.route('**/api/screenshot', route =>
    route.fulfill({ json: failure })
  );
  await mountMonitor(page);
  await page.getByRole('button', { name: 'Auto', exact: true }).click();
  await page.clock.runFor(20001);
  const monitor = page.getByTestId('phone-monitor');
  await expect(monitor.getByRole('alert')).toContainText(failure.error);
  await expect(monitor.getByTestId('phone-monitor-status')).toContainText(
    'Auto · Image'
  );
});
