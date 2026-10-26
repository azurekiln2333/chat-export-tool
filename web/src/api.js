/**
 * HTTP helper for the reader API.
 *
 * Every request path is relative to the document base (`api/...`, never a
 * leading slash), so the app keeps working when a reverse proxy strips a
 * prefix. Requests use same-origin credentials so the HttpOnly session cookie
 * is sent, and mutating requests carry the CSRF token from `api/session`.
 */

const API_PREFIX = 'api/';

let csrfToken = '';

const ERROR_MESSAGES = {
  invalid_token: '访问口令不正确。',
  authentication_required: '登录已失效，请重新登录。',
  csrf_rejected: '页面凭证已失效，请刷新后重试。',
  origin_rejected: '页面地址与服务地址不一致，请检查代理配置。',
  host_rejected: '当前访问地址未获允许。',
  conflict: '该消息已被更新，请确认最新内容后重新保存。',
  not_found: '未找到请求的记录或文件。',
  invalid_argument: '输入内容不符合要求，请检查后重试。',
  invalid_account: '账号标识无效。',
  invalid_query: '查询条件无效，请调整后重试。',
  payload_too_large: '正文或备注过长，请缩短后重试。',
  body_too_large: '提交内容过大，请缩短后重试。',
  internal_error: '服务器处理失败，请稍后重试。',
  database_error: '无法读取聊天数据库，请检查服务状态。',
};

/** Raised for every non-2xx response with the server's error code when present. */
export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function setCsrfToken(value) {
  csrfToken = typeof value === 'string' ? value : '';
}

function queryString(params) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'boolean') {
      if (value) search.append(key, '1');
      continue;
    }
    search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

async function parseBody(response) {
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function request(method, path, { params, body } = {}) {
  const headers = { Accept: 'application/json' };
  const init = {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
  };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (method !== 'GET' && method !== 'HEAD' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  let response;
  try {
    response = await fetch(API_PREFIX + path + queryString(params), init);
  } catch (cause) {
    throw new ApiError('无法连接服务器，请检查网络或服务状态。', 0, 'network_error');
  }

  const payload = await parseBody(response);
  if (!response.ok) {
    const detail = (payload && payload.error) || {};
    throw new ApiError(
      ERROR_MESSAGES[detail.code] || `请求失败（HTTP ${response.status}）`,
      response.status,
      detail.code || '',
    );
  }
  return payload;
}

export const api = {
  get: (path, params) => request('GET', path, { params }),
  post: (path, body, params) => request('POST', path, { body, params }),
  patch: (path, body, params) => request('PATCH', path, { body, params }),
  delete: (path, body, params) => request('DELETE', path, { body, params }),
};
