import { test, expect } from '@playwright/test';
import { expectNoBearerToken, loadFixtures, login, logout } from './helpers.js';

test.describe('cookie session and MFA', () => {
  test('login uses HttpOnly session cookie, readable CSRF cookie and MFA', async ({ page, context }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.manager);
    await expectNoBearerToken(page);

    const cookies = await context.cookies();
    const session = cookies.find(cookie => cookie.name === fixtures.sessionCookie);
    const csrf = cookies.find(cookie => cookie.name === fixtures.csrfCookie);
    expect(session).toBeTruthy();
    expect(session.httpOnly).toBe(true);
    expect(session.sameSite).toBe('Strict');
    expect(csrf).toBeTruthy();
    expect(csrf.httpOnly).toBe(false);
    await expect(page.locator('#currentUser')).toHaveText(fixtures.users.manager.display_name);
  });

  test('logout clears the browser session and returns to login', async ({ page, context }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.manager);
    await logout(page);
    await page.reload();
    await expect(page.locator('#loginBox')).toBeVisible();
  });

  test('expired session returns the agent page to login without a local token', async ({ page, context }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.employee);
    await context.clearCookies();
    await page.getByPlaceholder('问问 Agent，例如：我今天有什么任务？').fill('测试过期会话');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect(page.locator('#loginBox')).toBeVisible();
    await expect(page.locator('#loginHint')).toContainText('登录已过期');
    await expectNoBearerToken(page);
  });

  test('dashboard redirects to login after the session expires', async ({ page, context }) => {
    const fixtures = loadFixtures();
    await login(page, fixtures.users.employee);
    await page.goto('/dashboard');
    await expect(page.locator('#kpis')).not.toContainText('加载中');
    await context.clearCookies();
    await page.reload();
    await expect(page).toHaveURL(/\/agent\.html\?next=/);
    await expect(page.locator('#loginBox')).toBeVisible();
  });
});
