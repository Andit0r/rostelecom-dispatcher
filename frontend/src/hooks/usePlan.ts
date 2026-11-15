import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { api, ApiFailure } from '../api/client';
import type { DemoStatus, DemoStepItem, Plan, PlanStatus, UiOrder, UiReference } from '../types/api';
import { pushMetric, type MetricPoint } from '../planModel';

export type Busy = null | 'init' | 'load' | 'refresh';

/** Подсветка после автообновления: nonce растёт на каждое обновление (перезапуск CSS-анимации). */
export interface AutoFlash {
  nonce: number;
  /** Появились новые активные LATE_RISK. */
  late: boolean;
  /** Появились новые ПГП (наряды или тревога PGP_DETECTED). */
  pgp: boolean;
}

export interface PlanState {
  demo: DemoStatus | null;
  plan: Plan | null;
  orders: UiOrder[];
  reference: UiReference | null;
  busy: Busy;
  error: string | null;
  lastStep: DemoStepItem[];
  /** Метрики версий плана, увиденных в этой сессии (запасной источник графика динамики без журнала БД). */
  sessionMetrics: MetricPoint[];
  /** Последнее автообновление плана (null — ещё не было). */
  flash: AutoFlash | null;
  /** На сервере нет дня с планом (ещё не пришёл по API или очищен): пустое состояние «ждём данные». */
  noDay: boolean;
}

/** Период опроса /ui/plan/status. */
export const POLL_MS = 2000;
/** Потолок экспоненциального отката при ошибках опроса. */
const MAX_BACKOFF_MS = 30000;
const AUTO_KEY = 'rostelecom.autoRefresh';

/* Состояние связи живёт вне React-состояния хука: отметка последней проверки меняется каждые 2 с,
 * и перерисовывать из-за неё карту, Гант и окна незачем — её читает только шапка. */
