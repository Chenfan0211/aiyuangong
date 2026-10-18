import { test, expect } from '@playwright/test';
import { expectNoBearerToken, loadFixtures, login, logout } from './helpers.js';

const SESSION_USER = { name: 'E2E 店长', role: 'manager', role_label: '店长' };
const MANAGER_PERMISSIONS = ['agent:chat', 'tasks:read:store', 'tasks:verify', 'report:read'];

function jsonRoute(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

function sessionPayload(overrides = {}) {
  return {
    token: '',
    mfa_required: false,
    mfa_enrollment_required: false,
    must_change_password: false,
    user: SESSION_USER,
    tenant_id: 'LPL-001',
    store_id: 'WH-LION-001',
    permissions: MANAGER_PERMISSIONS,
    ...overrides,
  };
}

function minimalBootstrap() {
  return {
    task_count: 0,
    llm_enabled: false,
    data_status: { platforms: { meituan: null, douyin: null } },
  };
}

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

  test('MFA enrollment shows TOTP details and one-time recovery codes', async ({ page }) => {
    const fixtures = loadFixtures();
    let loggedIn = false;
    let enrolled = false;
    let setupCalls = 0;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/auth/login') {
        expect(request.method()).toBe('POST');
        expect(request.postDataJSON()).toEqual({
          username: fixtures.users.manager.username,
          password: fixtures.users.manager.password,
        });
        loggedIn = true;
        return jsonRoute(route, sessionPayload({ mfa_enrollment_required: true }));
      }
      if (path === '/api/auth/me') {
        if (!loggedIn) return jsonRoute(route, { detail: '未登录' }, 401);
        return jsonRoute(route, sessionPayload({ mfa_enrollment_required: !enrolled }));
      }
      if (path === '/api/auth/mfa/setup') {
        setupCalls += 1;
        expect(request.method()).toBe('POST');
        return jsonRoute(route, {
          secret: 'JBSWY3DPEHPK3PXP',
          otpauth_uri: 'otpauth://totp/门店运营系统:e2e-manager?secret=JBSWY3DPEHPK3PXP&issuer=门店运营系统',
        });
      }
      if (path === '/api/auth/mfa/enable') {
        expect(request.method()).toBe('POST');
        expect(request.postDataJSON()).toEqual({ code: '123456' });
        enrolled = true;
        return jsonRoute(route, {
          enabled: true,
          recovery_codes: [
            'aaaa-bbbb-cccc', 'dddd-eeee-ffff', '1111-2222-3333',
            '4444-5555-6666', '7777-8888-9999', 'abcd-ef01-2345',
            '6789-abcd-ef01', '2345-6789-abcd',
          ],
        });
      }
      if (path === '/api/bootstrap') return jsonRoute(route, minimalBootstrap());
      if (path === '/api/tasks/today') return jsonRoute(route, { tasks: [] });
      if (path === '/api/data-quality') return jsonRoute(route, { summary: { has_data: false } });
      return jsonRoute(route, { detail: 'Unexpected test API: ' + path }, 500);
    });

    await page.goto('/agent.html');
    await page.getByLabel('门店账号').fill(fixtures.users.manager.username);
    await page.getByLabel('口令', { exact: true }).fill(fixtures.users.manager.password);
    await page.getByRole('button', { name: '登录', exact: true }).click();

    await expect(page.locator('#securityGate')).toBeVisible();
    await expect(page.getByRole('heading', { name: '绑定动态验证码' })).toBeVisible();
    await expect(page.getByLabel('TOTP 密钥')).toHaveText('JBSWY3DPEHPK3PXP');
    await expect(page.getByLabel('otpauth 链接')).toContainText('otpauth://totp/');
    expect(setupCalls).toBe(1);

    await page.getByLabel('动态验证码', { exact: true }).fill('123456');
    await page.getByRole('button', { name: '绑定并启用 MFA' }).click();
    await expect(page.getByRole('heading', { name: '恢复码（仅显示一次）' })).toBeVisible();
    await expect(page.locator('#recoveryCodes li')).toHaveCount(8);
    await expect(page.locator('#recoveryCodes')).toContainText('aaaa-bbbb-cccc');

    await page.getByRole('button', { name: '我已保存，继续' }).click();
    await expect(page.locator('#securityGate')).toBeHidden();
    await expect(page.locator('#userCard')).toBeVisible();
    await expect(page.locator('#recoveryCodes li')).toHaveCount(0);
    await page.reload();
    await expect(page.locator('#securityGate')).toBeHidden();
    await expect(page.locator('#recoveryCodes li')).toHaveCount(0);
  });

  test('reset MFA keeps the session in the enrollment-only gate', async ({ page }) => {
    let setupCalls = 0;
    let bootstrapCalls = 0;
    let logoutCalls = 0;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/auth/me') {
        return jsonRoute(route, sessionPayload({ mfa_enrollment_required: true }));
      }
      if (path === '/api/auth/mfa/setup') {
        setupCalls += 1;
        expect(request.method()).toBe('POST');
        return jsonRoute(route, { secret: 'RESET-MFA-SECRET', otpauth_uri: 'otpauth://totp/reset' });
      }
      if (path === '/api/auth/logout') {
        logoutCalls += 1;
        return jsonRoute(route, { revoked: true });
      }
      if (path === '/api/bootstrap') {
        bootstrapCalls += 1;
        return jsonRoute(route, minimalBootstrap());
      }
      return jsonRoute(route, { detail: 'Unexpected test API: ' + path }, 500);
    });

    await page.goto('/agent.html');
    await expect(page.getByRole('heading', { name: '账户安全设置' })).toBeVisible();
    await expect(page.locator('#mfaSetupCard')).toBeVisible();
    await expect(page.getByLabel('TOTP 密钥')).toHaveText('RESET-MFA-SECRET');
    await expect(page.locator('#passwordSetupCard')).toBeHidden();
    await expect(page.locator('#chat')).toBeHidden();
    await expect(page.locator('#composer')).toBeHidden();
    await expect(page.locator('#opsSection')).toBeHidden();
    await expect(page.locator('#appLinks')).toBeHidden();
    expect(setupCalls).toBe(1);
    expect(bootstrapCalls).toBe(0);

    await page.getByRole('button', { name: '退出登录' }).click();
    await expect(page.locator('#loginBox')).toBeVisible();
    await expect(page.locator('#securityGate')).toBeHidden();
    expect(logoutCalls).toBe(1);
  });

  test('MFA reset during an active session gates the next business request', async ({ page }) => {
    let mfaRequired = false;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/auth/me') {
        return jsonRoute(route, sessionPayload({ mfa_enrollment_required: mfaRequired }));
      }
      if (path === '/api/auth/mfa/setup') {
        return jsonRoute(route, { secret: 'LATE-GATE-SECRET', otpauth_uri: 'otpauth://totp/late' });
      }
      if (path === '/api/bootstrap') return jsonRoute(route, minimalBootstrap());
      if (path === '/api/tasks/today') return jsonRoute(route, { tasks: [] });
      if (path === '/api/data-quality') return jsonRoute(route, { summary: { has_data: false } });
      if (path === '/api/reports/daily') {
        if (mfaRequired) return jsonRoute(route, { detail: '必须先完成 MFA 绑定' }, 403);
        return jsonRoute(route, { report: '正常日报', sources: [] });
      }
      return jsonRoute(route, { detail: 'Unexpected test API: ' + path }, 500);
    });

    await page.goto('/agent.html');
    await expect(page.locator('#chat')).toBeVisible();
    mfaRequired = true;
    await page.getByRole('button', { name: '生成今日日报' }).click();

    await expect(page.locator('#securityGate')).toBeVisible();
    await expect(page.locator('#mfaSetupCard')).toBeVisible();
    await expect(page.getByLabel('TOTP 密钥')).toHaveText('LATE-GATE-SECRET');
    await expect(page.locator('#chat')).toBeHidden();
  });

  test('temporary password forces password change and returns to login', async ({ page }) => {
    let passwordBody = null;
    let changed = false;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/auth/me') {
        return jsonRoute(route, sessionPayload({ must_change_password: !changed }));
      }
      if (path === '/api/auth/password') {
        expect(request.method()).toBe('POST');
        passwordBody = request.postDataJSON();
        changed = true;
        return jsonRoute(route, { changed: true, must_change_password: false });
      }
      if (path === '/api/bootstrap') return jsonRoute(route, minimalBootstrap());
      return jsonRoute(route, { detail: 'Unexpected test API: ' + path }, 500);
    });

    await page.goto('/agent.html');
    await expect(page.getByRole('heading', { name: '修改临时口令' })).toBeVisible();
    await expect(page.locator('#mfaSetupCard')).toBeHidden();
    await expect(page.locator('#chat')).toBeHidden();
    await expect(page.locator('#appLinks')).toBeHidden();

    await page.getByLabel('当前临时口令').fill('Initial-Password-2026');
    await page.getByLabel('新口令', { exact: true }).fill('Changed-Password-2026');
    await page.getByLabel('确认新口令').fill('Changed-Password-2026');
    await page.getByRole('button', { name: '修改口令' }).click();

    expect(passwordBody).toEqual({
      current_password: 'Initial-Password-2026',
      new_password: 'Changed-Password-2026',
    });
    await expect(page.locator('#loginBox')).toBeVisible();
    await expect(page.locator('#loginHint')).toContainText('口令已修改');
    await expect(page.locator('#passwordSetupCard')).toBeHidden();
    await expectNoBearerToken(page);
  });

  test('MFA enrollment is completed before the temporary password gate', async ({ page }) => {
    let enrolled = false;
    let changed = false;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/auth/me') {
        return jsonRoute(route, sessionPayload({
          mfa_enrollment_required: !enrolled,
          must_change_password: !changed,
        }));
      }
      if (path === '/api/auth/mfa/setup') {
        return jsonRoute(route, { secret: 'BOTH-GATE-SECRET', otpauth_uri: 'otpauth://totp/both' });
      }
      if (path === '/api/auth/mfa/enable') {
        enrolled = true;
        return jsonRoute(route, { enabled: true, recovery_codes: ['1111-2222-3333'] });
      }
      if (path === '/api/auth/password') {
        expect(request.method()).toBe('POST');
        changed = true;
        return jsonRoute(route, { changed: true, must_change_password: false });
      }
      if (path === '/api/bootstrap') return jsonRoute(route, minimalBootstrap());
      return jsonRoute(route, { detail: 'Unexpected test API: ' + path }, 500);
    });

    await page.goto('/agent.html');
    await expect(page.locator('#mfaSetupCard')).toBeVisible();
    await expect(page.locator('#passwordSetupCard')).toBeHidden();
    await page.getByLabel('动态验证码', { exact: true }).fill('123456');
    await page.getByRole('button', { name: '绑定并启用 MFA' }).click();
    await page.getByRole('button', { name: '我已保存，继续' }).click();

    await expect(page.locator('#mfaSetupCard')).toBeHidden();
    await expect(page.getByRole('heading', { name: '修改临时口令' })).toBeVisible();
    await page.getByLabel('当前临时口令').fill('Initial-Password-2026');
    await page.getByLabel('新口令', { exact: true }).fill('Changed-Password-2026');
    await page.getByLabel('确认新口令').fill('Changed-Password-2026');
    await page.getByRole('button', { name: '修改口令' }).click();
    await expect(page.locator('#loginBox')).toBeVisible();
  });
});
