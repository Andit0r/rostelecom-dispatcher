import type {
  Account,
  AccountList,
  AccountReset,
  AuthMe,
  AuthSession,
  CancelOrderResult,
  CreateOrderInput,
  CreateOrderResult,
  DemoHint,
  DayCreateResult,
  DayRebuildResult,
  DayResetResult,
  EmployeeMarkResult,
  EmployeeRoute,
  EmployeeStaffList,
  EmployeeStatus,
  EquipmentBulkInput,
  EquipmentBulkResult,
  EquipmentResult,
  EquipmentScope,
  ForecastModel,
  ForecastRisk,
  ErrorResponse,
  HistoryVersions,
  MarkKind,
  OrderCatalog,
  Plan,
  PlanStatus,
  RuntimeSettingsList,
  RuntimeSettingsResult,
  RuntimeSettingValue,
  StaffCutResult,
  StaffCutStrategy,
  StaffDirectoryList,
  StaffProfileInput,
  StaffProfileSaved,
  TrafficResult,
  UiDayOrders,
  UiReference,
} from '../types/api';

export class ApiFailure extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** 429: через сколько секунд можно повторить (заголовок Retry-After). */
    readonly retryAfter: number | null = null,
  ) {
    super(message);
  }
}

/*
 * Сессия. Веб живёт на httpOnly-cookie (fetch с credentials: 'include'); токен из ответа входа
 * держим только в памяти вкладки — как Bearer на случай, если браузер не сохранил cookie.
 * В localStorage токен не пишем.
 */
let bearer: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setAuthToken(token: string | null) {
  bearer = token;
}

/** Обработчик «сессия кончилась» (401 на защищённом запросе): показать экран входа. */
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

/** 401 на этих адресах — штатный ответ (не вошёл / неверный пароль), а не протухшая сессия. */
const AUTH_QUIET = /^\/ui\/auth\/(login|me|logout|change-password)\b/;

