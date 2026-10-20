import { test, expect } from '@playwright/test';
import { loadFixtures, login, logout } from './helpers.js';

test.describe('phase 1 AI store manager', () => {
  test('owner binds a chat account and manager asks for period diagnosis', async ({ page }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.owner);
    await page.goto('/settings');
    await expect(page.locator('#bindingForm')).toBeVisible();
    const externalId = 'e2e-ext-' + Date.now().toString(36);
    const mobile = '138' + String(Date.now()).slice(-8);
    await page.locator('#bindingProvider').selectOption('mock');
    await page.locator('#bindingExternalId').fill(externalId);
    await page.locator('#bindingUser').selectOption(fixtures.users.manager.user_id);
    await page.locator('#bindingMobile').fill(mobile);
    await page.getByRole('button', { name: '创建绑定' }).click();
    await expect(page.locator('#notice')).toContainText('绑定已创建');
    const row = page.locator('#bindingBody tr').filter({ hasText: externalId }).first();
    await expect(row).toBeVisible();
    await expect(row).toContainText(mobile.slice(0, 3) + '****' + mobile.slice(-4));
    await row.getByRole('button', { name: '停用' }).click();
    await expect(page.locator('#notice')).toContainText('绑定已更新');
    await expect(row).toContainText('disabled');

    await page.goto('/agent.html');
    await logout(page);
    await login(page, fixtures.users.manager);
    await page.locator('#input').fill('对比昨天和前7天数据，为什么订单下降？');
    await page.getByRole('button', { name: '发送' }).click();
    await expect(page.locator('#chat')).toContainText('周期对比');
    await expect(page.locator('#chat')).toContainText('依据');
  });
});