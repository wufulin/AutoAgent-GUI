import { expect, test, type Page } from '@playwright/test';
import type { SkillDefinition } from '../src/api';

const skill: SkillDefinition = {
  id: 'wechat-contact',
  app: { id: 'wechat', name: '微信' },
  icon: 'message-circle',
  version: 3,
  name: '打开联系人',
  description: '进入指定联系人的聊天页面',
  goal: '打开 {{contact}} 的聊天页面',
  parameters: {
    contact: { type: 'string', description: '联系人姓名', required: true },
  },
  preconditions: ['微信已登录'],
  constraints: ['不得发送消息'],
  success_criteria: ['聊天标题是 {{contact}}'],
  steps: [
    {
      id: 'find',
      goal: '查找 {{contact}}',
      success_criteria: ['标题是 {{contact}}'],
      constraints: ['重名时停止'],
      max_steps: 8,
      max_resumes: 1,
      effect: 'navigation',
    },
  ],
  max_steps: 30,
  max_seconds: 180,
  enabled: true,
  builtin: true,
};
const summary = {
  route: 'skill',
  status: 'completed',
  stop_reason: 'goal_verified',
  elapsed_ms: 12500,
  steps: 4,
  subtasks: 1,
  continuations: 1,
  planner_calls: 0,
  executor_calls: 4,
  verifier_calls: 2,
  input_tokens: null,
  output_tokens: null,
  usage_complete: false,
  calls_complete: false,
};
const device = {
  id: 'test-phone',
  serial: 'test-serial',
  model: '测试手机',
  status: 'device',
  connection_type: 'remote',
  state: 'connected',
  is_available_only: false,
  display_name: null,
  group_id: 'default',
  agent: null,
};
const session = {
  id: 'v2-session',
  kind: 'chat',
  mode: 'layered',
  device_id: device.id,
  device_serial: device.serial,
  status: 'open',
  created_at: '2026-10-21T00:00:00',
  updated_at: '2026-10-21T00:00:00',
};
const task = {
  id: 'v2-task',
  session_id: session.id,
  source: 'chat',
  executor_key: 'layered_chat',
  status: 'SUCCEEDED',
  input_text: '打开联系人',
  final_message: '已验证目标页面',
  error_message: null,
  stop_reason: 'goal_verified',
  step_count: 4,
  created_at: '2026-10-21T00:00:00',
  finished_at: '2026-10-21T00:00:12',
};
const history = {
  id: 'v2-history',
  task_text: task.input_text,
  final_message: task.final_message,
  success: true,
  result_status: 'completed',
  steps: 4,
  start_time: task.created_at,
  end_time: task.finished_at,
  duration_ms: 12500,
  source: 'layered',
  source_detail: '',
  error_message: null,
  messages: [],
  step_timings: [],
  execution_summary: summary,
};

async function mockApp(
  page: Page,
  planner = true,
  catalog: SkillDefinition[] = [skill]
) {
  await page.addInitScript(() => localStorage.setItem('locale', 'zh'));
  await page.routeWebSocket(/\/socket\.io\//, () => {});
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === '/api/devices')
      return route.fulfill({ json: { devices: [device] } });
    if (path === '/api/device-groups')
      return route.fulfill({
        json: {
          groups: [
            {
              id: 'default',
              name: '默认',
              order: 0,
              is_default: true,
              device_count: 1,
            },
          ],
        },
      });
    if (path === '/api/config')
      return route.fulfill({
        json: {
          base_url: 'http://example.test/v1',
          model_name: 'test-vision',
          agent_type: 'glm-async',
          decision_base_url: planner ? 'http://example.test/v1' : null,
          decision_model_name: planner ? 'test-planner' : null,
        },
      });
    if (path === '/api/skills')
      return route.fulfill({ json: { skills: catalog } });
    if (path === '/api/task-sessions') return route.fulfill({ json: session });
    if (path.endsWith('/tasks') && path.includes('/task-sessions/'))
      return route.fulfill({
        json: method === 'POST' ? task : { tasks: [], total: 0 },
      });
    if (path === '/api/tasks/v2-task/events')
      return route.fulfill({
        json: {
          events: [
            {
              event_type: 'execution_route',
              payload: {
                route: 'skill',
                reason: 'explicit_skill',
                skill_id: skill.id,
                skill_version: skill.version,
                skill_name: skill.name,
              },
            },
            { event_type: 'execution_summary', payload: summary },
            {
              event_type: 'done',
              payload: {
                success: true,
                content: task.final_message,
                result_status: 'completed',
              },
            },
          ].map((event, index) => ({
            ...event,
            task_id: task.id,
            seq: index + 1,
            role: 'assistant',
            created_at: task.created_at,
          })),
        },
      });
    if (path.startsWith('/api/history/'))
      return route.fulfill({
        json: { records: [history], total: 1, limit: 50, offset: 0 },
      });
    if (path === '/api/scheduled-tasks')
      return route.fulfill({ json: { tasks: [] } });
    if (path === '/api/screenshot')
      return route.fulfill({
        json: {
          success: false,
          error: 'Mock device preview',
          image: '',
          width: 1,
          height: 1,
        },
      });
    return route.fulfill({ json: {} });
  });
}