function retryAfterSec(resp: Response, message: string): number | null {
  const h = resp.headers.get('Retry-After');
  const n = h ? Number.parseInt(h, 10) : NaN;
  if (Number.isFinite(n) && n > 0) return n;
  // Заголовок может быть недоступен (CORS без expose_headers) — число есть и в тексте ошибки.
  const m = /(\d+)\s*с\b/.exec(message);
  return m ? Number(m[1]) : null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (bearer && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${bearer}`);
  let resp: Response;
  try {
    resp = await fetch(path, { ...init, headers, credentials: 'include' });
  } catch {
    throw new ApiFailure(0, 'NETWORK', 'Сервер планирования недоступен — запустите backend на порту 8080');
  }
  const text = await resp.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!resp.ok) {
    const err = (body as ErrorResponse | null)?.error;
    const code = err?.code ?? 'HTTP_' + resp.status;
    const msg = err?.message ?? resp.statusText;
    switch (resp.status) {
      case 401:
        if (!AUTH_QUIET.test(path)) {
          bearer = null;
          onUnauthorized?.();
          throw new ApiFailure(401, code, 'Сессия завершена, войдите снова');
        }
        throw new ApiFailure(401, code, msg || 'Неверный логин или пароль');
      case 403:
        throw new ApiFailure(403, code, 'Нет прав на это действие');
      case 428:
        throw new ApiFailure(428, code, 'Действие не подтверждено: повторите и подтвердите его');
      case 429: {
        const wait = retryAfterSec(resp, msg);
        throw new ApiFailure(
          429,
          code,
          wait ? `Слишком много попыток, подождите ${wait} с` : 'Слишком много попыток, подождите немного',
          wait,
        );
      }
    }
    throw new ApiFailure(resp.status, code, msg);
  }
  return body as T;
}

/*
 * «Недоступно для этого дня»: эндпоинт, один раз ответивший так (503 без БД, 409/404 — нет данных
 * дня), до смены дня или перезагрузки страницы больше не запрашиваем — иначе браузер пишет ошибку
 * в консоль на каждой версии плана. Повторный вызов сразу бросает ApiFailure с code UNAVAILABLE.
 */
const unavailableFor = new Map<string, string>();

export const UNAVAILABLE = 'UNAVAILABLE';

export function isUnavailable(e: unknown): boolean {
  return e instanceof ApiFailure && e.code === UNAVAILABLE;
}

async function oncePerDay<T>(name: string, date: string, statuses: number[], call: () => Promise<T>): Promise<T> {
  if (unavailableFor.get(name) === date) throw new ApiFailure(0, UNAVAILABLE, 'Недоступно для этого дня');
  try {
    const res = await call();
    unavailableFor.delete(name);
    return res;
  } catch (e) {
    if (e instanceof ApiFailure && statuses.includes(e.status)) {
      unavailableFor.set(name, date);
      throw new ApiFailure(e.status, UNAVAILABLE, e.message);
    }
    throw e;
  }
}

export type EmployeeMarkBody = {
  order_id?: string;
  kind: MarkKind | 'UNAVAILABLE';
  reason?: string;
  timestamp?: string;
  until?: string;
  /** Обязательно true для TRANSFER и UNAVAILABLE (иначе 428). */
  confirmed?: boolean;
};

export const api = {
  // ---- авторизация
  authMe: () => request<AuthMe>('/ui/auth/me'),
  authLogin: (login: string, password: string) => request<AuthSession>('/ui/auth/login', json({ login, password })),
  authLogout: () => request<{ ok: boolean }>('/ui/auth/logout', { method: 'POST' }),
  authChangePassword: (old_password: string, new_password: string) =>
    request<AuthSession>('/ui/auth/change-password', json({ old_password, new_password })),
  authDemoHint: () => request<DemoHint>('/ui/auth/demo-hint'),
  accountsList: () => request<AccountList>('/ui/directory/accounts'),
  accountReset: (staffId: string) =>
    request<AccountReset>(`/ui/directory/accounts/${encodeURIComponent(staffId)}/reset-password`, { method: 'POST' }),
  accountSetDisabled: (staffId: string, disabled: boolean) =>
    request<Account>(`/ui/directory/accounts/${encodeURIComponent(staffId)}/${disabled ? 'disable' : 'enable'}`, {
      method: 'POST',
    }),

  getPlan: (date: string) => request<Plan>(`/api/v1/days/${date}/plan`),
  getOrders: (date: string) => request<UiDayOrders>(`/ui/day/${date}/orders`),
  getReference: () => request<UiReference>('/ui/reference'),
  planStatus: () => request<PlanStatus>('/ui/plan/status'),
  /** «Создать день»: пустой день на дату (по умолчанию сегодня) и пустой план. */
  dayCreate: (body: { date?: string; now?: string } = {}) => request<DayCreateResult>('/ui/day/create', json(body)),
  /** «Пересобрать»: план активного дня с нуля по его нарядам, текущим режимам и рюкзакам. */
  dayRebuild: () => request<DayRebuildResult>('/ui/day/rebuild', { method: 'POST' }),
  dayReset: (withReference = false) =>
    request<DayResetResult>(`/ui/day/reset${withReference ? '?with_reference=true' : ''}`, { method: 'POST' }),
  scenarioStaffCut: (body: { date?: string; remove_count: number; strategy: StaffCutStrategy; seed?: number; budget_sec?: number }) =>
    request<StaffCutResult>('/ui/scenario/staff-cut', json(body)),
  scenarioEquipment: (body: { date?: string; mode: 'no_restock'; budget_sec?: number }) =>
    request<EquipmentResult>('/ui/scenario/equipment', json(body)),
  scenarioTraffic: (body: { date?: string; budget_sec?: number }) =>
    request<TrafficResult>('/ui/scenario/traffic', json(body)),
  employeeStatus: () => request<EmployeeStatus>('/ui/employee/status'),
  employeeStaff: () => request<EmployeeStaffList>('/ui/employee/staff'),
  employeeRoute: (staffId: string) => request<EmployeeRoute>(`/ui/employee/${encodeURIComponent(staffId)}/route`),
  employeeMark: (staffId: string, body: EmployeeMarkBody) =>
    request<EmployeeMarkResult>(`/ui/employee/${encodeURIComponent(staffId)}/marks`, json(body)),
  directoryStaff: () => request<StaffDirectoryList>('/ui/directory/staff'),
  directoryUpdate: (staffId: string, body: StaffProfileInput) =>
    request<StaffProfileSaved>(`/ui/directory/staff/${encodeURIComponent(staffId)}`, { ...json(body), method: 'PUT' }),
  directoryCreate: (body: StaffProfileInput) => request<StaffProfileSaved>('/ui/directory/staff', json(body)),
  directoryEquipmentReset: (staffId: string) =>
    request<StaffProfileSaved>(`/ui/directory/staff/${encodeURIComponent(staffId)}/equipment-override`, { method: 'DELETE' }),
  directoryEquipmentBulk: (body: EquipmentBulkInput) =>
    request<EquipmentBulkResult>('/ui/directory/staff/equipment/bulk', json(body)),
  directoryEquipmentBulkReset: (profile: EquipmentScope) =>
    request<EquipmentBulkResult>('/ui/directory/staff/equipment/bulk/reset', json({ profile })),
  ordersCatalog: (params: { q?: string; work_type?: string }) => {
    const q = new URLSearchParams();
    if (params.q) q.set('q', params.q);
    if (params.work_type) q.set('work_type', params.work_type);
    return request<OrderCatalog>(`/ui/orders/catalog?${q}`);
  },
  ordersCreate: (body: CreateOrderInput) => request<CreateOrderResult>('/ui/orders/create', json(body)),
  ordersCancel: (orderId: string) =>
    request<CancelOrderResult>(`/ui/orders/${encodeURIComponent(orderId)}/cancel`, { method: 'POST' }),
  /** Журнал БД: после 503 (нет БД) до смены дня не запрашивается. */
  historyVersions: (date: string) =>
    oncePerDay('history', date, [503], () => request<HistoryVersions>(`/ui/history/${date}/versions`)),
  forecastModel: () => request<ForecastModel>('/ui/forecast/model'),
  /** Риск опоздания при p80 по дню; после «недоступно» (409/404/503) до смены дня не запрашивается. */
  forecastRisk: (date: string) =>
    oncePerDay('risk', date, [404, 409, 503], () =>
      request<ForecastRisk>(`/ui/forecast/risk?${new URLSearchParams({ date })}`),
    ),
  // ---- режимы планировщика (только диспетчер)
  settingsGet: () => request<RuntimeSettingsList>('/ui/settings'),
  settingsPut: (changes: Record<string, RuntimeSettingValue>) =>
    request<RuntimeSettingsResult>('/ui/settings', { ...json(changes), method: 'PUT' }),
  settingsReset: () => request<RuntimeSettingsResult>('/ui/settings/reset', { method: 'POST' }),
};

function json(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
