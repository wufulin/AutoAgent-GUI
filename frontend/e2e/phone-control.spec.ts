import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }, info) => {
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [] } })
  );
  if (!info.title.startsWith('renders H.264')) await page.goto('/about');
});

test('serializes gestures and coalesces queued movement before release', async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const url = '/src/lib/phone-control.ts';
    const { PhoneControl } = await import(url);
    const sent: { action: string; x: number; sequence: number }[] = [];
    let release!: () => void;
    const blocked = new Promise<void>(resolve => {
      release = resolve;
    });
    const controller = new PhoneControl(
      'session',
      async (input: { action: string; x: number; sequence: number }) => {
        sent.push(input);
        if (input.action === 'down') await blocked;
        return { status: 'sent' };
      },
      () => {},
      () => {}
    );
    const frame = { x: 1, y: 1, width: 600, height: 1200, epoch: 1 };
    controller.input({ ...frame, action: 'down' });
    controller.input({ ...frame, action: 'move', x: 2 });
    controller.input({ ...frame, action: 'move', x: 3 });
    controller.input({ ...frame, action: 'up', x: 4 });
    controller.input({ ...frame, action: 'move', x: 5 });
    const before = sent.length;
    release();
    await new Promise(resolve => setTimeout(resolve, 20));
    return { before, sent };
  });
  expect(result.before).toBe(1);
  expect(result.sent.map(item => [item.action, item.x, item.sequence])).toEqual(
    [
      ['down', 1, 1],
      ['move', 3, 2],
      ['up', 4, 3],
    ]
  );
});

test('cancel removes queued moves and uses the newly displayed geometry', async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const url = '/src/lib/phone-control.ts';
    const { PhoneControl, mapVideoPoint } = await import(url);
    const sent: { action: string; width: number; epoch: number }[] = [];
    let release!: () => void;
    const wait = new Promise<void>(resolve => {
      release = resolve;
    });
    const controller = new PhoneControl(
      'session',
      async (input: { action: string; width: number; epoch: number }) => {
        sent.push(input);
        if (input.action === 'down') await wait;
        return { status: 'sent' };
      },
      () => {},
      () => {}
    );
    controller.input({
      action: 'down',
      x: 500,
      y: 1000,
      width: 600,
      height: 1200,
      epoch: 1,
    });
    controller.input({
      action: 'move',
      x: 510,
      y: 1100,
      width: 600,
      height: 1200,
      epoch: 1,
    });
    controller.cancel({ width: 1200, height: 600, epoch: 2 });
    release();
    await new Promise(resolve => setTimeout(resolve, 20));
    return {
      sent,
      edge: mapVideoPoint(
        999,
        -10,
        { left: 10, top: 20, width: 300, height: 600 },
        { width: 600, height: 1200, epoch: 1 },
        true
      ),
    };
  });
  expect(
    result.sent.map(item => [item.action, item.width, item.epoch])
  ).toEqual([
    ['down', 600, 1],
    ['cancel', 1200, 2],
  ]);
  expect(result.edge).toEqual({ x: 599, y: 0 });
});

test('a transport timeout is uncertain and never retries or drains pending gestures', async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const url = '/src/lib/phone-control.ts';
    const { PhoneControl } = await import(url);
    const errors: boolean[] = [];
    const sent: string[] = [];
    const controller = new PhoneControl(
      'session',
      async (input: { action: string }) => {
        sent.push(input.action);
        throw new Error('ACK timeout');
      },
      (_message: string, uncertain: boolean) => errors.push(uncertain),
      () => {}
    );
    const frame = { x: 1, y: 1, width: 600, height: 1200, epoch: 1 };
    controller.input({ ...frame, action: 'down' });
    controller.input({ ...frame, action: 'up' });
    await new Promise(resolve => setTimeout(resolve, 20));
    controller.input({ ...frame, action: 'down' });
    return { sent, errors };
  });
  expect(result).toEqual({ sent: ['down'], errors: [true] });
});

