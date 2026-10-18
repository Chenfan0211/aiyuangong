import { test, expect } from '@playwright/test';
import { loadFixtures, login, logout, onePixelPng } from './helpers.js';

test.describe('task evidence and verification', () => {
  test('employee uploads image evidence, completes, and manager verifies to verified', async ({ page }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.employee);

    const readyResponse = await page.request.get('/api/tasks/today?status=ready');
    const readyTasks = (await readyResponse.json()).tasks || [];
    expect(readyTasks.length).toBeGreaterThan(0);
    const title = readyTasks[0].title;
    const taskCard = page.locator('#taskCards .card').filter({ hasText: title }).first();
    await expect(taskCard).toBeVisible();
    await taskCard.getByRole('button', { name: '接受任务' }).click();

    const acceptedCard = page.locator('#taskCards .card').filter({ hasText: title }).first();
    await expect(acceptedCard.getByRole('button', { name: '上传图片证据' })).toBeVisible();
    const fileChooserPromise = page.waitForEvent('filechooser');
    await acceptedCard.getByRole('button', { name: '上传图片证据' }).click();
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({ name: 'evidence.png', mimeType: 'image/png', buffer: onePixelPng() });
    await expect(page.locator('#chat')).toContainText('图片证据已上传');

    const evidenceCard = page.locator('#taskCards .card').filter({ hasText: title }).first();
    page.once('dialog', dialog => dialog.accept('E2E-ORDER-001'));
    await evidenceCard.getByRole('button', { name: '提交证据并完成' }).click();
    await expect(page.locator('#chat')).toContainText('任务已更新');

    await logout(page);
    await login(page, fixtures.users.manager);
    await expect(page.locator('#reviewPanel')).toBeVisible();
    const reviewCard = page.locator('#reviewCards .card').filter({ hasText: title }).first();
    await expect(reviewCard).toBeVisible();
    await reviewCard.getByRole('button', { name: '验收通过' }).click();
    await expect(page.locator('#chat')).toContainText('任务已验收');
  });
});
