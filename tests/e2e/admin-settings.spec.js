import { test, expect } from '@playwright/test';
import { loadFixtures, login, logout } from './helpers.js';

test.describe('owner settings and members', () => {
  test('owner updates settings, creates a member, resets credentials and logs out', async ({ page }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.owner);
    await page.goto('/settings');
    await expect(page.locator('#configForm')).toBeVisible();

    const target = page.locator('#targetQualified');
    const current = Number(await target.inputValue());
    await target.fill(String(current + 1));
    await page.getByRole('button', { name: '保存配置' }).click();
    await expect(page.locator('#notice')).toContainText('配置已保存');

    const username = `e2e_member_${Date.now().toString(36)}`;
    await page.locator('#memberUsername').fill(username);
    await page.locator('#memberName').fill('E2E 新成员');
    await page.locator('#memberRole').selectOption('employee');
    await page.getByRole('button', { name: '创建成员' }).click();
    await expect(page.locator('#notice')).toContainText('成员已创建');

    const memberRow = page.locator('#memberBody tr').filter({ hasText: username }).first();
    await expect(memberRow).toBeVisible();
    await memberRow.getByRole('button', { name: '重置 MFA' }).click();
    await expect(page.locator('#notice')).toContainText('MFA 已重置');
    await memberRow.getByRole('button', { name: '重置口令' }).click();
    await expect(page.locator('#notice')).toContainText('临时口令');

    await page.goto('/agent.html');
    await logout(page);
  });
});
