import { useEffect, useState, type ReactNode } from 'react';
import { Badge, Button, Switch } from './ui';
import { dayLabel, EVENT_TYPE, hhmm, PLAN_TIME_HINT } from '../format';
import { POLL_MS, useLiveStatus, type Busy, type LiveStore } from '../hooks/usePlan';
import type { DemoStatus, DemoStepItem, Plan } from '../types/api';

interface Props {
  /** Состояние загруженного дня (дата, время плана). */
  demo: DemoStatus | null;
  plan: Plan | null;
  busy: Busy;
  /** Перечитать текущее состояние дня с сервера. */
  onRefresh: () => void;
  lastStep: DemoStepItem[];
  /** Меню «Окна» (показать/скрыть панели, сброс раскладки). */
  windows?: ReactNode;
  /** Открыть боковую панель «Данные» (справочник сотрудников). */
  onData?: () => void;
  dataOpen?: boolean;
  /** Открыть ту же панель на вкладке «Режимы» (только диспетчер). */
  onModes?: () => void;
  modesOpen?: boolean;
  /** Включённые нестандартные режимы — бейджи рядом с днём, клик открывает «Режимы». */
  activeModes?: string[];
  /** Выход из аккаунта (только при включённой авторизации). */
  onLogout?: () => void;
  /** Автообновление (опрос состояния плана) и его переключатель. */
  autoRefresh?: boolean;
  onAutoRefresh?: (on: boolean) => void;
  live?: LiveStore;
  /** Растёт при каждом автообновлении плана — подсветка «Версии плана». */
  flashNonce?: number;
  /** Настройка «Время плана идёт по часам» включена. */
  clockFollows?: boolean;
}

const hms = (t: number) => new Date(t).toLocaleTimeString('ru-RU', { hour12: false });

/** Переключатель «Автообновление» и тихая отметка «обновлено HH:MM:SS» (момент последнего изменения
 *  плана или ручного «Обновить данные», а не опроса) / «нет связи». Время проверки — в подсказке. */
function AutoRefresh({ on, onChange, live }: { on: boolean; onChange: (on: boolean) => void; live: LiveStore }) {
  const { syncedAt, checkedAt, offline } = useLiveStatus(live);
  const stamp = syncedAt != null ? `План обновлён в ${hms(syncedAt)}.` : 'План ещё не обновлялся.';
  const check = on && checkedAt != null ? ` Связь проверена в ${hms(checkedAt)}.` : '';
  return (
    <span
      className="topbar__auto"
      title={`Каждые ${POLL_MS / 1000} с проверять, не изменил ли план внешний клиент (симулятор, /api/v1). ${stamp}${check}`}
    >
      <Switch size="xs" label="Автообновление" checked={on} onChange={onChange} />
      {on && offline && <span className="topbar__stamp topbar__stamp--off">нет связи</span>}
      {on && !offline && syncedAt != null && <span className="topbar__stamp num">{hms(syncedAt)}</span>}
    </span>
  );
}

const EKB_MS = 5 * 3600 * 1000;
/** Текущее время Екатеринбурга (UTC+5) по часам браузера: { date: YYYY-MM-DD, hhmm }. */
const ekbNow = () => {
  const iso = new Date(Date.now() + EKB_MS).toISOString();
  return { date: iso.slice(0, 10), hhmm: iso.slice(11, 16) };
};
const toMinutes = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
const LAG_HINT = 'Время плана не идёт по часам (Режимы → Время плана идёт по часам)';

/** «Сейчас ЧЧ:ММ» только для сегодняшнего дня; тикает сам (раз в 30 с), остальную шапку не перерисовывает. */
function NowClock({ day, planTime, clockFollows }: { day?: string; planTime?: string | null; clockFollows?: boolean }) {
  const [now, setNow] = useState(ekbNow);
  useEffect(() => {
    const id = window.setInterval(() => setNow(ekbNow()), 30_000);
    return () => window.clearInterval(id);
  }, []);
  if (!day || day !== now.date) return null;
  const lag = planTime && !clockFollows ? toMinutes(now.hhmm) - toMinutes(hhmm(planTime)) > 10 : false;
  return (
    <span className="num" style={{ marginLeft: 8, opacity: 0.7, fontSize: '0.85em' }} title={lag ? LAG_HINT : undefined}>
      Сейчас {now.hhmm}
    </span>
  );
}

export function TopBar({
  demo,
  plan,
  busy,
  onRefresh,
  lastStep,
  windows,
  onData,
  dataOpen,
  onModes,
  modesOpen,
  activeModes = [],
  onLogout,
  autoRefresh,
  onAutoRefresh,
  live,
  flashNonce,
  clockFollows,
}: Props) {
  const last = lastStep[lastStep.length - 1];

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <img className="topbar__mark" src="/logo-mark.png" alt="" aria-hidden />
        <h1 className="topbar__title">Ростелеком Диспетчер</h1>
      </div>

      <dl className="topbar__facts">
        <div>
          <dt>День</dt>
          <dd className="topbar__day">
            {dayLabel(demo?.date)}
            {onModes &&
              activeModes.map((m) => (
                <button
                  key={m}
                  type="button"
                  className="topbar__mode"
                  title="Включён нестандартный режим планировщика — открыть «Режимы»"
                  onClick={onModes}
                >
                  <Badge size="2xs" variant="secondary" colorScheme="warning" label={m} />
                </button>
              ))}
          </dd>
        </div>
        <div title={PLAN_TIME_HINT}>
          <dt>Время плана</dt>
          <dd className="num">
            {hhmm(plan?.as_of ?? demo?.as_of)}
            <NowClock day={demo?.date} planTime={plan?.as_of ?? demo?.as_of} clockFollows={clockFollows} />
          </dd>
        </div>
        <div>
          <dt>Версия плана</dt>
          <dd key={flashNonce ?? 0} className={`num${flashNonce ? ' flash' : ''}`}>
            {plan ? plan.plan_version : '—'}
          </dd>
        </div>
      </dl>

      <div className="topbar__last" title={last?.message ?? undefined}>
        {last && (
          <>
            <span className="num">{hhmm(last.timestamp)}</span> {EVENT_TYPE[last.type] ?? last.type}
            {last.accepted ? (last.message ? `: ${last.message}` : '') : ': событие отклонено'}
          </>
        )}
      </div>

      <div className="topbar__actions">
        {busy === 'init' && <span className="topbar__loading">Загружаем план…</span>}
        {busy !== 'init' && (
          <Button
            size="s"
            variant="ghost"
            label={busy === 'refresh' ? 'Обновляем…' : 'Обновить данные'}
            title="Перечитать текущее состояние плана и нарядов с сервера"
            onClick={onRefresh}
            disabled={!!busy}
          />
        )}
        {live && onAutoRefresh && <AutoRefresh on={!!autoRefresh} onChange={onAutoRefresh} live={live} />}
        {onData && (
          <Button
            size="s"
            variant={dataOpen ? 'secondary' : 'ghost'}
            label="Данные"
            title="Справочник сотрудников: ФИО, телефоны, табельные номера"
            onClick={onData}
          />
        )}
        {onModes && (
          <Button
            size="s"
            variant={modesOpen ? 'secondary' : 'ghost'}
            label="Режимы"
            title="Режимы планировщика: пробки, совместные наряды, бюджет расчёта"
            onClick={onModes}
          />
        )}
        {windows}
        {onLogout && (
          <Button size="s" variant="ghost" colorScheme="neutral" label="Выйти" title="Выйти из аккаунта" onClick={onLogout} />
        )}
      </div>
    </header>
  );
}
