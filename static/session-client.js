(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.StoreSession = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COOKIE = Object.freeze({
    session: 'store_ops_session',
    csrf: 'store_ops_csrf'
  });
  const CSRF_HEADER = 'X-CSRF-Token';
  const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

  function readCookie(name) {
    if (typeof document === 'undefined' || typeof document.cookie !== 'string') return '';
    const prefix = encodeURIComponent(name) + '=';
    for (const part of document.cookie.split(';')) {
      const item = part.trim();
      if (!item.startsWith(prefix)) continue;
      const raw = item.slice(prefix.length);
      try {
        return decodeURIComponent(raw);
      } catch (_error) {
        return raw;
      }
    }
    return '';
  }

  async function readPayload(response) {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_error) {
      return { detail: text };
    }
  }

  class SessionError extends Error {
    constructor(message, details) {
      super(message);
      this.name = 'SessionError';
      this.status = details && details.status ? details.status : 0;
      this.payload = (details && details.payload) || {};
      this.method = (details && details.method) || '';
      this.url = (details && details.url) || '';
    }
  }

  class SessionClient {
    constructor(options) {
      const opts = options || {};
      this.baseUrl = String(opts.baseUrl || '').replace(/\/$/, '');
      this.onUnauthorized = typeof opts.onUnauthorized === 'function' ? opts.onUnauthorized : null;
      this.redirectToLogin = opts.redirectToLogin !== false;
      this.loginUrl = opts.loginUrl || '/agent.html';
    }

    csrfToken() {
      return readCookie(COOKIE.csrf);
    }

    setUnauthorizedHandler(handler) {
      this.onUnauthorized = typeof handler === 'function' ? handler : null;
      return this;
    }

    resolveUrl(path) {
      const value = String(path || '');
      if (/^https?:\/\//i.test(value) || !this.baseUrl) return value;
      return this.baseUrl + (value.startsWith('/') ? value : '/' + value);
    }

    async request(path, options) {
      const opts = options || {};
      const method = String(opts.method || 'GET').toUpperCase();
      const headers = new Headers(opts.headers || {});
      if (!SAFE_METHODS.has(method)) {
        const token = this.csrfToken();
        if (token) headers.set(CSRF_HEADER, token);
      }

      const response = await fetch(this.resolveUrl(path), {
        ...opts,
        method,
        headers,
        credentials: 'same-origin'
      });

      if (response.status === 401) {
        const payload = await readPayload(response);
        const error = new SessionError(payload.detail || payload.error || '登录已过期，请重新登录', {
          status: 401,
          payload,
          method,
          url: path
        });
        if (this.onUnauthorized) {
          await this.onUnauthorized(error);
        } else {
          this.redirectToLoginPage();
        }
        throw error;
      }
      return response;
    }

    async json(path, options) {
      const response = await this.request(path, options);
      const payload = await readPayload(response);
      if (!response.ok) {
        throw new SessionError(payload.detail || payload.error || ('HTTP ' + response.status), {
          status: response.status,
          payload,
          method: String((options && options.method) || 'GET').toUpperCase(),
          url: path
        });
      }
      return payload;
    }

    async streamJson(path, body, onEvent) {
      // SSE 读取器必须放在共享客户端里：4 个页面脚本被契约测试禁止出现 fetch(。
      const response = await this.request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body === undefined ? {} : body)
      });
      if (!response.ok || !response.body) {
        const payload = await readPayload(response);
        throw new SessionError(payload.detail || payload.error || ('HTTP ' + response.status), {
          status: response.status,
          payload,
          method: 'POST',
          url: path
        });
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      const handle = (line) => {
        const value = String(line).trim();
        if (!value.startsWith('data:')) return;
        const raw = value.slice(5).trim();
        if (!raw || raw === '[DONE]') return;
        let event = null;
        try {
          event = JSON.parse(raw);
        } catch (_error) {
          return;
        }
        if (typeof onEvent === 'function') onEvent(event);
      };
      let buffer = '';
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const parts = buffer.split('\n');
        buffer = parts.pop() || '';
        parts.forEach(handle);
      }
      if (buffer) handle(buffer);
    }

    getJson(path, options) {
      return this.json(path, { ...(options || {}), method: 'GET' });
    }

    postJson(path, body, options) {
      return this.json(path, {
        ...(options || {}),
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...((options && options.headers) || {}) },
        body: JSON.stringify(body === undefined ? {} : body)
      });
    }

    patchJson(path, body, options) {
      return this.json(path, {
        ...(options || {}),
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...((options && options.headers) || {}) },
        body: JSON.stringify(body === undefined ? {} : body)
      });
    }

    upload(path, file, options) {
      const headers = { ...((options && options.headers) || {}) };
      if (!headers['Content-Type'] && file && file.type) headers['Content-Type'] = file.type;
      return this.json(path, {
        ...(options || {}),
        method: 'POST',
        headers,
        body: file
      });
    }

    redirectToLoginPage() {
      if (!this.redirectToLogin || typeof location === 'undefined') return;
      const next = location.pathname + location.search;
      location.assign(this.loginUrl + '?next=' + encodeURIComponent(next));
    }
  }

  function createClient(options) {
    return new SessionClient(options);
  }

  return {
    COOKIE,
    CSRF_HEADER,
    SAFE_METHODS,
    SessionClient,
    SessionError,
    createClient,
    readCookie
  };
});