test('copies a built-in into an editable versioned skill with structured criteria', async ({
  page,
}) => {
  await mockApp(page);
  let created: SkillDefinition | undefined;
  await page.route('**/api/skills', route => {
    if (route.request().method() === 'POST') {
      created = route.request().postDataJSON();
      return route.fulfill({ json: created });
    }
    return route.fallback();
  });
  await page.goto('/skills');
  await expect(page.getByText('内置 · 只读')).toBeVisible();
  await expect(
    page.getByRole('button', { name: '编辑', exact: true })
  ).toHaveCount(0);
  await page.getByRole('button', { name: '复制为自定义技能' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('名称', { exact: true }).fill('我的联系人导航');
  await dialog.getByLabel('技能标识').fill('my-contact');
  await expect(dialog.getByLabel('参数名', { exact: true })).toHaveValue(
    'contact'
  );
  await dialog
    .getByLabel('最终完成条件（每行一项）')
    .fill('聊天标题是 {{contact}}\n输入框为空');
  if (process.env.AUTOGLM_V2_SCREENSHOTS === '1')
    await page.screenshot({ path: '/tmp/autoglm-v2-skills.png' });
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(created).toMatchObject({
    id: 'my-contact',
    version: 1,
    builtin: false,
    name: '我的联系人导航',
    success_criteria: ['聊天标题是 {{contact}}', '输入框为空'],
    parameters: skill.parameters,
    app: skill.app,
    icon: skill.icon,
  });
  expect(created?.steps[0]).toMatchObject({
    effect: 'navigation',
    max_resumes: 1,
  });
});

test('runs the exact skill version with parameters and renders route and honest usage', async ({
  page,
}) => {
  await mockApp(page);
  await page.goto('/chat?serial=test-serial');
  await page.getByLabel('执行方式', { exact: true }).selectOption('skill');
  await page.getByRole('combobox', { name: '选择技能' }).click();
  await page.getByRole('option', { name: /打开联系人/ }).click();
  await page.getByRole('button', { name: '提交任务' }).click();
  await expect(
    page.getByText('请填写技能必填参数并确认最终完成条件。')
  ).toBeVisible();
  await page.getByLabel('联系人姓名').fill('张三');
  await page
    .getByLabel('补充限制（可选，例如：禁止发送消息）')
    .fill('禁止发送消息');
  const submitted = page.waitForRequest(
    request =>
      request.url().endsWith('/v2-session/tasks') && request.method() === 'POST'
  );
  await page.getByRole('button', { name: '提交任务' }).click();
  expect((await submitted).postDataJSON()).toMatchObject({
    message: '打开联系人',
    execution: {
      route: 'skill',
      skill_id: 'wechat-contact',
      skill_version: 3,
      parameters: { contact: '张三' },
      constraints: ['禁止发送消息'],
      max_steps: 30,
      max_seconds: 180,
    },
  });
  const card = page.getByTestId('execution-summary');
  await expect(card).toContainText('打开联系人 · v3');
  await expect(card).toContainText('12.5s');
  await expect(card).toContainText('未提供完整 token 用量');
  await expect(card).toContainText('未提供完整模型调用次数');
  await expect(card).not.toContainText('explicit_skill');
  await expect(card).toContainText('运行你选择的技能与版本');
  await expect(card.locator('dd').filter({ hasText: /^—$/ })).toHaveCount(2);
  if (process.env.AUTOGLM_V2_SCREENSHOTS === '1')
    await page.screenshot({ path: '/tmp/autoglm-v2-chat.png' });
});

test('exposes direct and skill routes without a planner and requires final criteria', async ({
  page,
}) => {
  await mockApp(page, false);
  await page.goto('/chat?serial=test-serial');
  await expect(page.getByLabel('执行方式', { exact: true })).toHaveValue(
    'direct'
  );
  await page.locator('textarea').last().fill('打开设置');
  await page.getByRole('button', { name: '提交任务' }).click();
  await expect(
    page.getByText('请填写技能必填参数并确认最终完成条件。')
  ).toBeVisible();
  await page.getByLabel('最终完成条件（每行一项）').fill('设置页面已显示');
  const submitted = page.waitForRequest(
    request =>
      request.url().endsWith('/v2-session/tasks') && request.method() === 'POST'
  );
  await page.getByRole('button', { name: '提交任务' }).click();
  expect((await submitted).postDataJSON().execution).toMatchObject({
    route: 'direct',
    success_criteria: ['设置页面已显示'],
  });
});

test('keeps a stale edit open and shows the version conflict', async ({
  page,
}) => {
  await mockApp(page);
  await page.route('**/api/skills', route =>
    route.fulfill({ json: { skills: [{ ...skill, builtin: false }] } })
  );
  let body: unknown;
  await page.route('**/api/skills/wechat-contact', route => {
    body = route.request().postDataJSON();
    return route.fulfill({
      status: 409,
      json: { detail: '技能已更新，请刷新后重试' },
    });
  });
  await page.goto('/skills');
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: '保存', exact: true })
    .click();
  await expect(page.getByRole('dialog').getByRole('alert')).toHaveText(
    '技能已更新，请刷新后重试'
  );
  expect(body).toMatchObject({
    expected_version: 3,
    skill: { id: 'wechat-contact', version: 3 },
  });
});

