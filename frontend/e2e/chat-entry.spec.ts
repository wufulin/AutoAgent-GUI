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
