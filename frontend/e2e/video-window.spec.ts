import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/devices', route =>
    route.fulfill({ json: { devices: [] } })
  );
  await page.goto('/about');
});

test('pipelined H.264 frames keep their own geometry and wait for actual rendering', async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const url = '/src/lib/scrcpy-h264-decoder.ts';
    const { ScrcpyH264Decoder } = await import(url);
    type Frame = { timestamp: number; close: () => void };
    let emit: (frame: Frame) => void = () => {
      throw new Error('Decoder not constructed');
    };
    const chunks: { timestamp: number }[] = [];
    Object.defineProperty(window, 'VideoDecoder', {
      value: class {
        state = 'configured';
        constructor(options: { output: (frame: Frame) => void }) {
          emit = options.output;
        }
        configure() {}
        decode(chunk: { timestamp: number }) {
          chunks.push(chunk);
        }
        close() {
          this.state = 'closed';
        }
      },
    });
    Object.defineProperty(window, 'EncodedVideoChunk', {
      value: class {
        timestamp: number;
        constructor(init: { timestamp: number }) {
          this.timestamp = init.timestamp;
        }
      },
    });
    const acks: number[] = [];
    const drawn: number[][] = [];
    const closed: number[] = [];
    const errors: string[] = [];
    let release = () => {};
    const gate = new Promise<void>(resolve => {
      release = resolve;
    });
    const decoder = new ScrcpyH264Decoder(
      async (frame: Frame, epoch: number) => {
        drawn.push([frame.timestamp, epoch]);
        if (frame.timestamp === 1) await gate;
      },
      (error: unknown) => errors.push(String(error))
    );
    for (let i = 1; i <= 4; i++) {
      decoder.write({ type: 'data', data: new Uint8Array([0]), epoch: i }, () =>
        acks.push(i)
      );
    }
    let overflow = '';
    try {
      decoder.write({ type: 'data', data: new Uint8Array([0]), epoch: 5 });
    } catch (error) {
      overflow = String(error);
    }
    for (const chunk of chunks)
      emit({
        timestamp: chunk.timestamp,
        close: () => closed.push(chunk.timestamp),
      });
    await new Promise(resolve => setTimeout(resolve, 0));
    const before = [...acks];
    release();
    await new Promise(resolve => setTimeout(resolve, 0));
    decoder.dispose();
    return { before, acks, drawn, closed, errors, overflow };
  });
  expect(result.before).toEqual([]);
  expect(result.acks).toEqual([1, 2, 3, 4]);
  expect(result.drawn).toEqual([
    [1, 1],
    [2, 2],
    [3, 3],
    [4, 4],
  ]);
  expect(result.closed).toEqual([1, 2, 3, 4]);
  expect(result.errors).toEqual([]);
  expect(result.overflow).toContain('Video render window exceeded');
});

test('disposing a decoder suppresses late render acknowledgements and closes queued frames', async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const url = '/src/lib/scrcpy-h264-decoder.ts';
    const { ScrcpyH264Decoder } = await import(url);
    type Frame = { timestamp: number; close: () => void };
    let emit: (frame: Frame) => void = () => {
      throw new Error('Decoder not constructed');
    };
    Object.defineProperty(window, 'VideoDecoder', {
      value: class {
        state = 'configured';
        constructor(options: { output: (frame: Frame) => void }) {
          emit = options.output;
        }
        configure() {}
        decode() {}
        close() {
          this.state = 'closed';
        }
      },
    });
    Object.defineProperty(window, 'EncodedVideoChunk', { value: class {} });
    let release = () => {};
    const gate = new Promise<void>(resolve => {
      release = resolve;
    });
    let acknowledgements = 0;
    let closed = 0;
    let draws = 0;
    const errors: string[] = [];
    const decoder = new ScrcpyH264Decoder(
      async () => {
        draws++;
        await gate;
      },
      (error: unknown) => errors.push(String(error))
    );
    for (let i = 1; i <= 2; i++) {
      decoder.write(
        { type: 'data', data: new Uint8Array([0]), epoch: 1 },
        () => acknowledgements++
      );
      emit({ timestamp: i, close: () => closed++ });
    }
    await new Promise(resolve => setTimeout(resolve, 0));
    decoder.dispose();
    release();
    await new Promise(resolve => setTimeout(resolve, 0));
    return { acknowledgements, closed, draws, errors };
  });
  expect(result).toEqual({
    acknowledgements: 0,
    closed: 2,
    draws: 1,
    errors: [],
  });
});