test('renders H.264 and releases a pointer dragged outside the canvas', async ({
  page,
}) => {
  let encoded: number[] = [];
  const config: number[] = [];
  const actions: {
    action: string;
    x: number;
    y: number;
    width: number;
    height: number;
  }[] = [];
  await page.routeWebSocket(/\/socket\.io\//, ws => {
    let frameNumber = 0;
    let nextFrame: ReturnType<typeof setTimeout> | undefined;
    ws.onClose(() => clearTimeout(nextFrame));
    ws.send(
      '0' +
        JSON.stringify({
          sid: 'test-engine',
          upgrades: [],
          pingInterval: 25000,
          pingTimeout: 20000,
          maxPayload: 1000000,
        })
    );
    ws.onMessage(message => {
      const raw = String(message);
      if (raw === '40') {
        ws.send('40{"sid":"test-socket"}');
        return;
      }
      if (raw.startsWith('42')) {
        const bracket = raw.indexOf('[');
        const id = raw.slice(2, bracket);
        const [event, payload] = JSON.parse(raw.slice(bracket));
        if (event === 'connect-device') {
          ws.send(
            '42' +
              JSON.stringify([
                'video-metadata',
                { codec: 0x68323634, session: 'test-session' },
              ])
          );
          ws.send(
            '451-1' +
              JSON.stringify([
                'video-data',
                {
                  type: 'configuration',
                  epoch: 1,
                  data: { _placeholder: true, num: 0 },
                },
              ])
          );
          ws.send(Buffer.from(config));
        } else if (event === 'control-input') {
          actions.push(payload);
          ws.send(
            '43' +
              id +
              JSON.stringify([{ status: 'sent', sequence: payload.sequence }])
          );
        }
      }
      if (raw.startsWith('43')) {
        nextFrame = setTimeout(() => {
          frameNumber += 1;
          ws.send(
            `451-${frameNumber + 1}` +
              JSON.stringify([
                'video-data',
                {
                  type: 'data',
                  keyframe: true,
                  pts: frameNumber * 50000,
                  epoch: 1,
                  data: { _placeholder: true, num: 0 },
                },
              ])
          );
          ws.send(Buffer.from(encoded));
        }, 50);
      }
    });
  });
  // Mount the isolated monitor on a route without chat configuration dialogs.
  await page.goto('/about');
  // Generate public, synthetic video pixels. No device screen or ADB is involved.
  encoded = await page.evaluate(async () => {
    const canvas = new OffscreenCanvas(64, 128);
    const context = canvas.getContext('2d');
    if (!context) throw new Error('2D canvas unavailable');
    context.fillStyle = '#348acc';
    context.fillRect(0, 0, 64, 128);
    const chunks: number[][] = [];
    const encoder = new VideoEncoder({
      output: chunk => {
        const data = new Uint8Array(chunk.byteLength);
        chunk.copyTo(data);
        chunks.push([...data]);
      },
      error: error => {
        throw error;
      },
    });
    encoder.configure({
      codec: 'avc1.42001e',
      width: 64,
      height: 128,
      bitrate: 100000,
      framerate: 20,
      avc: { format: 'annexb' },
    });
    const frame = new VideoFrame(canvas, { timestamp: 0 });
    encoder.encode(frame, { keyFrame: true });
    frame.close();
    await encoder.flush();
    encoder.close();
    return chunks[0];
  });
  // Extract SPS/PPS Annex B units as the scrcpy configuration packet.
  const starts: number[] = [];
  for (let i = 0; i < encoded.length - 4; i++) {
    if (
      encoded[i] === 0 &&
      encoded[i + 1] === 0 &&
      encoded[i + 2] === 0 &&
      encoded[i + 3] === 1
    ) {
      starts.push(i);
      i += 3;
    } else if (
      encoded[i] === 0 &&
      encoded[i + 1] === 0 &&
      encoded[i + 2] === 1
    ) {
      starts.push(i);
      i += 2;
    }
  }
  for (let i = 0; i < starts.length; i++) {
    const offset = encoded[starts[i] + 2] === 1 ? 3 : 4;
    const type = encoded[starts[i] + offset] & 31;
    if (type === 7 || type === 8)
      config.push(...encoded.slice(starts[i], starts[i + 1] || encoded.length));
  }
  expect(config.length).toBeGreaterThan(0);
  await page.evaluate(async () => {
    const reactUrl = '/node_modules/.vite/deps/react.js';
    const domUrl = '/node_modules/.vite/deps/react-dom_client.js';
    const componentUrl = '/src/components/DeviceMonitor.tsx';
    const i18nUrl = '/src/lib/i18n-context.tsx';
    const [reactModule, domModule, { DeviceMonitor }, { I18nProvider }] =
      await Promise.all([
        import(reactUrl),
        import(domUrl),
        import(componentUrl),
        import(i18nUrl),
      ]);
    const React = reactModule.default || reactModule;
    const { createRoot } = domModule.default || domModule;
    const appRoot = document.querySelector('#app, #root');
    if (appRoot instanceof HTMLElement) appRoot.style.display = 'none';
    const root = document.createElement('div');
    root.id = 'control-test';
    root.style.cssText =
      'position:fixed;left:100px;top:100px;width:300px;height:600px';
    document.body.appendChild(root);
    createRoot(root).render(
      React.createElement(
        I18nProvider,
        null,
        React.createElement(DeviceMonitor, {
          deviceId: 'synthetic',
          className: 'h-full',
        })
      )
    );
  });
  const canvas = page.locator('#control-test canvas');
  await expect(canvas).toBeVisible();
  await expect(page.locator('#control-test')).toContainText('FPS');
  const header = page.getByTestId('phone-monitor-header');
  const footer = page.getByTestId('phone-monitor-status');
  const settings = page.getByTestId('phone-monitor-settings');
  const settingsButton = header.getByRole('button');
  const assertOutsideScreen = async () => {
    const [top, screen, bottom] = await Promise.all([
      header.boundingBox(),
      canvas.boundingBox(),
      footer.boundingBox(),
    ]);
    if (!top || !screen || !bottom) throw new Error('Missing monitor region');
    expect(top.y + top.height).toBeLessThanOrEqual(screen.y + 1);
    expect(screen.y + screen.height).toBeLessThanOrEqual(bottom.y + 1);
    expect(bottom.y + bottom.height).toBeLessThanOrEqual(701);
    if (await settings.isVisible()) {
      const box = await settings.boundingBox();
      if (!box) throw new Error('Missing settings region');
      expect(box.y + box.height).toBeLessThanOrEqual(screen.y + 1);
    }
  };
  await assertOutsideScreen();
  await settingsButton.press('Enter');
  await expect(settingsButton).toHaveAttribute('aria-expanded', 'true');
  await expect(settings).toBeVisible();
  await assertOutsideScreen();
  expect(actions).toHaveLength(0);
  await settingsButton.click();
  await expect(settings).not.toBeVisible();
  const bounds = await canvas.boundingBox();
  if (!bounds) throw new Error('No rendered canvas');
  // The upper and lower screen edges remain touchable without overlays.
  await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + 40);
  await expect
    .poll(() => actions.filter(action => action.action === 'up').length)
    .toBe(1);
  expect(actions[0].action).toBe('down');
  await page.mouse.move(
    bounds.x + bounds.width / 2,
    bounds.y + bounds.height / 2
  );
  await page.mouse.down();
  await page.mouse.move(
    bounds.x + bounds.width + 40,
    bounds.y + bounds.height + 40,
    { steps: 5 }
  );
  await page.mouse.up();
  await expect
    .poll(() => actions.filter(action => action.action === 'up').length)
    .toBe(2);
  expect(actions[0].action).toBe('down');
  const up = actions.filter(action => action.action === 'up').at(-1);
  expect(up).toMatchObject({ x: 63, y: 127, width: 64, height: 128 });
  // Window blur cancels an active gesture even without a pointer-up event.
  await page.mouse.move(bounds.x + 10, bounds.y + 10);
  await page.mouse.down();
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect
    .poll(() => actions.filter(action => action.action === 'cancel').length)
    .toBe(1);
  await page.mouse.up();
  await page.mouse.click(
    bounds.x + bounds.width / 2,
    bounds.y + bounds.height - 2
  );
  await expect
    .poll(() => actions.filter(action => action.action === 'up').length)
    .toBe(3);
  // A narrow panel and screenshot mode keep controls outside the image.
  await page.evaluate(() => {
    const panel = document.querySelector('[data-testid="phone-monitor"]');
    if (panel instanceof HTMLElement) panel.style.width = '240px';
  });
  await settingsButton.click();
  await assertOutsideScreen();
  await page.route('**/api/screenshot', route =>
    route.fulfill({
      json: {
        success: true,
        image:
          'iVBORw0KGgoAAAANSUhEUgAAAEAAAACACAIAAAA04/g9AAAA4ElEQVR4nNXOQREAIAzAsFJNqEQbohCxB9coyNrnUiZxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEidxEufvwNQDLV8CirCOi1cAAAAASUVORK5CYII=',
        width: 64,
        height: 128,
      },
    })
  );
  await settings.getByRole('button').nth(2).click();
  const screenshot = page.locator('#control-test img');
  await expect(screenshot).toBeVisible();
  await expect
    .poll(() =>
      screenshot.evaluate(image => (image as HTMLImageElement).naturalWidth)
    )
    .toBe(64);
  await expect(footer).not.toContainText('FPS');
  const imageBox = await screenshot.boundingBox();
  const settingsBox = await settings.boundingBox();
  const footerBox = await footer.boundingBox();
  if (!imageBox || !settingsBox || !footerBox)
    throw new Error('Missing screenshot region');
  expect(settingsBox.y + settingsBox.height).toBeLessThanOrEqual(
    imageBox.y + 1
  );
  expect(imageBox.y + imageBox.height).toBeLessThanOrEqual(footerBox.y + 1);
});
