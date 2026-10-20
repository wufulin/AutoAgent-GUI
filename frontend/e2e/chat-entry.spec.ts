import { expect, test, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const serviceUrlsPath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '.service_urls.json'
);

const entryTest = test.extend<{ chatUrl: string }>({
  chatUrl: async ({ page, request }, provide) => {
    const { backend_url, agent_url, llm_url } = JSON.parse(
      fs.readFileSync(serviceUrlsPath, 'utf8')
    );
    await page.addInitScript(() => localStorage.setItem('locale', 'zh'));
    const response = await request.post(
      `${backend_url}/api/devices/add_remote`,
      {
        data: {
          base_url: agent_url,
          device_id: `mock_chat_entry_${Date.now()}`,
        },
      }
    );
    expect(response.ok()).toBeTruthy();
    const { success, serial } = await response.json();
    expect(success).toBe(true);
    expect(serial).toBeTruthy();
    expect(
      (await request.delete(`${backend_url}/api/config`)).ok()
    ).toBeTruthy();
    expect(
      (
        await request.post(`${backend_url}/api/config`, {
          data: {
            base_url: `${llm_url}/v1`,
            model_name: 'mock-glm-model',
            api_key: 'mock-key',
            decision_base_url: `${llm_url}/v1`,
            decision_model_name: 'mock-planner',
            decision_api_key: 'mock-key',
            agent_type: 'glm-async',
          },
        })
      ).ok()
    ).toBeTruthy();
    await provide(`/chat?serial=${encodeURIComponent(serial)}`);
  },
});

async function openAdvanced(page: Page) {
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('tab', { name: '高级设置', exact: true }).click();
}

entryTest(
  'defaults to layered sessions with no top-level mode switch',
  async ({ page, chatUrl }) => {
    const sessionRequest = page.waitForRequest(
      request =>
        request.url().endsWith('/api/task-sessions') &&
        request.method() === 'POST'
    );
    await page.goto(chatUrl);
    expect((await sessionRequest).postDataJSON().mode).toBe('layered');
    await expect(page.getByRole('heading', { name: 'AI 助手' })).toBeVisible();
    await expect(
      page.getByRole('button', { name: '经典模式', exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByRole('button', { name: '分层代理', exact: true })
    ).toHaveCount(0);
    await openAdvanced(page);
    await expect(page.getByLabel('聊天执行方式')).toHaveValue('chatkit');
  }
);

entryTest(
  'saves direct execution, preserves the URL on reload, and can return to planning',
  async ({ page, chatUrl }) => {
    await page.goto(chatUrl);
    await expect(page.locator('textarea')).toBeVisible();
    await openAdvanced(page);
    await page.getByLabel('聊天执行方式').selectOption('classic');
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await expect(page).toHaveURL(/mode=chatkit/);
    await openAdvanced(page);
    await expect(page.getByLabel('聊天执行方式')).toHaveValue('chatkit');
    await page.getByLabel('聊天执行方式').selectOption('classic');
    await page.keyboard.press('Escape');
    await openAdvanced(page);
    await expect(page.getByLabel('聊天执行方式')).toHaveValue('chatkit');
    await page.getByLabel('聊天执行方式').selectOption('classic');
    const sessionRequest = page.waitForRequest(
      request =>
        request.url().endsWith('/api/task-sessions') &&
        request.method() === 'POST'
    );
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    expect((await sessionRequest).postDataJSON().mode).toBe('classic');
    await expect(page).toHaveURL(/mode=classic/);
    await page.reload();
    await expect(page.locator('textarea')).toBeVisible();
    await openAdvanced(page);
    await expect(page.getByLabel('聊天执行方式')).toHaveValue('classic');
    await page.getByLabel('聊天执行方式').selectOption('chatkit');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect(page).toHaveURL(/mode=chatkit/);
    await expect(page.getByRole('heading', { name: 'AI 助手' })).toBeVisible();
  }
);

entryTest(
  'keeps legacy classic URLs and isolates changes from backend defaults',
  async ({ page, chatUrl, request }) => {
    const sessionRequest = page.waitForRequest(
      request =>
        request.url().endsWith('/api/task-sessions') &&
        request.method() === 'POST'
    );
    await page.goto(`${chatUrl}&mode=classic`);
    const sessionBody = (await sessionRequest).postDataJSON();
    expect(sessionBody.mode).toBe('classic');
    await openAdvanced(page);
    await expect(page.getByLabel('聊天执行方式')).toHaveValue('classic');
    await page.getByLabel('聊天执行方式').selectOption('chatkit');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect(page).toHaveURL(/mode=chatkit/);
    const { backend_url } = JSON.parse(
      fs.readFileSync(serviceUrlsPath, 'utf8')
    );
    const legacyBody = {
      device_id: sessionBody.device_id,
      device_serial: sessionBody.device_serial,
    };
    const response = await request.post(`${backend_url}/api/task-sessions`, {
      data: legacyBody,
    });
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).mode).toBe('classic');
  }
);