test('renders persisted execution statistics in history', async ({ page }) => {
  await mockApp(page);
  await page.goto('/history');
  await page.getByTestId('history-record-card').click();
  await expect(
    page.getByRole('dialog').getByTestId('execution-summary')
  ).toContainText('未提供完整 token 用量');
  await expect(
    page.getByRole('dialog').getByTestId('execution-summary')
  ).toContainText('完成条件已验证');
});

test('edits a schedule while preserving its pinned historical version', async ({
  page,
}) => {
  await mockApp(page);
  const schedule = {
    id: 'schedule-1',
    name: '每日打开联系人',
    skill_id: skill.id,
    skill_version: 2,
    parameters: { contact: '李四' },
    device_serialnos: [device.serial],
    device_group_id: null,
    cron_expression: '0 8 * * *',
    enabled: true,
    last_run_time: null,
    last_run_success: null,
    next_run_time: null,
  };
  await page.route('**/api/scheduled-tasks', route =>
    route.fulfill({ json: { tasks: [schedule] } })
  );
  await page.route('**/api/skills/wechat-contact?version=2', route =>
    route.fulfill({ json: { ...skill, version: 2 } })
  );
  let saved: unknown;
  await page.route('**/api/scheduled-tasks/schedule-1', route => {
    saved = route.request().postDataJSON();
    return route.fulfill({ json: schedule });
  });
  await page.goto('/scheduled-tasks');
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(
    dialog.getByRole('combobox', { name: '选择技能' })
  ).toContainText('打开联系人 · v2');
  await expect(dialog.getByLabel('联系人姓名')).toHaveValue('李四');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(saved).toMatchObject({
    skill_id: skill.id,
    skill_version: 2,
    parameters: { contact: '李四' },
  });
  expect(saved).not.toHaveProperty('execution_mode');
  expect(saved).not.toHaveProperty('workflow_uuid');
});

const dianping: SkillDefinition = {
  ...skill,
  id: 'dianping_search_shops',
  name: '大众点评：按城市搜索店铺',
  version: 1,
  app: { id: 'dianping', name: '大众点评' },
  icon: 'search',
  platforms: ['android'],
};
const general: SkillDefinition = {
  ...skill,
  id: 'general-check',
  name: '通用检查',
  app: null,
  icon: null,
  builtin: false,
};
const unknownApp: SkillDefinition = {
  ...skill,
  id: 'another-app-skill',
  name: '其他应用检查',
  app: { id: 'another', name: '其他应用' },
  icon: 'constructor',
  builtin: false,
};

