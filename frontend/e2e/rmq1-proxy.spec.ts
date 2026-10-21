import { expect, test } from '@playwright/test';

for (const endpoint of ['preview', 'control']) {
  test(`proxies the native RMQ1 ${endpoint} WebSocket to the backend`, async ({
    page,
  }) => {
    // Use a same-origin document without mounting the device UI. This exercises
    // the real Vite proxy and ASGI wrapper without connecting or controlling a phone.
    const response = await page.goto('/api/health');
    expect(response?.ok()).toBe(true);

    const result = await page.evaluate(async endpoint => {
      const url = new URL(`/api/rmq1/${endpoint}`, window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      // A unique lookup-only ID cannot select an existing device, even when a
      // developer reuses a running backend. No input messages are sent.
      url.searchParams.set(
        'device_id',
        `rmq1:proxy-regression-${crypto.randomUUID()}`
      );

      return new Promise<{
        events: string[];
        messages: string[];
        closeCode: number;
      }>((resolve, reject) => {
        const socket = new WebSocket(url);
        const events: string[] = [];
        const messages: string[] = [];
        const timeout = window.setTimeout(() => {
          socket.close();
          reject(
            new Error('RMQ1 proxy did not complete the WebSocket exchange')
          );
        }, 10000);

        socket.onopen = () => events.push('open');
        socket.onmessage = event => {
          events.push('message');
          messages.push(String(event.data));
        };
        socket.onerror = () => events.push('error');
        socket.onclose = event => {
          window.clearTimeout(timeout);
          events.push('close');
          resolve({ events, messages, closeCode: event.code });
        };
      });
    }, endpoint);

    // A proxy failure or Origin rejection never opens the WebSocket. Requiring
    // the exact application message also proves the request reached device lookup.
    expect(result.events).toEqual(['open', 'message', 'close']);
    expect(result.messages).toHaveLength(1);
    expect(JSON.parse(result.messages[0])).toEqual({
      type: 'error',
      error: 'RMQ1 未连接',
    });
    expect(result.closeCode).toBe(1008);
  });
}
