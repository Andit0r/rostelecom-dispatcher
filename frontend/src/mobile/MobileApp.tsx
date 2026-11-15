/*
 * Мобильный экран выездного сотрудника (#/m, #/m/ST-001): маршрут на день, навигация
 * к следующему адресу и отметки «позвонил → прибыл → завершил».
 * Данные — /ui/employee/*; план опрашивается раз в POLL_MS, состояние меняется только
 * при изменении ответа (без мерцания). Время — время плана (as_of).
 *
 * Сотрудник (роль EMPLOYEE) видит только свой маршрут; диспетчер может открыть любого.
 * Защита от промахов: одна главная кнопка следующего шага; «Передать» и «Недоступен» — в меню
 * «Ещё» в шапке; «Завершил», «Передать», «Недоступен» подтверждаются удержанием; любая отметка
 * уходит на сервер через SEND_DELAY_MS, до этого её можно отменить; «Прибыл» сверяется с геолокацией.
 */
import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { api, ApiFailure } from '../api/client';
import { hhmm, PLAN_TIME_HINT } from '../format';
import type { EmployeeMarkResult, EmployeeRoute, EmployeeStaffItem, EmployeeStop, MarkKind } from '../types/api';
import { StaffDirectoryContext, useStaffDirectory, useStaffDirectoryContext, useStaffName } from '../hooks/useStaffDirectory';
import { canLogout, useAuth } from '../auth/AuthContext';
import { currentFix, distanceText, haversineM } from './geo';
import './mobile.css';

const POLL_MS = 5000;
const SEND_DELAY_MS = 8000;
const HOLD_MS = 1000;
const ARRIVE_RADIUS_M = 300;
const STORAGE_KEY = 'rtk.mobile.staff';
const NOT_READY = new Set(['PLAN_NOT_BUILT', 'DAY_NOT_FOUND', 'REFERENCE_NOT_LOADED', 'PLAN_NOT_FOUND']);
const NOT_READY_TEXT = 'План дня ещё не построен. Маршрут появится сам, когда диспетчер загрузит день.';

const STEPS: { kind: MarkKind; label: string; done: string }[] = [
  { kind: 'CALLED', label: 'Позвонил абоненту', done: 'Звонок' },
  { kind: 'ARRIVED', label: 'Прибыл на адрес', done: 'Прибытие' },
  { kind: 'DONE', label: 'Завершил работу', done: 'Завершено' },
];

const TRANSFER_REASONS = [
  { value: 'STATION_EQUIPMENT', label: 'Станционное оборудование', to: 'службу станционного оборудования' },
  { value: 'TRUNK_CABLE', label: 'Магистральный кабель', to: 'службу магистральных кабелей' },
];
const UNAVAILABLE_REASONS = [
  { value: 'VEHICLE_BREAKDOWN', label: 'Поломка машины', to: '' },
  { value: 'ILLNESS', label: 'Болезнь', to: '' },
  { value: 'OTHER', label: 'Другая причина', to: '' },
];
const EVENT_LABEL: Record<string, string> = {
  VISIT_DELAYED: 'задержка визита',
  ORDER_TRANSFERRED: 'передача наряда',
  STAFF_UNAVAILABLE: 'недоступность сотрудника',
};
const KIND_LABEL: Record<string, string> = {
  CALLED: 'Позвонил',
  ARRIVED: 'Прибыл',
  DONE: 'Завершил',
  TRANSFER: 'Передать наряд',
  UNAVAILABLE: 'Недоступен',
};

const PROFILE: Record<string, string> = { INSTALL: 'Инсталлятор', LTP3: 'ЗЛТП' };

// ---------------------------------------------------------------- helpers

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(id: string | null) {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* хранилище недоступно — выбор просто не запомнится */
  }
}

function staffFromHash(): string | null {
  const m = /^#\/m\/(ST-\d{3})\b/.exec(window.location.hash);
  return m ? m[1] : null;
}

