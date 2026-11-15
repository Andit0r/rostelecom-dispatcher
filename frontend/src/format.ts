import type { AlertAction, UiOrderStatus, UnassignedReason, WorkTypeCode } from './types/api';
import type { BadgeScheme } from './components/ui';

/** Время HH:MM в часовом поясе сервера (+05:00, Екатеринбург) — берём из ISO как есть. */
export const hhmm = (iso?: string | null) => (iso ? iso.slice(11, 16) : '—');

/** Подсказка к «Времени плана» (as_of): не системные часы, а момент, на который построен план. */
export const PLAN_TIME_HINT = 'Время, на которое построен план; продвигается событиями и отметками сотрудников';

const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
export const dayLabel = (date?: string) => {
  if (!date) return '—';
  const [y, m, d] = date.split('-').map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
};

export const duration = (sec?: number) => {
  if (sec == null) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
};

export const STATUS: Record<UiOrderStatus, { label: string; scheme: BadgeScheme }> = {
  ROUTED: { label: 'В маршруте', scheme: 'success' },
  UNASSIGNED: { label: 'Не распределён', scheme: 'error' },
  PGP: { label: 'ПГП', scheme: 'status-01' },
  CANCELLED: { label: 'Отменён', scheme: 'neutral' },
  UNKNOWN: { label: 'Нет данных', scheme: 'neutral' },
};

export const WORK_TYPE_SHORT: Record<WorkTypeCode, string> = {
  INST_PON_MKD: 'Инсталляция, МКД PON',
  INST_FTTB_MKD: 'Инсталляция, МКД FTTB',
  INST_PRIVATE_VLS: 'Инсталляция, частный ВЛС',
  ADD_SERVICE: 'Доподключение',
  SURVEY_PRIVATE: 'Обследование',
  REPAIR: 'Неисправность',
  DEGRADATION: 'Деградация',
  DISMANTLE: 'Демонтаж',
};

export const REASON: Record<UnassignedReason, string> = {
  NO_QUALIFIED_STAFF: 'Нет подходящего сотрудника',
  NO_SHIFT_TIME: 'Не хватает времени смены',
  SLOT_UNREACHABLE: 'Не успеть в слот',
  NO_EQUIPMENT: 'Нет оборудования',
  STAFF_UNAVAILABLE: 'Исполнитель выбыл',
  OTHER: 'Другое',
};

export const ACTION: Record<AlertAction | 'ESCALATE', string> = {
  REASSIGN: 'Передать другому',
  RESCHEDULE: 'Перенести визит',
  TRANSFER_TO_STATION: 'Передать на станцию',
  ESCALATE: 'Решение диспетчера',
  NONE: 'Без действия',
};

export const EVENT_TYPE: Record<string, string> = {
  NEW_ORDER: 'Новый наряд',
  ORDER_CANCELLED: 'Отмена',
  VISIT_DELAYED: 'Визит затянулся',
  ORDER_TRANSFERRED: 'Передан на станцию',
  STAFF_UNAVAILABLE: 'Сотрудник выбыл',
  TIME_TICK: 'Ход времени',
};

export const window2 = (w?: { start: string; end: string } | null) => (w ? `${hhmm(w.start)}–${hhmm(w.end)}` : '—');

/** Дата слота, если она не совпадает с днём плана (перенос на другой день). */
export const windowWithDay = (w: { start: string; end: string } | null | undefined, day?: string) => {
  if (!w) return '—';
  const d = w.start.slice(0, 10);
  if (day && d !== day) return `${Number(d.slice(8, 10))}.${d.slice(5, 7)} ${window2(w)}`;
  return window2(w);
};
