import { test, expect } from '@playwright/test';
import { loadFixtures, login } from './helpers.js';

test.describe('task details and advice', () => {
  test('employee expands task details and receives an action plan', async ({ page }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.employee);

    const card = page.locator('#taskCards .card').first();
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: '查看详情' }).click();
    await expect(card.locator('.task-detail')).toBeVisible();
    await expect(card.locator('.task-detail')).toContainText('生成依据');
    await expect(card.locator('.task-detail')).toContainText('需要提交的证据');

    await page.getByPlaceholder('问问 Agent，例如：我今天有什么任务？').fill('给我一个提升曝光的方案');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const advice = page.locator('.advice-block').last();
    await expect(advice).toBeVisible();
    await expect(advice).toContainText('优先动作');
  });
});