function setHash(id: string | null) {
  const next = id ? `#/m/${id}` : '#/m';
  if (window.location.hash !== next) window.history.replaceState(null, '', next);
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const minutesBetween = (a: string, b: string) => Math.round((Date.parse(a) - Date.parse(b)) / 60000);

function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
const mins = (n: number) => `${n} ${plural(n, 'минуту', 'минуты', 'минут')}`;

/** Ссылки навигации из координат: Яндекс.Карты и 2ГИС строят маршрут от текущего места. */
function navLinks(lat: number, lon: number, label: string) {
  return [
    { id: 'yandex', title: 'Яндекс.Карты', href: `https://yandex.ru/maps/?rtext=~${lat},${lon}&rtt=auto` },
    { id: '2gis', title: '2ГИС', href: `https://2gis.ru/routeSearch/rsType/car/to/${lon},${lat}` },
    {
      id: 'geo',
      title: 'Системная навигация',
      href: `geo:${lat},${lon}?q=${lat},${lon}(${encodeURIComponent(label)})`,
    },
  ];
}

/** Смена на сегодня, если день загружен (GET /ui/employee/status); null — плана ещё нет. */
async function loadShift(): Promise<EmployeeStaffItem[] | null> {
  const st = await api.employeeStatus();
  if (!st.loaded) return null;
  try {
    return (await api.employeeStaff()).staff;
  } catch (e) {
    if (e instanceof ApiFailure && NOT_READY.has(e.code)) return null;
    throw e;
  }
}

/** Короткий итог отметки для сотрудника: что отправлено в план и пересчитан ли маршрут. */
function resultText(r: EmployeeMarkResult): string {
  const what = KIND_LABEL[r.kind] ?? r.kind;
  const where = r.order_id ? ` по ${r.order_id}` : '';
  if (r.duplicate) return `«${what}»${where} уже была отмечена, план не менялся.`;
  if (!r.event_sent || !r.event) return `Отправлено: «${what}»${where}. План не менялся.`;
  const ev = EVENT_LABEL[r.event.type] ?? r.event.type;
  return `Отправлено: «${what}»${where} (${ev}). План обновлён до версии ${r.plan_version}${
    r.route_changed ? ', ваш маршрут пересчитан.' : ', ваш маршрут не изменился.'
  }`;
}

function stopTitle(s: EmployeeStop, scName?: string) {
  switch (s.type) {
    case 'VISIT':
      return s.order?.address ?? s.location?.address ?? 'Адрес не найден';
    case 'BREAK':
      return 'Перерыв';
    case 'RESTOCK':
      return `Заезд в ${s.location?.name ?? scName ?? 'СЦ'} за оборудованием`;
    case 'DEPOT_START':
      return `Начало смены, ${s.location?.name ?? scName ?? 'СЦ'}`;
    case 'DEPOT_END':
      return `Возврат в ${s.location?.name ?? scName ?? 'СЦ'}`;
  }
}

function equipmentText(eq: { ONT: number; ROUTER: number; STB: number } | undefined) {
  if (!eq) return null;
  const parts = (['ONT', 'ROUTER', 'STB'] as const).filter((k) => eq[k] > 0).map((k) => `${k} × ${eq[k]}`);
  return parts.length ? parts : null;
}

// ---------------------------------------------------------------- удержание для подтверждения

/**
 * Кнопка «удерживайте ~1 с»: заливка показывает прогресс, отпускание раньше — отмена.
 * Работает пальцем, мышью и с клавиатуры (Enter/Пробел держать).
 */
function HoldButton({
  label,
  summary,
  onConfirm,
  disabled,
  danger,
}: {
  label: string;
  /** Что произойдёт — показывается над кнопкой и читается скринридером. */
  summary: string;
  onConfirm: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  const [p, setP] = useState(0);
  const [short, setShort] = useState(false);
  const raf = useRef<number | null>(null);
  const t0 = useRef<number | null>(null);
  const shortTimer = useRef<number | undefined>(undefined);

  const stop = useCallback((released: boolean) => {
    if (raf.current != null) cancelAnimationFrame(raf.current);
    raf.current = null;
    if (released && t0.current != null) {
      setShort(true);
      window.clearTimeout(shortTimer.current);
      shortTimer.current = window.setTimeout(() => setShort(false), 2500);
    }
    t0.current = null;
    setP(0);
  }, []);

  useEffect(
    () => () => {
      if (raf.current != null) cancelAnimationFrame(raf.current);
      window.clearTimeout(shortTimer.current);
    },
    [],
  );

  const begin = () => {
    if (disabled || t0.current != null) return;
    setShort(false);
    t0.current = performance.now();
    const tick = (t: number) => {
      if (t0.current == null) return;
      const v = Math.min(1, (t - t0.current) / HOLD_MS);
      setP(v);
      if (v >= 1) {
        t0.current = null;
        raf.current = null;
        setP(0);
        navigator.vibrate?.(25);
        onConfirm();
      } else raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
  };

  const id = useId();
  const face = (
    <span className="m-hold__face">
      <span className="m-hold__label">{p > 0 ? 'Держите…' : label}</span>
      <span className="m-hold__cap">{p > 0 ? 'отпустите раньше — отмена' : 'удерживайте 1 секунду'}</span>
    </span>
  );
  return (
    <div className="m-hold-wrap">
      <p className="m-hold__summary" id={id}>
        {summary}
      </p>
      <button
        type="button"
        className={'m-hold' + (danger ? ' m-hold--danger' : '')}
        style={{ '--p': p } as CSSProperties}
        disabled={disabled}
        aria-describedby={id}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          e.currentTarget.setPointerCapture?.(e.pointerId);
          begin();
        }}
        onPointerUp={() => stop(true)}
        onPointerCancel={() => stop(false)}
        onLostPointerCapture={() => t0.current != null && stop(true)}
        onKeyDown={(e) => {
          if ((e.key === 'Enter' || e.key === ' ') && !e.repeat) {
            e.preventDefault();
            begin();
          }
        }}
        onKeyUp={(e) => {
          if (e.key === 'Enter' || e.key === ' ') stop(true);
        }}
        onBlur={() => stop(false)}
        onContextMenu={(e) => e.preventDefault()}
      >
        {face}
        {/* Залитый слой с тем же текстом, обрезанный по прогрессу: текст контрастен на обеих частях. */}
        <span className="m-hold__fill" aria-hidden>
          {face}
        </span>
      </button>
      {short && (
        <p className="m-hold__short" role="status">
          Отпустили раньше — ничего не отправлено
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- нижний лист

function Sheet({ title, hint, children, onClose }: { title: string; hint?: ReactNode; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="m-sheet" role="dialog" aria-modal="true" aria-label={title} onClick={onClose}>
      <div className="m-sheet__panel" onClick={(e) => e.stopPropagation()}>
        <h2 className="m-sheet__title">{title}</h2>
        {hint && <p className="m-sheet__hint">{hint}</p>}
        {children}
      </div>
    </div>
  );
}

/** Выбор причины и подтверждение удержанием (передача наряда, недоступность). */
function ReasonSheet({
  title,
  hint,
  options,
  holdLabel,
  summary,
  onConfirm,
  onClose,
}: {
  title: string;
  hint: string;
  options: { value: string; label: string; to: string }[];
  holdLabel: string;
  summary: (o: { value: string; label: string; to: string }) => string;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState<string | null>(null);
  const opt = options.find((o) => o.value === reason) ?? null;
  return (
    <Sheet title={title} hint={hint} onClose={onClose}>
      <div className="m-sheet__opts" role="radiogroup" aria-label="Причина">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={reason === o.value}
            className={'m-sheet__opt' + (reason === o.value ? ' is-on' : '')}
            onClick={() => setReason(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
      {opt ? (
        <HoldButton label={holdLabel} summary={summary(opt)} onConfirm={() => onConfirm(opt.value)} danger />
      ) : (
        <p className="m-sheet__wait">Выберите причину</p>
      )}
      <button type="button" className="m-btn m-btn--ghost" onClick={onClose}>
        Отмена
      </button>
    </Sheet>
  );
}

// ---------------------------------------------------------------- data hook

function useEmployeeRoute(staffId: string | null) {
  const [route, setRoute] = useState<EmployeeRoute | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastJson = useRef('');

  const load = useCallback(async () => {
    if (!staffId) return;
    try {
      const r = await api.employeeRoute(staffId);
      const json = JSON.stringify(r);
      if (json !== lastJson.current) {
        lastJson.current = json;
        setRoute(r);
      }
      setError(null);
    } catch (e) {
      setError(e instanceof ApiFailure && NOT_READY.has(e.code) ? NOT_READY_TEXT : errText(e));
    }
  }, [staffId]);

  useEffect(() => {
    lastJson.current = '';
    setRoute(null);
    setError(null);
    if (!staffId) return;
    void load();
    // Опрос продолжается и при ошибке: как только диспетчер загрузит день, маршрут появится сам.
    const t = window.setInterval(() => {
      if (!document.hidden) void load();
    }, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(t);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [staffId, load]);

  return { route, error, reload: load };
}

// ---------------------------------------------------------------- screen

export default function MobileApp() {
  const auth = useAuth();
  const isEmployee = auth?.user.role === 'EMPLOYEE';
  // Сотруднику справочник диспетчера недоступен (403): ФИО берём из маршрута.
  const directory = useStaffDirectory(!isEmployee);
  return (
    <StaffDirectoryContext.Provider value={directory}>
      <MobileScreen />
    </StaffDirectoryContext.Provider>
  );
}

function MobileScreen() {
  const auth = useAuth();
  const ownId = auth?.user.role === 'EMPLOYEE' ? auth.user.staff_id : null;
  const isEmployee = auth?.user.role === 'EMPLOYEE';
  const [chosen, setChosen] = useState<string | null>(() => staffFromHash() ?? readStored());
  const staffId = isEmployee ? ownId : chosen;
  const [picking, setPicking] = useState(false);
  const [staffList, setStaffList] = useState<EmployeeStaffItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const { route, error, reload } = useEmployeeRoute(picking ? null : staffId);

  useEffect(() => {
    document.title = 'Ростелеком: маршрут сотрудника';
  }, []);

  // Синхронизация с адресом (только диспетчер): #/m/ST-xxx выбирает сотрудника.
  useEffect(() => {
    if (isEmployee) return;
    const on = () => {
      const id = staffFromHash();
      if (id) {
        setChosen(id);
        setPicking(false);
      }
    };
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, [isEmployee]);

  useEffect(() => {
    if (staffId && !picking) setHash(staffId);
  }, [staffId, picking]);

  const needList = !isEmployee && (picking || !staffId);
  useEffect(() => {
    if (!needList) return;
    let alive = true;
    let timer: number | undefined;
    const run = () => {
      loadShift()
        .then((list) => {
          if (!alive) return;
          setListError(list ? null : NOT_READY_TEXT);
          if (list) setStaffList(list);
          else timer = window.setTimeout(run, POLL_MS);
        })
        .catch((e) => alive && setListError(errText(e)));
    };
    setListError(null);
    run();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [needList]);

  const choose = (id: string) => {
    writeStored(id);
    setChosen(id);
    setPicking(false);
  };

  if (isEmployee && !staffId) {
    return (
      <div className="m">
        <div className="m-empty">
          <p className="m-empty__title">Аккаунт не связан с сотрудником</p>
          <p className="m-empty__text">Обратитесь к диспетчеру.</p>
          <LogoutLink />
        </div>
      </div>
    );
  }

  if (needList) {
    return (
      <StaffPicker
        list={staffList}
        error={listError}
        current={staffId}
        onPick={choose}
        onCancel={staffId ? () => setPicking(false) : undefined}
      />
    );
  }

  return (
    <div className="m">
      {route ? (
        <RouteScreen route={route} onSwitch={isEmployee ? undefined : () => setPicking(true)} onChanged={reload} />
      ) : (
        <div className="m-empty">
          {error ? (
            <>
              <p className="m-empty__title">Маршрут пока не загрузился</p>
              <p className="m-empty__text">{error}</p>
              {!isEmployee && (
                <button type="button" className="m-btn m-btn--ghost" onClick={() => setPicking(true)}>
                  Выбрать другого сотрудника
                </button>
              )}
              <LogoutLink />
            </>
          ) : (
            <p className="m-empty__text">Загружаем маршрут…</p>
          )}
        </div>
      )}
    </div>
  );
}

function LogoutLink() {
  const auth = useAuth();
  if (!canLogout(auth)) return null;
  return (
    <button type="button" className="m-link" onClick={() => void auth!.logout()}>
      Выйти
    </button>
  );
}

// ---------------------------------------------------------------- staff picker (диспетчер)

function StaffPicker({
  list,
  error,
  current,
  onPick,
  onCancel,
}: {
  list: EmployeeStaffItem[] | null;
  error: string | null;
  current: string | null;
  onPick: (id: string) => void;
  onCancel?: () => void;
}) {
  const [query, setQuery] = useState('');
  const staffName = useStaffName();
  const profiles = useStaffDirectoryContext()?.items;
  const byId = new Map((profiles ?? []).map((p) => [p.staff_id, p]));
  const q = query.trim().toLowerCase();
  const items = (list ?? []).filter((s) => {
    if (!q) return true;
    const p = byId.get(s.staff_id);
    return [s.staff_id, s.display_name, p?.full_name, p?.personnel_number].some((v) => (v ?? '').toLowerCase().includes(q));
  });
  return (
    <div className="m">
      <header className="m-pick__head">
        <img className="m-pick__logo" src="/logo-mark.png" alt="" aria-hidden />
        <div className="m-pick__titles">
          <h1 className="m-pick__title">Чей маршрут открыть?</h1>
          <p className="m-pick__sub">Просмотр от имени диспетчера</p>
        </div>
        {onCancel && (
          <button type="button" className="m-link" onClick={onCancel}>
            Отмена
          </button>
        )}
      </header>
      <input
        className="m-search"
        type="search"
        inputMode="search"
        placeholder="Имя или табельный номер"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Поиск сотрудника"
      />
      {error && <p className="m-alert m-alert--error">{error}</p>}
      {!list && !error && <p className="m-empty__text">Загружаем список смены…</p>}
      <ul className="m-staff">
        {items.map((s) => (
          <li key={s.staff_id}>
            <button
              type="button"
              className={'m-staff__item' + (s.staff_id === current ? ' is-current' : '')}
              onClick={() => onPick(s.staff_id)}
            >
              <span className="m-staff__name">{staffName(s.staff_id, s.display_name)}</span>
              <span className="m-staff__meta">
                {s.staff_id}, {s.profiles.map((p) => PROFILE[p] ?? p).join(' и ')}, {s.service_center_name ?? s.service_center_id}
              </span>
              <span className="m-staff__count num">
                {s.route_status === 'UNAVAILABLE'
                  ? 'Снят со смены'
                  : s.visits
                    ? `${s.visits_done}/${s.visits}`
                    : 'Нет визитов'}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {list && !items.length && <p className="m-empty__text">Никого не нашли</p>}
    </div>
  );
}

// ---------------------------------------------------------------- route screen

interface Pending {
  kind: MarkKind | 'UNAVAILABLE';
  orderId?: string;
  reason?: string;
  sendAt: number;
}

type SheetState =
  | null
  | { t: 'more' }
  | { t: 'transfer'; orderId: string }
  | { t: 'unavailable' }
  | { t: 'geo'; orderId: string; distance: number }
  | { t: 'logout' };

type Toast = { tone: 'ok' | 'error'; text: string } | null;

function RouteScreen({
  route,
  onSwitch,
  onChanged,
}: {
  route: EmployeeRoute;
  /** Только диспетчер: выбрать другого сотрудника. */
  onSwitch?: () => void;
  onChanged: () => Promise<void>;
}) {
  const { staff, progress } = route;
  const staffName = useStaffName();
  const auth = useAuth();
  const [notice, setNotice] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast>(null);
  const [sheet, setSheet] = useState<SheetState>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [sending, setSending] = useState(false);
  const [locating, setLocating] = useState(false);
  const [, setTick] = useState(0);
  const staffId = staff.staff_id;

  const next = route.next ? route.stops.find((s) => s.seq === route.next!.seq) ?? null : null;
  const depotEnd = route.stops.find((s) => s.type === 'DEPOT_END');
  const pct = progress.visits_total ? Math.round((progress.visits_done / progress.visits_total) * 100) : 0;
  const canUnavailable = route.allowed_actions.includes('UNAVAILABLE');
  const canTransfer = !!next?.order_id && next.allowed_actions.includes('TRANSFER');
  const locked = !!pending || sending || locating;

  const send = useCallback(
    async (p: Pending) => {
      setSending(true);
      try {
        const dangerous = p.kind === 'TRANSFER' || p.kind === 'UNAVAILABLE';
        const r = await api.employeeMark(staffId, {
          kind: p.kind,
          order_id: p.orderId,
          reason: p.reason,
          ...(dangerous ? { confirmed: true } : {}),
        });
        const text = resultText(r);
        setNotice(text);
        setToast({ tone: 'ok', text });
        await onChanged();
      } catch (e) {
        setToast({ tone: 'error', text: `Не отправлено: ${errText(e)}` });
      } finally {
        setSending(false);
      }
    },
    [staffId, onChanged],
  );

  // Отложенная отправка: таймер на sendAt, тик раз в 250 мс для обратного отсчёта.
  useEffect(() => {
    if (!pending) return;
    const fire = window.setTimeout(() => {
      setPending(null);
      void send(pending);
    }, Math.max(0, pending.sendAt - Date.now()));
    const tick = window.setInterval(() => setTick((n) => n + 1), 250);
    return () => {
      window.clearTimeout(fire);
      window.clearInterval(tick);
    };
  }, [pending, send]);

  // Итог гаснет сам; ошибка — тоже, но позже.
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), toast.tone === 'ok' ? 8000 : 12000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const schedule = (p: Omit<Pending, 'sendAt'>) => {
    setSheet(null);
    setToast(null);
    setPending({ ...p, sendAt: Date.now() + SEND_DELAY_MS });
  };

  /** «Прибыл»: мягкая проверка места. Нет геолокации или отказ — не мешаем. */
  const arrive = async (stop: EmployeeStop) => {
    if (!stop.order_id || locked) return;
    const lat = stop.order?.lat ?? stop.location?.lat;
    const lon = stop.order?.lon ?? stop.location?.lon;
    if (lat == null || lon == null) return schedule({ kind: 'ARRIVED', orderId: stop.order_id });
    setLocating(true);
    const fix = await currentFix();
    setLocating(false);
    const d = fix ? haversineM(fix.lat, fix.lon, lat, lon) : 0;
    if (fix && d > ARRIVE_RADIUS_M) setSheet({ t: 'geo', orderId: stop.order_id, distance: d });
    else schedule({ kind: 'ARRIVED', orderId: stop.order_id });
  };

  const left = pending ? Math.max(0, Math.ceil((pending.sendAt - Date.now()) / 1000)) : 0;
  const closeSheet = useCallback(() => setSheet(null), []);

  return (
    <>
      <header className="m-head">
        <div className="m-head__row">
          <div className="m-head__who">
            <h1 className="m-head__name">{staffName(staff.staff_id, staff.display_name)}</h1>
            <p className="m-head__meta">
              {staff.service_center?.name ?? '—'}, смена {staff.shift.start}–{staff.shift.end}
            </p>
          </div>
          <div className="m-head__actions">
            <button
              type="button"
              className="m-head__btn"
              aria-haspopup="dialog"
              onClick={() => setSheet({ t: 'more' })}
            >
              Ещё
            </button>
            {canLogout(auth) && (
              <button type="button" className="m-head__btn m-head__btn--quiet" onClick={() => setSheet({ t: 'logout' })}>
                Выйти
              </button>
            )}
          </div>
        </div>
        <div className="m-head__progress">
          <div className="m-head__progress-text">
            <span>
              Выполнено <b className="num">{progress.visits_done}</b> из <b className="num">{progress.visits_total}</b>
            </span>
            <span className="m-head__clock" title={PLAN_TIME_HINT}>
              Время плана <b className="num">{hhmm(route.as_of)}</b>
            </span>
          </div>
          <div className="m-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div className="m-bar__fill" style={{ width: `${pct}%` }} />
          </div>
        </div>
      </header>

      {notice && (
        <p className="m-alert m-alert--ok" role="status">
          {notice}
        </p>
      )}

      {route.route_status === 'UNAVAILABLE' && (
        <p className="m-alert m-alert--error">
          Вы сняты со смены
          {route.unavailable_from ? ` с ${hhmm(route.unavailable_from)}` : ''}
          {route.unavailable_until ? ` до ${hhmm(route.unavailable_until)}` : ''}. Оставшиеся наряды передаются другим
          сотрудникам.
        </p>
      )}

      {next ? (
        <NextCard key={next.order_id} stop={next} route={route}>
          <MainAction
            stop={next}
            pending={pending}
            sending={sending}
            locating={locating}
            left={left}
            onCall={() => next.order_id && schedule({ kind: 'CALLED', orderId: next.order_id })}
            onArrive={() => void arrive(next)}
            onDone={() => next.order_id && schedule({ kind: 'DONE', orderId: next.order_id })}
          />
        </NextCard>
      ) : (
        <section className="m-card m-card--calm">
          <h2 className="m-card__title">
            {progress.visits_total ? 'Все визиты на сегодня выполнены' : 'На сегодня визитов нет'}
          </h2>
          {depotEnd && (
            <p className="m-card__text">
              Возврат в {depotEnd.location?.name ?? 'СЦ'} к <span className="num">{hhmm(depotEnd.arrival)}</span>
            </p>
          )}
        </section>
      )}

      <section className="m-day" aria-label="Маршрут на день">
        <h2 className="m-day__title">Маршрут на день</h2>
        <ol className="m-day__list">
          {route.stops.map((s) => (
            <StopRow key={s.seq} stop={s} isNext={s.seq === next?.seq} scName={staff.service_center?.name} />
          ))}
        </ol>
      </section>
      <p className="m-foot">
        План версии <span className="num">{route.plan_version}</span>, обновляется автоматически
      </p>

      {(pending || sending || toast) && (
        <div className={'m-toast' + (toast && !pending && !sending ? ` m-toast--${toast.tone}` : '')} role="status">
          {pending ? (
            <>
              <span className="m-toast__text">
                «{KIND_LABEL[pending.kind]}»{pending.orderId ? ` по ${pending.orderId}` : ''}: отправим через{' '}
                <b className="num">{left}</b> с
              </span>
              <button type="button" className="m-toast__btn" onClick={() => setPending(null)}>
                Отменить
              </button>
            </>
          ) : sending ? (
            <span className="m-toast__text">Отправляем отметку…</span>
          ) : (
            <>
              <span className="m-toast__text">{toast!.text}</span>
              <button type="button" className="m-toast__btn" onClick={() => setToast(null)}>
                Закрыть
              </button>
            </>
          )}
        </div>
      )}

      {sheet?.t === 'more' && (
        <Sheet title="Ещё" onClose={closeSheet}>
          {pending && <p className="m-sheet__wait">Дождитесь отправки или отмените текущую отметку.</p>}
          {canTransfer && (
            <button
              type="button"
              className="m-sheet__opt"
              disabled={locked}
              onClick={() => setSheet({ t: 'transfer', orderId: next!.order_id! })}
            >
              Не могу выполнить, передать наряд
            </button>
          )}
          {canUnavailable && (
            <button
              type="button"
              className="m-sheet__opt m-sheet__opt--danger"
              disabled={locked}
              onClick={() => setSheet({ t: 'unavailable' })}
            >
              Я недоступен (поломка, болезнь)
            </button>
          )}
          {!canTransfer && !canUnavailable && (
            <p className="m-sheet__wait">
              Передать наряд можно после отметки «Прибыл». Других действий сейчас нет.
            </p>
          )}
          {onSwitch && (
            <button type="button" className="m-sheet__opt" onClick={onSwitch}>
              Открыть маршрут другого сотрудника
            </button>
          )}
          <button type="button" className="m-btn m-btn--ghost" onClick={closeSheet}>
            Закрыть
          </button>
        </Sheet>
      )}

      {sheet?.t === 'transfer' && (
        <ReasonSheet
          title="Передать наряд"
          hint="Повреждение вне зоны ответственности: наряд уйдёт в профильное подразделение, вы освободитесь."
          options={TRANSFER_REASONS}
          holdLabel="Передать наряд"
          summary={(o) => `Наряд ${sheet.orderId} будет передан в ${o.to}, план пересчитается.`}
          onConfirm={(reason) => schedule({ kind: 'TRANSFER', orderId: sheet.orderId, reason })}
          onClose={closeSheet}
        />
      )}

      {sheet?.t === 'unavailable' && (
        <ReasonSheet
          title="Вы недоступны"
          hint="Оставшиеся наряды диспетчер передаст другим сотрудникам."
          options={UNAVAILABLE_REASONS}
          holdLabel="Сообщить: недоступен"
          summary={(o) =>
            `Вы будете сняты со смены (${o.label.toLowerCase()}), оставшиеся наряды получат другие сотрудники, план пересчитается.`
          }
          onConfirm={(reason) => schedule({ kind: 'UNAVAILABLE', reason })}
          onClose={closeSheet}
        />
      )}

      {sheet?.t === 'geo' && (
        <Sheet
          title="Вы точно на месте?"
          hint={
            <>
              По геолокации телефона до адреса около <b>{distanceText(sheet.distance)}</b>. Отметьте прибытие, только
              если вы уже у абонента.
            </>
          }
          onClose={closeSheet}
        >
          <button type="button" className="m-btn" onClick={() => schedule({ kind: 'ARRIVED', orderId: sheet.orderId })}>
            Да, я на месте
          </button>
          <button type="button" className="m-btn m-btn--ghost" onClick={closeSheet}>
            Ещё еду
          </button>
        </Sheet>
      )}

      {sheet?.t === 'logout' && (
        <Sheet
          title="Выйти из аккаунта?"
          hint={pending ? 'Неотправленная отметка будет отменена.' : 'Чтобы вернуться к маршруту, понадобится пароль.'}
          onClose={closeSheet}
        >
          <button
            type="button"
            className="m-btn m-btn--ghost m-btn--danger"
            onClick={() => {
              setPending(null);
              void auth?.logout();
            }}
          >
            Выйти
          </button>
          <button type="button" className="m-btn" onClick={closeSheet}>
            Остаться
          </button>
        </Sheet>
      )}
    </>
  );
}

// ---------------------------------------------------------------- next stop

/** Одна главная кнопка следующего шага по next_mark / allowed_actions. */
function MainAction({
  stop,
  pending,
  sending,
  locating,
  left,
  onCall,
  onArrive,
  onDone,
}: {
  stop: EmployeeStop;
  pending: Pending | null;
  sending: boolean;
  locating: boolean;
  left: number;
  onCall: () => void;
  onArrive: () => void;
  onDone: () => void;
}) {
  const kind = stop.next_mark;
  if (!kind || !stop.allowed_actions.includes(kind)) return null;
  if (pending || sending) {
    return (
      <button type="button" className="m-main" disabled>
        {sending ? 'Отправляем…' : `Отправим через ${left} с`}
      </button>
    );
  }
  if (kind === 'CALLED') {
    return (
      <button type="button" className="m-main" onClick={onCall}>
        Позвонил абоненту
      </button>
    );
  }
  if (kind === 'ARRIVED') {
    return (
      <button type="button" className="m-main" disabled={locating} onClick={onArrive}>
        {locating ? 'Проверяем место…' : 'Прибыл на адрес'}
      </button>
    );
  }
  if (kind === 'DONE') {
    return (
      <HoldButton
        label="Завершил работу"
        summary={`Наряд ${stop.order_id} будет закрыт.`}
        onConfirm={onDone}
      />
    );
  }
  return null;
}

function NextCard({ stop, route, children }: { stop: EmployeeStop; route: EmployeeRoute; children: ReactNode }) {
  const order = stop.order;
  const marks = stop.marks ?? { CALLED: null, ARRIVED: null, DONE: null, TRANSFER: null };
  const eq = equipmentText(order?.required_equipment);
  const lat = order?.lat ?? stop.location?.lat;
  const lon = order?.lon ?? stop.location?.lon;
  const late = order ? minutesBetween(stop.start, order.slot.end) : 0;

  return (
    <section className="m-card m-next" aria-label="Следующий адрес">
      <div className="m-next__when">
        <div>
          <span className="m-next__k">Слот</span>
          <span className="m-next__v num">
            {order ? `${hhmm(order.slot.start)}–${hhmm(order.slot.end)}` : '—'}
          </span>
        </div>
        <div>
          <span className="m-next__k">Прибытие по плану</span>
          <span className="m-next__v num">{hhmm(stop.arrival)}</span>
        </div>
      </div>

      <h2 className="m-next__address">{stopTitle(stop)}</h2>
      <p className="m-next__work">
        {order?.work_type_name ?? 'Визит'}
        {order && <span className="m-tag">{order.technology}</span>}
        {order?.requires_height && <span className="m-tag m-tag--warn">Работа на высоте</span>}
        {order?.is_repeat && <span className="m-tag m-tag--warn">Повторный</span>}
      </p>
      <p className="m-next__eq">
        {eq ? (
          <>
            Взять АО: <b>{eq.join(', ')}</b>
          </>
        ) : (
          'Абонентское оборудование не требуется'
        )}
      </p>
      {order && late > 0 && (
        <p className="m-next__late">
          Начало по плану {hhmm(stop.start)}, позже слота на {mins(late)}. Предупредите абонента.
        </p>
      )}

      {stop.call_window && (
        <CallWindow
          window={stop.call_window}
          asOf={route.as_of}
          calledAt={marks.CALLED}
          slotStart={order?.slot.start}
        />
      )}

      {lat != null && lon != null && <NavButton lat={lat} lon={lon} label={stopTitle(stop)} />}

      {/* Этапы — только индикатор; действие одно, кнопкой ниже. */}
      <ol className="m-steps m-steps--status" aria-label="Отметки по визиту">
        {STEPS.map((st, i) => {
          const at = marks[st.kind];
          const active = !at && stop.next_mark === st.kind;
          const cls = at ? 'is-done' : active ? 'is-active' : 'is-wait';
          return (
            <li key={st.kind} className={`m-step ${cls}`} aria-current={active ? 'step' : undefined}>
              <div className="m-step__btn">
                <span className="m-step__n" aria-hidden>
                  {at ? '✓' : i + 1}
                </span>
                <span className="m-step__label">{st.label}</span>
                {at && (
                  <span className="m-step__time num">
                    {st.done} в {hhmm(at)}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {children}
      <p className="m-next__note">Номера абонента в данных нет: кнопка звонка только ставит отметку.</p>
    </section>
  );
}

function CallWindow({
  window: w,
  asOf,
  calledAt,
  slotStart,
}: {
  window: { start: string; end: string };
  asOf: string;
  calledAt: string | null;
  slotStart?: string;
}) {
  if (calledAt) {
    return (
      <div className="m-call is-ok">
        <p className="m-call__head">Абоненту позвонили в {hhmm(calledAt)}</p>
      </div>
    );
  }
  const toStart = minutesBetween(w.start, asOf);
  const toEnd = minutesBetween(w.end, asOf);
  let tone = 'is-wait';
  let text: string;
  if (toStart > 0) text = `Окно откроется через ${mins(toStart)}`;
  else if (toEnd >= 0) {
    tone = 'is-open';
    text = toEnd ? `Пора звонить: осталось ${mins(toEnd)}` : 'Пора звонить: окно закрывается';
  } else {
    tone = 'is-late';
    text = `Окно прошло ${mins(-toEnd)} назад, позвоните сейчас`;
  }
  // Шкала от начала окна до начала слота: окно звонка — первая половина, маркер — время плана.
  const span = slotStart ? Math.max(1, minutesBetween(slotStart, w.start)) : 60;
  const pos = Math.min(100, Math.max(0, ((-toStart) / span) * 100));
  const winPct = Math.min(100, (minutesBetween(w.end, w.start) / span) * 100);
  return (
    <div className={`m-call ${tone}`}>
      <p className="m-call__head">
        Позвонить абоненту с <span className="num">{hhmm(w.start)}</span> до <span className="num">{hhmm(w.end)}</span>
      </p>
      <div className="m-call__track" aria-hidden>
        <div className="m-call__win" style={{ width: `${winPct}%` }} />
        <div className="m-call__now" style={{ left: `${pos}%` }} />
      </div>
      <div className="m-call__scale num" aria-hidden>
        <span>{hhmm(w.start)}</span>
        <span style={{ left: `${winPct}%` }}>{hhmm(w.end)}</span>
        {slotStart && <span>{hhmm(slotStart)}</span>}
      </div>
      <p className="m-call__text">{text}</p>
    </div>
  );
}

function NavButton({ lat, lon, label }: { lat: number; lon: number; label: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <div className="m-nav" ref={ref}>
      <button
        type="button"
        className="m-btn m-btn--nav"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((v) => !v)}
      >
        Построить маршрут
        <svg className={'m-nav__chev' + (open ? ' is-open' : '')} viewBox="0 0 16 16" width="16" height="16" aria-hidden>
          <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>
      {open && (
        <ul className="m-nav__menu" role="menu">
          {navLinks(lat, lon, label).map((l) => (
            <li key={l.id} role="none">
              <a
                role="menuitem"
                className="m-nav__item"
                href={l.href}
                target={l.id === 'geo' ? undefined : '_blank'}
                rel="noopener noreferrer"
                onClick={() => setOpen(false)}
              >
                {l.title}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- day list

const STATE_LABEL: Record<string, string> = { DONE: 'Сделано', IN_PROGRESS: 'В работе', PLANNED: 'Впереди' };

function StopRow({ stop, isNext, scName }: { stop: EmployeeStop; isNext: boolean; scName?: string }) {
  const o = stop.order;
  const state = isNext && stop.state === 'PLANNED' ? 'NEXT' : stop.state;
  const restock = stop.type === 'RESTOCK' ? equipmentText(stop.equipment_after ?? undefined) : null;
  const sub =
    stop.type === 'VISIT' && o
      ? `${o.work_type_name}, слот ${hhmm(o.slot.start)}–${hhmm(o.slot.end)}`
      : stop.type === 'BREAK'
        ? `до ${hhmm(stop.end)}`
        : restock
          ? `С собой после заезда: ${restock.join(', ')}`
          : null;
  return (
    <li className={`m-stop m-stop--${stop.type.toLowerCase()} is-${state.toLowerCase().replace('_', '-')}`}>
      <span className="m-stop__time num">{hhmm(stop.type === 'VISIT' ? stop.arrival : stop.start)}</span>
      <span className="m-stop__dot" aria-hidden />
      <span className="m-stop__body">
        <span className="m-stop__title">{stopTitle(stop, scName)}</span>
        {sub && <span className="m-stop__sub">{sub}</span>}
      </span>
      <span className="m-stop__state">{state === 'NEXT' ? 'Следующий' : STATE_LABEL[stop.state]}</span>
    </li>
  );
}
