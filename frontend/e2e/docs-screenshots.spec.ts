/**
 * Documentation screenshot capture.
 *
 * Reuses the E2E mock-service infra (globalSetup) to launch the app with a
 * mock device + mock model, then captures screenshots into
 * docs/static/img/screenshots/ for the Diátaxis docs.
 *
 * Run only this file (backend on a free port to avoid clashing with other apps):
 *   ADB_SERVER_SOCKET=tcp:127.0.0.1:1 \
 *   AUTOGLM_E2E_BACKEND_PORT=8077 VITE_PROXY_TARGET=http://localhost:8077 \
 *     pnpm exec playwright test docs-screenshots.spec.ts
 *
 * A manifest of which shots succeeded/failed is written to
 * docs/static/img/screenshots/_manifest.json
 */
import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// CI/local verification can preserve generated artifacts outside tracked docs.
const OUT_DIR = process.env.AUTOGLM_E2E_SCREENSHOTS_DIR
  ? path.resolve(process.env.AUTOGLM_E2E_SCREENSHOTS_DIR)
  : path.resolve(__dirname, '../../docs/static/img/screenshots');

type ServiceUrls = {
  llm_url: string;
  agent_url: string;
  backend_url: string;
};

type TaskRun = { id: string; status: string };
const terminalStatuses = new Set([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'INTERRUPTED',
]);
const submittedTaskIds: string[] = [];

function readServiceUrls(): ServiceUrls {
  const urlsPath = path.resolve(__dirname, '.service_urls.json');
  if (!fs.existsSync(urlsPath)) {
    throw new Error(
      '.service_urls.json not found - is start_e2e_services.py running?'
    );
  }
  return JSON.parse(fs.readFileSync(urlsPath, 'utf-8')) as ServiceUrls;
}

async function setupMockDeviceAndConfig(
  request: APIRequestContext,
  page: Page
): Promise<string> {
  const { backend_url, agent_url, llm_url } = readServiceUrls();
  // Use a unique device ID so this spec does not collide with other E2E
  // tests (e.g. scroll.spec.ts) that share the same backend process.
  const deviceId = `mock_device_docs_${Date.now()}`;

  let serial = deviceId;
  try {
    const addResp = await request.post(
      `${backend_url}/api/devices/add_remote`,
      {
        data: { base_url: agent_url, device_id: deviceId },
      }
    );
    const addData = (await addResp.json()) as {
      success: boolean;
      serial: string | null;
    };
    if (addData?.serial) serial = addData.serial;
  } catch {
    /* device may already exist */
  }

  expect((await request.delete(`${backend_url}/api/config`)).ok()).toBe(true);
  const config = await request.post(`${backend_url}/api/config`, {
    data: {
      base_url: `${llm_url}/v1`,
      model_name: 'mock-glm-model',
      api_key: 'mock-key',
      agent_type: 'glm-async',
      decision_base_url: `${llm_url}/v1`,
      decision_model_name: 'mock-planner',
      decision_api_key: 'mock-key',
    },
  });
  expect(config.ok(), await config.text()).toBe(true);
  // The documentation scenario owns one remote mock device. Do not let the
  // home page auto-select another device connected to the test workstation.
  await page.route('**/api/devices', async route => {
    const response = await route.fetch();
    const body = (await response.json()) as {
      devices: { serial: string }[];
    };
    await route.fulfill({
      response,
      json: {
        ...body,
        devices: body.devices.filter(device => device.serial === serial),
      },
    });
  });
  return serial;
}

async function readTask(
  request: APIRequestContext,
  backendUrl: string,
  taskId: string
): Promise<TaskRun> {
  const response = await request.get(`${backendUrl}/api/tasks/${taskId}`);
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as TaskRun;
}

const results: { name: string; ok: boolean; error?: string }[] = [];

async function shot(
  page: Page,
  name: string,
  fn: () => Promise<void>
): Promise<void> {
  try {
    await fn();
    await page.waitForTimeout(400);
    await page.screenshot({
      path: path.join(OUT_DIR, `${name}.png`),
      fullPage: false,
    });
    results.push({ name, ok: true });
    console.log(`[shot] OK   ${name}`);
  } catch (e) {
    results.push({ name, ok: false, error: String(e).split('\n')[0] });
    console.log(`[shot] FAIL ${name}: ${String(e).split('\n')[0]}`);
    // Best-effort screenshot of whatever is on screen, for debugging
    try {
      await page.screenshot({
        path: path.join(OUT_DIR, `${name}.png`),
        fullPage: false,
      });
    } catch {
      /* ignore */
    }
  }
}