test('groups and filters application skills with local icons on desktop and mobile themes', async ({
  page,
}, testInfo) => {
  await mockApp(page, true, [skill, dianping, general, unknownApp]);
  await page.goto('/skills');
  await expect(
    page.getByRole('region', { name: '微信', exact: true })
  ).toContainText(skill.name);
  await expect(
    page.getByRole('region', { name: '大众点评', exact: true })
  ).toContainText(dianping.name);
  await expect(
    page.getByRole('region', { name: '通用技能', exact: true })
  ).toContainText(general.name);
  await expect(
    page.locator('[data-app-icon="wechat"] svg').first()
  ).toBeVisible();
  await expect(
    page.locator('[data-app-icon="dianping"] svg').first()
  ).toBeVisible();
  await expect(
    page.locator('[data-app-icon="another"] svg').first()
  ).toBeVisible();
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const dark of [false, true]) {
      await page.evaluate(
        value => document.documentElement.classList.toggle('dark', value),
        dark
      );
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth
          )
        )
        .toBe(true);
      for (const card of await page.locator('[data-slot="card"]').all()) {
        const box = await card.boundingBox();
        expect(box && box.x + box.width).toBeLessThanOrEqual(width);
      }
      await page.screenshot({
        path: testInfo.outputPath(
          `skills-${width}-${dark ? 'dark' : 'light'}.png`
        ),
        fullPage: true,
      });
    }
  }
  await page.getByRole('button', { name: '大众点评', exact: true }).click();
  await expect(
    page.getByRole('region', { name: '大众点评', exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: '微信', exact: true })
  ).toHaveCount(0);
  await page.getByRole('button', { name: '通用技能', exact: true }).click();
  await expect(
    page.getByRole('region', { name: '通用技能', exact: true })
  ).toBeVisible();
});

test('edits custom app metadata and returns a skill to the general category', async ({
  page,
}) => {
  await mockApp(page);
  let created: SkillDefinition | undefined;
  await page.route('**/api/skills', route => {
    if (route.request().method() === 'POST') {
      created = route.request().postDataJSON();
      return route.fulfill({ json: created });
    }
    return route.fallback();
  });
  await page.goto('/skills');
  await page.getByRole('button', { name: '复制为自定义技能' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('技能标识').fill('my-app-skill');
  await dialog
    .getByLabel('所属应用', { exact: true })
    .selectOption('__custom__');
  await dialog.getByLabel('应用标识').fill('wechat');
  await expect(dialog.getByLabel('所属应用', { exact: true })).toHaveValue(
    'wechat'
  );
  await expect(dialog.getByLabel('应用名称')).toHaveCount(0);
  await dialog
    .getByLabel('所属应用', { exact: true })
    .selectOption('__custom__');
  await dialog.getByLabel('应用标识').fill('my-app');
  await dialog.getByLabel('应用名称').fill('我的应用');
  await dialog.getByLabel('功能图标').selectOption('search');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(created).toMatchObject({
    app: { id: 'my-app', name: '我的应用' },
    icon: 'search',
  });
  await page.getByRole('button', { name: '复制为自定义技能' }).click();
  await dialog.getByLabel('所属应用', { exact: true }).selectOption('');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(created?.app).toBeNull();
});

test('shared chat picker supports keyboard, groups, platform and disabled filtering', async ({
  page,
}) => {
  await mockApp(page, true, [
    skill,
    dianping,
    general,
    { ...skill, id: 'disabled-skill', name: '已停用技能', enabled: false },
    { ...skill, id: 'ios-skill', name: 'iOS限定技能', platforms: ['ios'] },
  ]);
  await page.goto('/chat?serial=test-serial');
  await page.getByLabel('执行方式', { exact: true }).selectOption('skill');
  const picker = page.getByRole('combobox', { name: '选择技能' });
  await picker.focus();
  await picker.press('ArrowDown');
  await expect(page.getByRole('listbox')).toBeVisible();
  await expect(
    page.getByRole('option', { name: /已停用技能|iOS限定技能/ })
  ).toHaveCount(0);
  await page.getByRole('option', { name: /通用检查/ }).press('Home');
  await page.keyboard.press('Enter');
  await expect(picker).toContainText('通用检查');
  await expect(picker).toBeFocused();
  await picker.click();
  await expect(
    page.getByRole('group', { name: '大众点评', exact: true })
  ).toBeVisible();
  await page.getByRole('option', { name: /大众点评：按城市搜索店铺/ }).click();
  await expect(picker).toContainText('大众点评：按城市搜索店铺');
  await expect(picker.locator('[data-app-icon="dianping"]')).toBeVisible();
});