entryTest(
  'guides vision-only users to planner setup or direct execution',
  async ({ page, chatUrl }) => {
    await page.route('**/api/config', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch();
      const config = await response.json();
      await route.fulfill({
        json: { ...config, decision_base_url: null, decision_model_name: null },
      });
    });
    await page.goto(chatUrl);
    await expect(
      page.getByRole('heading', { name: '配置决策模型以开始使用' })
    ).toBeVisible();
    await expect(page.locator('textarea')).toHaveCount(0);
    await page
      .getByRole('button', { name: '配置决策模型', exact: true })
      .click();
    await expect(
      page.getByRole('tab', { name: '决策模型', exact: true })
    ).toHaveAttribute('data-state', 'active');
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '高级设置', exact: true }).click();
    await page.getByLabel('聊天执行方式').selectOption('classic');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect(page).toHaveURL(/mode=classic/);
    await expect(page.locator('textarea')).toBeVisible();
  }
);

entryTest(
  'configures DeepSeek for vision and planning with user-provided keys',
  async ({ page, chatUrl }) => {
    await page.goto(chatUrl);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.locator('#api_key').fill('');
    await page.getByRole('button', { name: 'DeepSeek V4.1 Flash' }).click();
    await expect(page.locator('#base_url')).toHaveValue(
      'https://api.deepseek.com'
    );
    await expect(page.locator('#model_name')).toHaveValue('deepseek-flash');
    await expect(page.locator('#api_key')).toHaveValue('');
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await expect(page.locator('#model_name')).toHaveValue('mock-glm-model');
    await expect(page.locator('#api_key')).toHaveValue('mock-key');
    await page.getByRole('button', { name: 'DeepSeek V4.1 Flash' }).click();
    // Returning to AutoGLM presets must restore their execution protocol.
    for (const name of ['智谱 BigModel', 'ModelScope']) {
      await page.getByRole('button', { name, exact: false }).click();
      await expect(
        page.getByRole('button', { name: 'GLM Agent', exact: false })
      ).toHaveClass(/border-\[#1d9bf0\]/);
      await page.getByRole('button', { name: 'DeepSeek V4.1 Flash' }).click();
    }
    await page.locator('#api_key').fill('test-vision-key');
    await page.getByRole('tab', { name: '决策模型', exact: true }).click();
    await page.locator('#decision_api_key').fill('');
    await page.getByRole('button', { name: 'DeepSeek V4.1 Flash' }).click();
    await expect(page.locator('#decision_base_url')).toHaveValue(
      'https://api.deepseek.com'
    );
    await expect(page.locator('#decision_model_name')).toHaveValue(
      'deepseek-flash'
    );
    await expect(page.locator('#decision_api_key')).toHaveValue('');
    await page.locator('#decision_api_key').fill('test-decision-key');
    const saveRequest = page.waitForRequest(
      request =>
        request.url().endsWith('/api/config') && request.method() === 'POST'
    );
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    expect((await saveRequest).postDataJSON()).toMatchObject({
      base_url: 'https://api.deepseek.com',
      model_name: 'deepseek-flash',
      api_key: 'test-vision-key',
      agent_type: 'gemini',
      decision_base_url: 'https://api.deepseek.com',
      decision_model_name: 'deepseek-flash',
      decision_api_key: 'test-decision-key',
    });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.reload();
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await expect(page.locator('#model_name')).toHaveValue('deepseek-flash');
    await expect(page.locator('#api_key')).toHaveValue('test-vision-key');
    await page.getByRole('tab', { name: '决策模型', exact: true }).click();
    await expect(page.locator('#decision_model_name')).toHaveValue(
      'deepseek-flash'
    );
    await expect(page.locator('#decision_api_key')).toHaveValue(
      'test-decision-key'
    );
  }
);

for (const removedAgent of ['mai', 'droidrun', 'midscene']) {
  entryTest(
    `requires an explicit replacement for removed executor ${removedAgent}`,
    async ({ page, chatUrl }) => {
      let legacyConfig = true;
      await page.route('**/api/config', async route => {
        if (route.request().method() !== 'GET') {
          legacyConfig = false;
          return route.continue();
        }
        if (!legacyConfig) return route.continue();
        const response = await route.fetch();
        await route.fulfill({
          json: {
            ...(await response.json()),
            agent_type: removedAgent,
            agent_type_supported: false,
            agent_config_params: {
              history_n: 3,
              model_family: 'doubao-vision',
            },
          },
        });
      });
      await page.goto(chatUrl);
      await expect(page.locator('textarea')).toHaveCount(0);
      await page
        .getByRole('button', { name: '选择执行器', exact: true })
        .click();
      const save = page.getByRole('button', { name: '保存配置', exact: true });
      await expect(save).toBeDisabled();
      for (const name of ['MAI Agent', 'DroidRun Agent', 'Midscene Agent']) {
        await expect(
          page.getByRole('button', { name, exact: false })
        ).toHaveCount(0);
      }
      await page.getByRole('button', { name: /GLM Agent/ }).click();
      const saved = page.waitForRequest(
        request =>
          request.url().endsWith('/api/config') && request.method() === 'POST'
      );
      await save.click();
      const body = (await saved).postDataJSON();
      expect(body.agent_type).toBe('glm-async');
      expect(body.agent_config_params).toEqual({});
      await expect(page.locator('textarea')).toBeVisible();
    }
  );
}

type LayeredTestEvent = {
  task_id: string;
  seq: number;
  event_type: string;
  role: string;
  payload: Record<string, unknown>;
  created_at: string;
};
function layeredEvent(
  seq: number,
  event_type: string,
  payload: Record<string, unknown>
): LayeredTestEvent {
  return {
    task_id: 'verified-task',
    seq,
    event_type,
    payload,
    role: 'assistant',
    created_at: '2026-01-03T10:00:00',
  };
}
async function mockLayeredEvents(
  page: Page,
  initialEvents: LayeredTestEvent[],
  streamedEvents: LayeredTestEvent[] = []
) {
  let session = {
    id: 'verified-session',
    kind: 'chat',
    mode: 'layered',
    status: 'open',
    device_id: '',
    device_serial: '',
    created_at: '2026-01-03T10:00:00',
    updated_at: '2026-01-03T10:00:00',
  };
  const task = {
    id: 'verified-task',
    session_id: session.id,
    source: 'chat',
    executor_key: 'layered_chat',
    status: 'RUNNING',
    input_text: '打开设置',
    final_message: null,
    error_message: null,
    stop_reason: null,
    step_count: 0,
    created_at: '2026-01-03T10:00:00',
  };
  let releaseStream = () => {};
  const streamReady = new Promise<void>(resolve => {
    releaseStream = resolve;
  });
  await page.route('**/api/task-sessions**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith('/tasks'))
      return route.fulfill({
        json: { tasks: [task], total: 1, limit: 50, offset: 0 },
      });
    if (route.request().method() === 'POST')
      session = { ...session, ...route.request().postDataJSON() };
    await route.fulfill({ json: session });
  });
  await page.route('**/api/tasks/verified-task/**', async route => {
    if (new URL(route.request().url()).pathname.endsWith('/events'))
      return route.fulfill({ json: { events: initialEvents } });
    await streamReady;
    await route.fulfill({
      contentType: 'text/event-stream',
      body: streamedEvents
        .map(
          event =>
            `event: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`
        )
        .join(''),
    });
  });
  return releaseStream;
}

for (const phase of ['executing', 'verifying'] as const) {
  entryTest(
    `shows ${phase} and merges resumed subtask results`,
    async ({ page, chatUrl }) => {
      const result = {
        subtask_id: 'subtask-settings',
        goal: '打开设置',
        status: 'partial',
        message: '已进入应用列表',
        stop_reason: 'step_budget',
        steps: 5,
        total_steps: 5,
        success_criteria: ['设置标题可见'],
        checkpoint: 'opaque-checkpoint',
      };
      const completed = {
        ...result,
        status: 'completed',
        message: '已看到设置标题',
        stop_reason: 'goal_verified',
        steps: 2,
        total_steps: 7,
        checkpoint: null,
        verification: {
          criteria: [
            {
              criterion: '设置标题可见',
              verdict: 'met',
              reason: '新截图显示设置标题',
            },
          ],
          facts: ['当前为设置页面'],
          observed_at: '2026-01-03T10:00:03',
          screenshot_sha256: 'digest',
        },
      };
      const release = await mockLayeredEvents(
        page,
        [
          layeredEvent(1, 'subtask_progress', {
            ...result,
            phase: 'executing',
          }),
          layeredEvent(2, 'tool_result', {
            tool_name: 'execute_subtask',
            result,
          }),
          layeredEvent(3, 'subtask_progress', { ...result, phase }),
        ],
        [
          layeredEvent(4, 'tool_result', {
            tool_name: 'execute_subtask',
            result: completed,
          }),
          // A queued progress notification must not reopen a final result.
          layeredEvent(5, 'subtask_progress', {
            ...result,
            phase: 'verifying',
          }),
          layeredEvent(6, 'done', {
            success: true,
            content: '目标已验证',
            result_status: 'completed',
          }),
        ]
      );
      try {
        await page.goto(chatUrl);
        const card = page.getByTestId('subtask-card');
        await expect(card).toHaveCount(1);
        await expect(card).toContainText(
          phase === 'executing' ? '正在执行' : '正在验证'
        );
        release();
        await expect(card).toContainText('已验证完成');
        await expect(card).toContainText('累计 7 步');
        await expect(card).toHaveCount(1);
        await card.getByText('查看验证结果', { exact: true }).click();
        await expect(card).toContainText('新截图显示设置标题');
      } finally {
        release();
      }
    }
  );
}

for (const terminal of ['done', 'takeover'] as const) {
  entryTest(
    `does not report ${terminal} as success when execution is incomplete`,
    async ({ page, chatUrl }) => {
      const release = await mockLayeredEvents(page, [
        layeredEvent(1, terminal, {
          success: false,
          message: '需要处理设备上的登录',
          content: '需要处理设备上的登录',
          result_status: terminal === 'takeover' ? 'needs_user' : 'failed',
        }),
      ]);
      try {
        await page.goto(chatUrl);
        await expect(
          page.locator('[data-result-status="completed"]')
        ).toHaveCount(0);
        await expect(
          page.locator(
            `[data-result-status="${terminal === 'takeover' ? 'needs_user' : 'failed'}"]`
          )
        ).toBeVisible();
        if (terminal === 'takeover') {
          await expect(
            page.getByText(/请先完成所需操作，再描述当前状态并提交新任务/)
          ).toBeVisible();
          await expect(
            page.getByRole('button', { name: '继续任务', exact: true })
          ).toHaveCount(0);
          await page.reload();
          await expect(
            page.getByText(/请先完成所需操作，再描述当前状态并提交新任务/)
          ).toBeVisible();
        }
      } finally {
        release();
      }
    }
  );
}

entryTest(
  'clears verification progress when the task is cancelled',
  async ({ page, chatUrl }) => {
    const release = await mockLayeredEvents(
      page,
      [
        layeredEvent(1, 'subtask_progress', {
          subtask_id: 'cancelled-subtask',
          goal: '检查设置页面',
          phase: 'verifying',
          total_steps: 3,
        }),
      ],
      [
        layeredEvent(2, 'cancelled', {
          message: '任务已取消',
          stop_reason: 'user_stopped',
        }),
      ]
    );
    try {
      await page.goto(chatUrl);
      const card = page.getByTestId('subtask-card');
      await expect(card).toContainText('正在验证');
      release();
      await expect(card).toContainText('已取消');
      await expect(card).not.toContainText('正在验证');
    } finally {
      release();
    }
  }
);