/** Screenshot the open dialog if present, else the full viewport. */
async function shotDialog(
  page: Page,
  name: string,
  open: () => Promise<void>
): Promise<void> {
  await shot(page, name, async () => {
    await open();
    const dialog = page.locator('[role="dialog"]').first();
    await dialog.waitFor({ state: 'visible', timeout: 8000 });
    await page.waitForTimeout(500);
  });
}

async function closeDialog(page: Page): Promise<void> {
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(300);
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(300);
}

test.describe('Docs screenshots', () => {
  test.beforeAll(() => {
    fs.mkdirSync(OUT_DIR, { recursive: true });
  });

  test.beforeEach(async ({ page }) => {
    submittedTaskIds.length = 0;
    await page.addInitScript(() => {
      localStorage.setItem('locale', 'zh');
    });
    page.setDefaultTimeout(8000);
  });

  test.afterEach(async ({ request }) => {
    const { backend_url } = readServiceUrls();
    // A failed screenshot must not leave this test's task holding control and
    // block the following spec from changing its model configuration.
    for (const taskId of submittedTaskIds) {
      const task = await readTask(request, backend_url, taskId);
      if (!terminalStatuses.has(task.status)) {
        const cancelled = await request.post(
          `${backend_url}/api/tasks/${taskId}/cancel`
        );
        expect(cancelled.ok(), await cancelled.text()).toBe(true);
        await expect
          .poll(
            async () =>
              terminalStatuses.has(
                (await readTask(request, backend_url, taskId)).status
              ),
            { timeout: 15000 }
          )
          .toBe(true);
      }
    }
  });

  test('capture all pages and dialogs', async ({ page, request }) => {
    test.setTimeout(300000);
    results.length = 0; // reset shared state so failures don't cascade
    const serial = await setupMockDeviceAndConfig(request, page);
    const chatUrl = `/chat?serial=${encodeURIComponent(serial)}&mode=classic`;

    // ---- Top-level pages ----
    await shot(page, 'home', async () => {
      await page.goto('/');
      await page.waitForTimeout(1500);
    });

    await shot(page, 'chat-ready', async () => {
      await page.goto(chatUrl);
      await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
      await page.waitForTimeout(1200);
    });

    await shot(page, 'chat-layered', async () => {
      await page.goto(
        `/chat?serial=${encodeURIComponent(serial)}&mode=chatkit`
      );
      await page.waitForTimeout(1500);
    });

    await shot(page, 'skills-empty', async () => {
      await page.goto('/skills');
      await page.waitForTimeout(1200);
    });

    await shot(page, 'history-empty', async () => {
      await page.goto('/history');
      await page.waitForTimeout(1200);
    });

    await shot(page, 'scheduled-empty', async () => {
      await page.goto('/scheduled-tasks');
      await page.waitForTimeout(1200);
    });

    await shot(page, 'terminal', async () => {
      await page.goto('/terminal');
      await page.waitForTimeout(1500);
    });

    await shot(page, 'logs', async () => {
      await page.goto('/logs');
      await page.waitForTimeout(1200);
    });

    await shot(page, 'about', async () => {
      await page.goto('/about');
      await page.waitForTimeout(1000);
    });

    // ---- Chat-page dialogs ----
    await page.goto(chatUrl);
    await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(1000);

    await shotDialog(page, 'settings-vision', async () => {
      await page.getByRole('button', { name: '设置', exact: true }).click();
    });
    // Decision tab inside settings dialog
    await shot(page, 'settings-decision', async () => {
      await page.getByRole('tab', { name: /决策模型/ }).click();
      await page.waitForTimeout(600);
    });
    await closeDialog(page);

    await shotDialog(page, 'device-add', async () => {
      await page.getByRole('button', { name: '添加设备' }).click();
    });
    // Tabs within the add-device dialog. QR pairing renders inside the
    // "配对设备" tab, so it is captured separately below.
    for (const [tabName, file] of [
      ['配对设备', 'device-add-pair'],
      ['远程设备', 'device-add-remote'],
      ['无线连接', 'device-add-direct'],
    ] as const) {
      await shot(page, file, async () => {
        await page.getByRole('tab', { name: new RegExp(tabName) }).click();
        await page.waitForTimeout(700);
      });
    }

    // QR pairing UI auto-generates inside the "配对设备" tab.
    await shot(page, 'device-add-qr', async () => {
      await page.getByRole('tab', { name: /配对设备/ }).click();
      await page
        .locator('[role="dialog"] svg')
        .first()
        .waitFor({ state: 'visible', timeout: 8000 });
      await page.waitForTimeout(600);
    });
    await closeDialog(page);

    await shotDialog(page, 'device-groups', async () => {
      await page.getByRole('button', { name: '管理分组' }).click();
    });
    await closeDialog(page);

    // Skill quick-run popover on chat input
    await shot(page, 'chat-input', async () => {
      await page.goto(chatUrl);
      await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
      await page
        .locator('textarea')
        .fill('去美团点一杯霸王茶姬的伯牙绝弦，去冰加珍珠');
      await page.waitForTimeout(500);
    });

    // ---- Skills dialog ----
    await page.goto('/skills');
    await page.waitForTimeout(1000);
    await shotDialog(page, 'skill-create', async () => {
      await page
        .getByRole('button', { name: /新建 Skill|新建/ })
        .first()
        .click();
    });
    await closeDialog(page);

    // ---- Scheduled task dialog ----
    await page.goto('/scheduled-tasks');
    await page.waitForTimeout(1000);
    await shotDialog(page, 'scheduled-create', async () => {
      await page
        .getByRole('button', { name: /新建任务|新建/ })
        .first()
        .click();
    });
    await closeDialog(page);

    // ---- Dark theme variant of chat ----
    await shot(page, 'chat-dark', async () => {
      await page.goto(chatUrl);
      await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
      // Toggle theme via the footer toggle (icon button, no text) — best effort
      await page.evaluate(() => {
        localStorage.setItem('theme', 'dark');
        document.documentElement.classList.add('dark');
      });
      await page.waitForTimeout(800);
    });

    // Write manifest
    fs.writeFileSync(
      path.join(OUT_DIR, '_manifest.json'),
      JSON.stringify(
        { generatedAt: new Date().toISOString(), results },
        null,
        2
      )
    );
    const failed = results.filter(r => !r.ok);
    console.log(
      `\n[docs-screenshots] ${results.length} shots, ${failed.length} failed`
    );
    for (const f of failed) console.log(`  FAILED: ${f.name} — ${f.error}`);
    expect(
      failed,
      `Expected all screenshots to succeed, but ${failed.length} failed`
    ).toHaveLength(0);
  });

  test('capture populated states (seeded data + task run)', async ({
    page,
    request,
  }) => {
    test.setTimeout(300000);
    results.length = 0; // reset shared state so failures don't cascade
    const { backend_url, agent_url, llm_url } = readServiceUrls();
    const serial = await setupMockDeviceAndConfig(request, page);
    const chatUrl = `/chat?serial=${encodeURIComponent(serial)}&mode=classic`;

    // Seed a verifiable navigation skill.
    const skillId = `docs-open-settings-${Date.now()}`;
    await request.post(`${backend_url}/api/skills`, {
      data: {
        id: skillId,
        version: 1,
        name: '打开设置',
        description: '打开系统设置并验证页面',
        goal: '打开系统设置',
        parameters: {},
        preconditions: [],
        constraints: [],
        success_criteria: ['设置页面可见'],
        steps: [
          {
            id: 'open',
            goal: '打开设置',
            success_criteria: ['设置页面可见'],
            constraints: [],
            max_steps: 8,
            max_resumes: 1,
            effect: 'navigation',
          },
        ],
        max_steps: 20,
        max_seconds: 120,
        enabled: true,
        builtin: false,
      },
    });

    // --- Seed a scheduled task via API ---
    try {
      if (skillId) {
        await request.post(`${backend_url}/api/scheduled-tasks`, {
          data: {
            name: '每天早八签到',
            skill_id: skillId,
            skill_version: 1,
            parameters: {},
            device_serialnos: [serial],
            cron_expression: '0 8 * * *',
            enabled: true,
          },
        });
      }
    } catch (e) {
      console.log('[seed] scheduled task failed:', String(e).split('\n')[0]);
    }

    // --- Load a scenario into the mock agent so a real task run produces steps ---
    try {
      await request.post(`${agent_url}/test/load_scenario`, {
        data: {
          scenario_path:
            'tests/integration/fixtures/scenarios/meituan_message/scenario.yaml',
        },
      });
    } catch (e) {
      console.log('[seed] load_scenario failed:', String(e).split('\n')[0]);
    }

    // Model responses are shared by the E2E stack. Own this scenario instead
    // of inheriting a previous spec's long-running cancellation response.
    const modelResponses = await request.post(`${llm_url}/test/set_responses`, {
      data: [
        '正在检查屏幕上的消息入口。 '.repeat(350) +
          'do(action="Tap", element=[499,966])',
        '已进入消息页面。finish(message="已成功点击消息按钮！")',
      ],
    });
    expect(modelResponses.ok(), await modelResponses.text()).toBe(true);
    expect((await request.post(`${llm_url}/test/reset`)).ok()).toBe(true);

    // --- Populated list pages ---
    await shot(page, 'skills-list', async () => {
      await page.goto('/skills');
      await page.waitForTimeout(1500);
    });
    await shot(page, 'scheduled-list', async () => {
      await page.goto('/scheduled-tasks');
      await page.waitForTimeout(1500);
    });

    // --- Settings dialog scrolled to show agent types + advanced options ---
    await page.goto(chatUrl);
    await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(800);
    await shot(page, 'settings-agent-types', async () => {
      await page.getByRole('button', { name: '设置', exact: true }).click();
      const dialog = page.locator('[role="dialog"]').first();
      await dialog.waitFor({ state: 'visible', timeout: 8000 });
      await page.waitForTimeout(400);
      // scroll the dialog body to the bottom to reveal agent-type selection
      await page.mouse.move(640, 400);
      await page.mouse.wheel(0, 620);
      await page.waitForTimeout(600);
    });
    await closeDialog(page);

    // --- Run a real task on the mock device and capture execution + result ---
    await shot(page, 'chat-running', async () => {
      await page.goto(chatUrl);
      await expect(page.locator('textarea')).toBeVisible({ timeout: 15000 });
      const taskResponse = page.waitForResponse(
        response =>
          response.request().method() === 'POST' &&
          response.url().includes('/api/task-sessions/') &&
          response.url().endsWith('/tasks')
      );
      await page.locator('textarea').fill('点击屏幕下方的消息按钮');
      await page.locator('textarea').press('Meta+Enter');
      const response = await taskResponse;
      expect(response.ok(), await response.text()).toBe(true);
      submittedTaskIds.push(((await response.json()) as TaskRun).id);
      await page.waitForTimeout(2500);
    });
    // Let it finish, then capture the completed conversation
    await shot(page, 'chat-result', async () => {
      expect(submittedTaskIds).toHaveLength(1);
      await expect
        .poll(
          async () =>
            (await readTask(request, backend_url, submittedTaskIds[0])).status,
          { timeout: 30000 }
        )
        .toBe('SUCCEEDED');
      await expect(page.getByTitle('中止对话')).toBeHidden();
    });

    // Execution controls are available in the layered chat entry.
    await shot(page, 'chat-skill-selection', async () => {
      await page.goto(chatUrl.replace('mode=classic', 'mode=chatkit'));
      await page.getByLabel('执行方式', { exact: true }).selectOption('skill');
    });

    // --- Populated history list + detail (select the mock device, not the emulator) ---
    const selectMockDevice = async () => {
      const trigger = page
        .locator('button[aria-haspopup="listbox"], [role="combobox"], button')
        .filter({ hasText: /EMULATOR|MockPhone|mock|设备/i })
        .first();
      await trigger.click({ timeout: 6000 });
      await page.waitForTimeout(500);
      const opts = page.locator('[role="option"]');
      const count = await opts.count();
      for (let i = 0; i < count; i++) {
        const txt = (await opts.nth(i).innerText()).trim();
        if (!/EMULATOR/i.test(txt)) {
          await opts.nth(i).click();
          return;
        }
      }
      await page.keyboard.press('Escape');
    };

    await shot(page, 'history-list', async () => {
      await page.goto('/history');
      await page.waitForTimeout(1500);
      await selectMockDevice();
      await page.waitForTimeout(2000);
    });
    await shot(page, 'history-detail', async () => {
      // open the first history record's detail (click the first record card)
      const firstCard = page
        .locator('[class*="cursor-pointer"], button, [role="button"]')
        .filter({ hasText: /消息|点击|步骤|step/i })
        .first();
      await firstCard.click({ timeout: 5000 });
      await page.waitForTimeout(1200);
    });

    fs.writeFileSync(
      path.join(OUT_DIR, '_manifest.json'),
      JSON.stringify(
        { generatedAt: new Date().toISOString(), results },
        null,
        2
      )
    );
    const failed2 = results.filter(r => !r.ok);
    console.log(
      `\n[docs-screenshots] total ${results.length} shots, ${failed2.length} failed`
    );
    for (const f of failed2) console.log(`  FAILED: ${f.name} — ${f.error}`);
    expect(
      failed2,
      `Expected all screenshots to succeed, but ${failed2.length} failed`
    ).toHaveLength(0);
  });
});