export interface LiveStatus {
  /** Последнее ИЗМЕНЕНИЕ показанного плана (новая версия/день или ручное «Обновить данные»). */
  syncedAt: number | null;
  /** Последний успешный опрос /ui/plan/status (тихий индикатор связи). */
  checkedAt: number | null;
  offline: boolean;
}
export interface LiveStore {
  get: () => LiveStatus;
  set: (p: Partial<LiveStatus>) => void;
  subscribe: (fn: () => void) => () => void;
}
function createLiveStore(): LiveStore {
  let value: LiveStatus = { syncedAt: null, checkedAt: null, offline: false };
  const subs = new Set<() => void>();
  return {
    get: () => value,
    set: (p) => {
      const next = { ...value, ...p };
      if (next.syncedAt === value.syncedAt && next.checkedAt === value.checkedAt && next.offline === value.offline) return;
      value = next;
      subs.forEach((f) => f());
    },
    subscribe: (fn) => {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
  };
}
export function useLiveStatus(live: LiveStore): LiveStatus {
  return useSyncExternalStore(live.subscribe, live.get);
}

function readAuto(): boolean {
  try {
    return localStorage.getItem(AUTO_KEY) !== 'off';
  } catch {
    return true;
  }
}
function writeAuto(on: boolean) {
  try {
    localStorage.setItem(AUTO_KEY, on ? 'on' : 'off');
  } catch {
    /* хранилище недоступно — настройка живёт до перезагрузки */
  }
}

const sameTime = (a?: string | null, b?: string | null) => (a && b ? Date.parse(a) === Date.parse(b) : a == b);
const lateIds = (p: Plan | null) =>
  new Set((p?.alerts ?? []).filter((a) => a.type === 'LATE_RISK' && a.status === 'ACTIVE').map((a) => a.alert_id));
const pgpCount = (orders: UiOrder[]) => orders.filter((o) => o.status === 'PGP').length;
const pgpAlertIds = (p: Plan | null) => new Set((p?.alerts ?? []).filter((a) => a.type === 'PGP_DETECTED').map((a) => a.alert_id));
const hasNew = (before: Set<string>, after: Set<string>) => [...after].some((id) => !before.has(id));

interface FetchOpts {
  /** false — результат устарел (началась другая операция), не применять. */
  valid?: () => boolean;
  /** Дополнительные поля того же коммита состояния. */
  extra?: (s: PlanState, plan: Plan, orders: UiOrder[]) => Partial<PlanState>;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const EMPTY_DAY: Partial<PlanState> = { demo: null, plan: null, orders: [], lastStep: [], sessionMetrics: [], flash: null, noDay: true };

/** План дня + наряды со статусами. После шага событий опрашивает план, пока версия
 *  не дойдёт до той, что вернул сервер (до 6 попыток). Автообновление: опрос /ui/plan/status
 *  каждые POLL_MS и перечитывание плана, если сервер изменил его сам (внешний клиент /api/v1). */
export function usePlan() {
  const [state, setState] = useState<PlanState>({
    demo: null,
    plan: null,
    orders: [],
    reference: null,
    busy: 'init',
    error: null,
    lastStep: [],
    sessionMetrics: [],
    flash: null,
    noDay: false,
  });
  const busyRef = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;
  /** Растёт с каждой собственной операцией: фоновая синхронизация, начатая раньше, не применяется. */
  const genRef = useRef(0);
  /** load_id показанного дня (null — ещё не сверяли с /ui/plan/status). */
  const loadIdRef = useRef<string | null>(null);
  const live = useRef<LiveStore>(createLiveStore()).current;
  const [autoRefresh, setAutoState] = useState(readAuto);

  const setAutoRefresh = useCallback((on: boolean) => {
    writeAuto(on);
    setAutoState(on);
  }, []);

  const patch = (p: Partial<PlanState>) => setState((s) => ({ ...s, ...p }));
  const fail = (e: unknown) => patch({ error: e instanceof ApiFailure || e instanceof Error ? e.message : String(e) });

  /** true — состояние обновлено. */
  const fetchDay = useCallback(
    async (date: string, minVersion = 0, withReference = false, opts?: FetchOpts): Promise<boolean> => {
      for (let attempt = 0; attempt < 6; attempt++) {
        const [plan, orders, reference] = await Promise.all([
          api.getPlan(date),
          api.getOrders(date),
          withReference ? api.getReference() : Promise.resolve(null),
        ]);
        // План и наряды должны быть одной версии: иначе вкладка «Риск опоздания» (alerts плана)
        // и флаги late_risk в списке нарядов разойдутся после «+N событий».
        const fresh = plan.plan_version >= minVersion && orders.plan_version === plan.plan_version;
        if (fresh || attempt === 5) {
          if (opts?.valid && !opts.valid()) return false;
          setState((s) => ({
            ...s,
            plan,
            orders: orders.orders,
            reference: reference ?? s.reference,
            sessionMetrics: pushMetric(s.sessionMetrics, plan),
            noDay: false,
            ...opts?.extra?.(s, plan, orders.orders),
          }));
          live.set({ syncedAt: Date.now() });
          return true;
        }
        await sleep(500);
      }
      return false;
    },
    [live],
  );

  /** true — fn выполнилась без ошибки (ошибка уходит в state.error). */
  const run = useCallback(async (busy: Busy, fn: () => Promise<void>): Promise<boolean> => {
    if (busyRef.current) return false;
    busyRef.current = true;
    genRef.current++;
    patch({ busy, error: null });
    try {
      await fn();
      return true;
    } catch (e) {
      fail(e);
      return false;
    } finally {
      busyRef.current = false;
      patch({ busy: null });
    }
  }, []);

  /** Какой день сейчас на сервере — активный день /ui/plan/status (изменившийся последним: PUT
   *  /api/v1/days, события, «Создать день»; иначе самый поздний с планом). null — дня нет. */
  const currentDay = useCallback(async (): Promise<DemoStatus | null> => {
    const ps = await api.planStatus();
    if (ps.loaded && ps.date) {
      return { loaded: true, date: ps.date, as_of: ps.as_of ?? undefined, plan_version: ps.plan_version ?? undefined };
    }
    return null;
  }, []);

  useEffect(() => {
    void run('init', async () => {
      const day = await currentDay();
      if (day?.date) {
        patch({ demo: day });
        await fetchDay(day.date, 0, true);
      } else {
        // Дня на сервере нет: ждём данные через API (опрос подхватит) или «Создать день».
        patch(EMPTY_DAY);
      }
    });
  }, [run, fetchDay, currentDay]);

  /** «Создать день» (POST /ui/day/create): пустой день на дату и пустой план; true — успешно. */
  const createDay = useCallback(
    (date?: string) =>
      run('load', async () => {
        loadIdRef.current = null;
        const res = await api.dayCreate(date ? { date } : {});
        patch({ demo: { loaded: true, date: res.date, as_of: res.now, plan_version: res.plan_version }, lastStep: [], sessionMetrics: [] });
        await fetchDay(res.date, res.plan_version, true);
      }),
    [run, fetchDay],
  );

  /** «Пересобрать» (POST /ui/day/rebuild): план текущего дня с нуля по его нарядам, режимам и рюкзакам. */
  const rebuildDay = useCallback(
    () =>
      run('load', async () => {
        loadIdRef.current = null; // новое поколение дня сверим при следующем опросе
        const res = await api.dayRebuild();
        // Пересборка начинает версии плана заново: прежние метрики сессии к нему не относятся.
        patch({ lastStep: [], sessionMetrics: [] });
        await fetchDay(res.date, res.plan_version, true);
      }),
    [run, fetchDay],
  );

  /** «Очистить день» (POST /ui/day/reset): сервер забывает дни, планы и события; справочники остаются.
   *  Интерфейс переходит в пустое состояние, история версий сессии сбрасывается. */
  const resetDay = useCallback(
    () =>
      run('load', async () => {
        await api.dayReset();
        loadIdRef.current = null;
        patch(EMPTY_DAY);
      }),
    [run],
  );

  const refresh = useCallback(
    () =>
      run('refresh', async () => {
        // Перечитываем, какой день сейчас на сервере (мог смениться через API), затем план и наряды.
        const day = await currentDay();
        if (!day) {
          // Дня на сервере нет (очищен или ещё не загружен через API): пустое состояние вместо ошибки 404.
          loadIdRef.current = null;
          patch(EMPTY_DAY);
          return;
        }
        patch({ demo: day });
        if (day.date) await fetchDay(day.date, 0, true);
      }),
    [run, fetchDay, currentDay],
  );

  /** Событие уже отправлено сервером (наряд создан/отменён из панели «Данные»):
   *  показываем итог в верхней строке и подтягиваем план нужной версии. */
  const applyEvent = useCallback(
    (item: DemoStepItem) =>
      run('refresh', async () => {
        patch({ lastStep: [item] });
        if (state.demo?.date) await fetchDay(state.demo.date, item.plan_version ?? 0);
      }),
    [run, fetchDay, state.demo?.date],
  );

  /** Сервер изменил план сам (внешний клиент /api/v1): тихо перечитываем без busy и «Обновляем…».
   *  Выбор, фильтры, раскладка окон живут в App/панелях и не трогаются. */
  const syncFrom = useCallback(
    async (st: PlanStatus) => {
      const cur = stateRef.current;
      if (!st.loaded || !st.date || st.plan_version == null) {
        // День исчез на сервере (очищен, PUT reference внешним клиентом): показываем пустое состояние.
        // Раскладка окон и выбор живут в App и не трогаются; новый день подхватит следующий опрос.
        if (!st.loaded && (cur.plan || !cur.noDay) && cur.busy === null && !busyRef.current) {
          loadIdRef.current = null;
          setState((s) => ({ ...s, ...EMPTY_DAY, error: null }));
          live.set({ syncedAt: Date.now() });
        }
        return;
      }
      const known = loadIdRef.current;
      const reloaded = known !== null && st.load_id !== null && st.load_id !== known;
      const newDay = cur.plan?.date !== st.date || reloaded;
      if (!newDay && cur.plan?.plan_version === st.plan_version && sameTime(cur.plan?.as_of, st.as_of)) {
        loadIdRef.current = st.load_id;
        return;
      }
      const gen = genRef.current;
      const date = st.date;
      const ok = await fetchDay(date, st.plan_version, newDay, {
        valid: () => genRef.current === gen && !busyRef.current,
        extra: (s, plan, orders) => ({
          demo:
            s.demo?.date === date
              ? { ...s.demo, as_of: plan.as_of, plan_version: plan.plan_version }
              : { loaded: false, date, as_of: plan.as_of, plan_version: plan.plan_version },
          // Тот же день загружен заново (PUT days): прежние версии к нему не относятся.
          sessionMetrics: reloaded ? pushMetric([], plan) : pushMetric(s.sessionMetrics, plan),
          lastStep: newDay ? [] : s.lastStep,
          error: null,
          flash: {
            nonce: (s.flash?.nonce ?? 0) + 1,
            late: !newDay && hasNew(lateIds(s.plan), lateIds(plan)),
            pgp: !newDay && (pgpCount(orders) > pgpCount(s.orders) || hasNew(pgpAlertIds(s.plan), pgpAlertIds(plan))),
          },
        }),
      });
      if (ok) loadIdRef.current = st.load_id;
    },
    [fetchDay, live],
  );

  // Опрос: пауза при скрытой вкладке и во время собственной операции, откат при ошибках.
  useEffect(() => {
    if (!autoRefresh) {
      live.set({ offline: false });
      return;
    }
    let stopped = false;
    let inFlight = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = (ms: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => void tick(), ms);
    };
    const tick = async () => {
      timer = undefined;
      if (stopped || document.hidden) return; // возобновит visibilitychange
      if (busyRef.current) {
        schedule(POLL_MS);
        return;
      }
      inFlight = true;
      let delay = POLL_MS;
      try {
        const st = await api.planStatus();
        if (stopped) return;
        await syncFrom(st);
        failures = 0;
        // «обновлено» (syncedAt) ставит только fetchDay — при реальном изменении плана.
        live.set({ checkedAt: Date.now(), offline: false });
      } catch (e) {
        // 401 уже обработан в api/client (экран входа); здесь только откат.
        failures++;
        const net = e instanceof ApiFailure && (e.status === 0 || e.status >= 500);
        live.set({ offline: net });
        delay = Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS);
      } finally {
        inFlight = false;
      }
      if (!stopped && !document.hidden) schedule(delay);
    };
    const onVisible = () => {
      if (!document.hidden && !inFlight && !stopped) schedule(0);
    };
    document.addEventListener('visibilitychange', onVisible);
    schedule(POLL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [autoRefresh, syncFrom, live]);

  return { ...state, createDay, rebuildDay, resetDay, refresh, applyEvent, autoRefresh, setAutoRefresh, live };
}
