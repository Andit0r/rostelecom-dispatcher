// Общие вычисления над планом для карты, диаграммы Ганта и списков.
import type { Plan, Profile, ServiceCenter, Stop, UiOrder, UiReference } from './types/api';

/** Палитра маршрутов: --atmr-route-01..12 в theme/tokens.css, после 12-го цвета повтор. */
export const ROUTE_COLORS = 12;
const routeToken = (i: number) => `--atmr-route-${String((i % ROUTE_COLORS) + 1).padStart(2, '0')}`;
export const routeVar = (i: number) => `var(${routeToken(i)})`;
/** Canvas Leaflet не понимает var(--…): читаем значения из CSS. */
export function routeColorValues(): string[] {
  const cs = getComputedStyle(document.documentElement);
  return Array.from({ length: ROUTE_COLORS }, (_, i) => cs.getPropertyValue(routeToken(i)).trim());
}

/** Минуты от полуночи по локальному времени ISO (+05:00 берём как есть, как и hhmm). */
export const minutesOf = (iso?: string | null): number => {
  if (!iso) return NaN;
  return Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16)) + (Number(iso.slice(17, 19)) || 0) / 60;
};
/** "09:00" → 540 */
export const minutesOfHm = (hm?: string | null): number => {
  if (!hm) return NaN;
  const [h, m] = hm.split(':').map(Number);
  return h * 60 + (m || 0);
};

export const PROFILE_SHORT: Record<Profile, string> = { INSTALL: 'Инсталляция', LTP3: 'ЛТП-3' };

export interface StaffInfo {
  id: string;
  name: string;
  scId: string;
  profiles: Profile[];
  shift?: { start: string; end: string };
  /** Индекс цвета маршрута (стабилен, пока не меняется справочник). */
  color: number;
}

/** Справочник сотрудников + те, кто есть только в плане. Цвет — по порядку staff_id.
 *  names — «Фамилия И. О.» из справочника ФИО; без него остаётся display_name справочника. */
export function staffDirectory(
  reference: UiReference | null,
  plan: Plan | null,
  names?: ReadonlyMap<string, string>,
): Map<string, StaffInfo> {
  const res = new Map<string, StaffInfo>();
  for (const s of reference?.staff ?? []) {
    res.set(s.staff_id, {
      id: s.staff_id,
      name: names?.get(s.staff_id) || s.display_name || s.staff_id,
      scId: s.service_center_id,
      profiles: s.profiles,
      shift: { start: s.shift.start, end: s.shift.end },
      color: 0,
    });
  }
  for (const r of plan?.routes ?? []) {
    if (!res.has(r.staff_id)) {
      res.set(r.staff_id, { id: r.staff_id, name: names?.get(r.staff_id) ?? r.staff_id, scId: r.service_center_id, profiles: [], color: 0 });
    }
  }
  [...res.keys()].sort().forEach((id, i) => (res.get(id)!.color = i));
  return res;
}

export type LatLon = [number, number];

/** Координаты точек плана: дома (building_id) и СЦ (service_center_id). */
export function locationIndex(orders: UiOrder[], scs: ServiceCenter[]): Map<string, LatLon> {
  const m = new Map<string, LatLon>();
  for (const o of orders) m.set(o.building_id, [o.lat, o.lon]);
  for (const sc of scs) m.set(sc.service_center_id, [sc.lat, sc.lon]);
  return m;
}

export function stopLatLon(
  s: Stop,
  scId: string,
  loc: Map<string, LatLon>,
  ordersById: Map<string, UiOrder>,
): LatLon | undefined {
  if (s.type === 'VISIT' && s.order_id) {
    const o = ordersById.get(s.order_id);
    if (o) return [o.lat, o.lon];
  }
  return loc.get(s.location_id) ?? (s.type !== 'VISIT' && s.type !== 'BREAK' ? loc.get(scId) : undefined);
}

/** Совместные наряды (JOINT_ORDERS=on): визит напарника идёт в плане как ORD-…-B. */
export const PARTNER_SUFFIX = '-B';
/** Основной наряд для визита напарника, иначе null. */
export const jointMainOf = (orderId?: string | null): string | null =>
  orderId && orderId.endsWith(PARTNER_SUFFIX) ? orderId.slice(0, -PARTNER_SUFFIX.length) : null;

/** Пары плана: основной наряд → { основной сотрудник, напарник }. */
export function jointPairs(plan: Plan | null): Map<string, { main?: string; partner?: string }> {
  const res = new Map<string, { main?: string; partner?: string }>();
  const visits = new Map<string, string>();
  for (const r of plan?.routes ?? []) for (const s of r.stops) if (s.type === 'VISIT' && s.order_id) visits.set(s.order_id, r.staff_id);
  for (const [id, staff] of visits) {
    const main = jointMainOf(id);
    if (main) res.set(main, { main: visits.get(main), partner: staff });
  }
  return res;
}

export type Source = 'table' | 'late' | 'unassigned' | 'scenarios' | 'map' | 'gantt' | 'stats';

/** Метрики одной версии плана для графика динамики (как в /ui/history/{date}/versions). */
export interface MetricPoint {
  date: string;
  version: number;
  asOf: string | null;
  visits: number;
  unassigned: number;
  lateRisk: number;
  travelSec: number;
}

export function planMetrics(plan: Plan): MetricPoint {
  const sm = plan.summary;
  return {
    date: plan.date,
    version: plan.plan_version,
    asOf: plan.as_of,
    // визит напарника (ORD-…-B) не считается: совместная пара — один наряд
    visits:
      sm?.visits_planned ??
      plan.routes.reduce((n, r) => n + r.stops.filter((s) => s.type === 'VISIT' && !jointMainOf(s.order_id)).length, 0),
    unassigned: sm?.unassigned ?? plan.unassigned.length,
    lateRisk: plan.alerts.filter((a) => a.type === 'LATE_RISK' && a.status === 'ACTIVE').length,
    travelSec: sm?.travel_sec_total ?? plan.routes.reduce((n, r) => n + (r.totals?.travel_sec ?? 0), 0),
  };
}

/** Накопитель метрик сессии: одна точка на версию (повтор версии заменяет точку), не больше limit. */
export function pushMetric(list: MetricPoint[], plan: Plan, limit = 200): MetricPoint[] {
  const p = planMetrics(plan);
  const rest = list.filter((x) => x.date === p.date && x.version !== p.version);
  rest.push(p);
  rest.sort((a, b) => a.version - b.version);
  return rest.length > limit ? rest.slice(rest.length - limit) : rest;
}
export interface Selection {
  id: string;
  source: Source;
  /** Растёт на каждый явный выбор: повторный клик по тому же объекту снова центрирует/прокручивает. */
  nonce: number;
  /** Для сотрудника: вписать его маршрут в карту. */
  fit?: boolean;
}
export type RouteMode = 'all' | 'selected' | 'none';
