/*
 * Сценарный анализ (/ui/scenario/*): «что будет, если выйдет меньше сотрудников» и
 * «какой запас АО нужен в рюкзаке, чтобы не заезжать в СЦ», «что дают пробки по часам». Расчёт идёт на копии дня,
 * живой план не меняется. Пострадавшие наряды кликабельны — выбор на карте как в других списках.
 */
import { useEffect, useRef, useState } from 'react';
import { Badge, Button, SegmentedControl } from './ui';
import { api, ApiFailure } from '../api/client';
import { REASON, WORK_TYPE_SHORT, window2 } from '../format';
import type { Selection } from '../planModel';
import type {
  EquipmentResult,
  ScenarioMetrics,
  StaffCutResult,
  StaffCutStrategy,
  TrafficMetrics,
  TrafficResult,
  UiReference,
  UnassignedReason,
} from '../types/api';
import './scenarios.css';

const BUDGET_SEC = 15;
const MAX_REMOVE = 15;
const KINDS = ['ONT', 'ROUTER', 'STB'] as const;
const REASONS = Object.keys(REASON) as UnassignedReason[];

type Better = 'up' | 'down' | null;
interface Row<M = ScenarioMetrics> {
  key: string;
  label: string;
  get: (m: M) => number;
  fmt: (v: number) => string;
  better: Better;
  sub?: boolean;
}

const int = (v: number) => String(Math.round(v));
const pct = (v: number) => `${(v * 100).toFixed(1)} %`;
const hours = (v: number) => `${v.toFixed(1)} ч`;
const ratio = (v: number) => v.toFixed(3);
const secs = (v: number) => `${v.toFixed(1)} с`;

const TRAFFIC_ROWS: Row<TrafficMetrics>[] = [
  { key: 'visits', label: 'Размещено', get: (m) => m.visits, fmt: int, better: 'up' },
  { key: 'unassigned', label: 'Не распределено', get: (m) => m.unassigned, fmt: int, better: 'down' },
  { key: 'share', label: 'Доля нарядов в слот (по плану)', get: (m) => m.in_slot_share, fmt: pct, better: 'up' },
  { key: 'share_tr', label: 'Доля в слот при пробках', get: (m) => m.in_slot_share_traffic, fmt: pct, better: 'up' },
  { key: 'travel', label: 'Время в пути с пробками', get: (m) => m.travel_traffic_h, fmt: hours, better: 'down' },
  { key: 'late', label: 'Поздних прибытий (LATE_RISK)', get: (m) => m.late_visits, fmt: int, better: 'down' },
  { key: 'viol', label: 'Нарушений валидатора', get: (m) => m.violations, fmt: int, better: 'down' },
  { key: 'time', label: 'Время расчёта', get: (m) => m.time_sec, fmt: secs, better: null },
];

function rowsFor(base: ScenarioMetrics, other: ScenarioMetrics): Row[] {
  const rows: Row[] = [
    { key: 'share', label: 'Доля нарядов в слот', get: (m) => m.in_slot_share, fmt: pct, better: 'up' },
    { key: 'visits', label: 'Размещено', get: (m) => m.visits, fmt: int, better: 'up' },
    { key: 'unassigned', label: 'Не распределено', get: (m) => m.unassigned, fmt: int, better: 'down' },
  ];
  for (const r of REASONS) {
    if (!base.unassigned_by_reason[r] && !other.unassigned_by_reason[r]) continue;
    rows.push({ key: r, label: REASON[r], get: (m) => m.unassigned_by_reason[r] ?? 0, fmt: int, better: 'down', sub: true });
  }
  rows.push(
    { key: 'travel', label: 'Время в пути', get: (m) => m.travel_h, fmt: hours, better: 'down' },
    { key: 'cv', label: 'Неравномерность загрузки (CV)', get: (m) => m.util_cv, fmt: ratio, better: 'down' },
    { key: 'restocks', label: 'Заездов в СЦ за АО', get: (m) => m.restocks, fmt: int, better: 'down' },
    { key: 'staff', label: 'Сотрудников с визитами', get: (m) => m.staff_used, fmt: int, better: null },
  );
  return rows;
}

function deltaText<M>(row: Row<M>, d: number) {
  if (Math.abs(d) < 1e-9) return '0';
  const sign = d > 0 ? '+' : '−';
  if (row.key.startsWith('share')) return `${sign}${Math.abs(d * 100).toFixed(1)} п.п.`;
  return sign + row.fmt(Math.abs(d)).replace(' ч', '');
}

function deltaTone<M>(row: Row<M>, d: number) {
  if (Math.abs(d) < 1e-9 || !row.better) return 'sc-d--zero';
  return (d > 0) === (row.better === 'up') ? 'sc-d--good' : 'sc-d--bad';
}

function Compare<M extends ScenarioMetrics>({
  base,
  other,
  labels,
  rows,
}: {
  base: M;
  other: M;
  labels: [string, string];
  rows?: Row<M>[];
}) {
  return (
    <table className="sc-table">
      <thead>
        <tr>
          <th />
          <th className="sc-num">{labels[0]}</th>
          <th className="sc-num">{labels[1]}</th>
          <th className="sc-num">Δ</th>
        </tr>
      </thead>
      <tbody>
        {(rows ?? (rowsFor(base, other) as Row<M>[])).map((row) => {
          const a = row.get(base);
          const b = row.get(other);
          const d = b - a;
          return (
            <tr key={row.key} className={row.sub ? 'sc-sub' : undefined}>
              <td>{row.label}</td>
              <td className="sc-num">{row.fmt(a)}</td>
              <td className="sc-num sc-strong">{row.fmt(b)}</td>
              <td className={`sc-num sc-d ${deltaTone(row, d)}`}>{deltaText(row, d)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Секунды с начала расчёта; полоса заполняется к ожидаемому времени, но не до конца. */
function Progress({ startedAt, expected }: { startedAt: number; expected: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(t);
  }, []);
  const sec = (now - startedAt) / 1000;
  const share = Math.min(0.95, sec / expected);
  return (
    <div className="sc-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(share * 100)}>
      <div className="sc-progress__bar">
        <span style={{ width: `${share * 100}%` }} />
      </div>
      <span className="sc-progress__text num">
        {sec.toFixed(0)} с из ≈{expected} с
      </span>
    </div>
  );
}

const STRATEGY_LABEL: Record<string, string> = {
  random: 'Случайные',
  least_loaded: 'Наименее загруженные',
  'by_profile:INSTALL': 'Только инсталляторы',
  'by_profile:LTP3': 'Только ЗЛТП',
};

type Kind = 'cut' | 'eq' | 'traffic';
type Running = { kind: Kind; startedAt: number; expected: number } | null;

export function ScenariosPanel({
  date,
  planVersion,
  reference,
  selected,
  onSelect,
}: {
  date?: string;
  planVersion?: number;
  reference: UiReference | null;
  selected: Selection | null;
  onSelect: (id: string) => void;
}) {
  const [count, setCount] = useState(3);
  const [strategy, setStrategy] = useState<StaffCutStrategy>('random');
  const [seed, setSeed] = useState(1);
  const [cut, setCut] = useState<StaffCutResult | null>(null);
  const [eq, setEq] = useState<EquipmentResult | null>(null);
  const [traffic, setTraffic] = useState<TrafficResult | null>(null);
  const [running, setRunning] = useState<Running>(null);
  const [error, setError] = useState<{ kind: Kind; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Без событий база — текущий план; после событий база строится заново, расчётов два.
  const expected = BUDGET_SEC * ((planVersion ?? 1) > 1 ? 2 : 1) + 2;

  async function run<T>(kind: Kind, call: () => Promise<T>, done: (r: T) => void, expectedSec = expected) {
    if (running) return;
    setRunning({ kind, startedAt: Date.now(), expected: expectedSec });
    setError(null);
    try {
      const r = await call();
      if (alive.current) done(r);
    } catch (e) {
      if (alive.current) {
        const text = e instanceof ApiFailure || e instanceof Error ? e.message : String(e);
        // Нет данных дня (409 PLAN_NOT_BUILT / 404): сценарий недоступен для этого дня, а не «сломался».
        const noDay = e instanceof ApiFailure && (e.code === 'PLAN_NOT_BUILT' || e.status === 404);
        setError({ kind, text: noDay ? `Недоступно для этого дня. ${text}` : text });
      }
    } finally {
      if (alive.current) setRunning(null);
    }
  }

  const calcCut = () =>
    run('cut', () => api.scenarioStaffCut({ date, remove_count: count, strategy, seed, budget_sec: BUDGET_SEC }), setCut);
  const calcEq = () => run('eq', () => api.scenarioEquipment({ date, mode: 'no_restock', budget_sec: BUDGET_SEC }), setEq);
  // два плана на копии: без коэффициентов и с ними
  const calcTraffic = () =>
    run('traffic', () => api.scenarioTraffic({ date, budget_sec: BUDGET_SEC }), setTraffic, BUDGET_SEC * 2 + 2);

  const centers = reference?.service_centers ?? [];
  const disabled = !date || !!running;

  return (
    <div className="list">
      <div className="scroll sc">
        <p className="sc-note">
          Расчёт на копии дня {date ?? '—'}: живой план и маршруты не меняются. База —{' '}
          {(planVersion ?? 1) > 1 ? 'первоначальный план, пересчитанный без событий' : 'текущий план дня (версия 1)'}.
        </p>

        {/* ------------------------------------------------ меньше сотрудников */}
        <section className="sc-sec">
          <header className="sc-sec__head">
            <h3>Меньше сотрудников</h3>
            <span className="sc-sec__q">Что будет, если на смену выйдет на N человек меньше?</span>
          </header>

          <div className="sc-controls">
            <div className="sc-field">
              <span className="sc-label">Убрать</span>
              <div className="sc-stepper">
                <button type="button" aria-label="Меньше" disabled={disabled || count <= 0} onClick={() => setCount((c) => c - 1)}>
                  −
                </button>
                <input
                  className="num"
                  type="number"
                  min={0}
                  max={MAX_REMOVE}
                  value={count}
                  disabled={!!running}
                  aria-label="Сколько сотрудников убрать"
                  onChange={(e) => setCount(Math.max(0, Math.min(MAX_REMOVE, Number(e.target.value) || 0)))}
                />
                <button
                  type="button"
                  aria-label="Больше"
                  disabled={disabled || count >= MAX_REMOVE}
                  onClick={() => setCount((c) => c + 1)}
                >
                  +
                </button>
              </div>
            </div>
            <label className="sc-field">
              <span className="sc-label">Кого</span>
              <select
                className="select"
                value={strategy}
                disabled={!!running}
                onChange={(e) => setStrategy(e.target.value as StaffCutStrategy)}
              >
                {Object.entries(STRATEGY_LABEL).map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
                {centers.length > 0 && (
                  <optgroup label="Из одного СЦ">
                    {centers.map((c) => (
                      <option key={c.service_center_id} value={`by_service_center:${c.service_center_id}`}>
                        {c.service_center_id} · {c.name}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
            {strategy !== 'least_loaded' && (
              <div className="sc-field" title="Другое зерно — другой случайный выбор сотрудников">
                <span className="sc-label">Выборка</span>
                <SegmentedControl
                  size="s"
                  variant="primary"
                  value={String(seed)}
                  disabledAll={!!running}
                  onChange={(v) => setSeed(Number(v))}
                  items={[1, 2, 3].map((n) => ({ index: String(n), label: `#${n}` }))}
                />
              </div>
            )}
            <Button size="s" label="Рассчитать" disabled={disabled} onClick={calcCut} />
          </div>

          {running?.kind === 'cut' && <Progress startedAt={running.startedAt} expected={running.expected} />}
          {error?.kind === 'cut' && <p className="sc-error">{error.text}</p>}

          {cut && (
            <div className={`sc-result${running?.kind === 'cut' ? ' is-stale' : ''}`}>
              <div className="sc-meta">
                <span>
                  {STRATEGY_LABEL[cut.strategy] ?? cut.strategy.replace('by_service_center:', 'СЦ ')}, −{cut.remove_count}
                </span>
                <span className="num">расчёт {cut.time_sec.toFixed(1)} с</span>
              </div>
              {cut.removed.length > 0 && (
                <ul className="chips sc-removed">
                  {cut.removed.map((r) => (
                    <li key={r.staff_id} title={`${r.service_center_id}, в базе визитов: ${r.base_visits}`}>
                      {r.display_name ?? r.staff_id}
                      <span className="sub">
                        {r.profiles.join('/')} · {r.service_center_id} · {r.base_visits} виз.
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <Compare base={cut.base} other={cut.scenario} labels={['База', 'Сценарий']} />
              <p className="sc-foot num">
                Передано другим: {cut.reassigned} · встали заново: {cut.newly_routed.length}
              </p>

              <h4 className="sc-h4">
                Пострадавшие наряды <span className="num">{cut.affected_orders.length}</span>
              </h4>
              {cut.affected_orders.length === 0 ? (
                <p className="sc-empty">Все наряды базы остались в маршрутах.</p>
              ) : (
                <ul className="sc-affected">
                  {cut.affected_orders.map((a) => (
                    <li
                      key={a.order_id}
                      className={selected?.id === a.order_id ? 'is-selected' : undefined}
                      onClick={() => onSelect(a.order_id)}
                      title={a.reason_text ?? a.address ?? undefined}
                    >
                      <span className="mono-id">{a.order_id}</span>
                      <span className="sc-affected__what">
                        {a.work_type_code ? WORK_TYPE_SHORT[a.work_type_code] : '—'}
                        {a.is_repeat && <span className="flag">повт.</span>}
                        <span className="sub num">
                          слот {window2(a.slot)} · был у {a.base_staff_id}
                          {a.base_staff_removed ? ' (убран)' : ''}
                        </span>
                      </span>
                      {a.reason_code && (
                        <Badge
                          size="2xs"
                          variant="secondary"
                          colorScheme={a.reason_code === 'PGP' ? 'status-01' : 'error'}
                          label={a.reason_code === 'PGP' ? 'ПГП' : REASON[a.reason_code]}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>

        {/* ------------------------------------------------ запас АО */}
        <section className="sc-sec">
          <header className="sc-sec__head">
            <h3>Запас АО в рюкзаке</h3>
            <span className="sc-sec__q">Сколько ONT, роутеров и приставок взять с утра, чтобы не заезжать в СЦ?</span>
          </header>
          <div className="sc-controls">
            <p className="sc-hint">
              План строится с «бездонным» рюкзаком, по маршрутам считается расход АО каждого сотрудника за смену.
            </p>
            <Button size="s" label="Оценить" disabled={disabled} onClick={calcEq} />
          </div>

          {running?.kind === 'eq' && <Progress startedAt={running.startedAt} expected={running.expected} />}
          {error?.kind === 'eq' && <p className="sc-error">{error.text}</p>}

          {eq && (
            <div className={`sc-result${running?.kind === 'eq' ? ' is-stale' : ''}`}>
              <div className="sc-meta">
                <span>
                  Выиграно нарядов: <b className="num">{eq.visits_gained > 0 ? `+${eq.visits_gained}` : eq.visits_gained}</b>
                  {' · '}в пути {eq.travel_saved_h >= 0 ? 'меньше' : 'больше'} на{' '}
                  <b className="num">{Math.abs(eq.travel_saved_h).toFixed(1)} ч</b>
                </span>
                <span className="num">расчёт {eq.time_sec.toFixed(1)} с</span>
              </div>
              <Compare base={eq.current} other={eq.no_restock} labels={['Как сейчас', 'Без заездов']} />

              <h4 className="sc-h4">Рекомендуемый запас на сотрудника</h4>
              <table className="sc-table sc-eq">
                <thead>
                  <tr>
                    <th />
                    <th className="sc-num">p50</th>
                    <th className="sc-num">p90</th>
                    <th className="sc-num">макс.</th>
                    <th className="sc-num">сейчас, ср.</th>
                    <th className="sc-num">ёмкость</th>
                  </tr>
                </thead>
                {eq.recommended.map((r) => (
                  <tbody key={r.profile}>
                    <tr className="sc-group">
                      <td colSpan={6}>
                        {r.profile === 'INSTALL' ? 'Инсталляторы' : 'ЗЛТП'}
                        <span className="sub num">
                          {r.staff_with_visits} из {r.staff_on_shift} с визитами · не хватило бы утреннего запаса:{' '}
                          {r.staff_exceeding_current_start}
                        </span>
                      </td>
                    </tr>
                    {KINDS.map((k) => {
                      const st = r[k];
                      return (
                        <tr key={k}>
                          <td>{k === 'STB' ? 'STB (приставка)' : k === 'ROUTER' ? 'Роутер' : 'ONT'}</td>
                          <td className="sc-num">{st.p50}</td>
                          <td className={`sc-num sc-strong${st.p90 > st.capacity ? ' sc-over' : ''}`}>{st.p90}</td>
                          <td className={`sc-num${st.max > st.capacity ? ' sc-over' : ''}`}>{st.max}</td>
                          <td className="sc-num sc-muted">{st.current_start_avg.toFixed(1)}</td>
                          <td className="sc-num sc-muted">{st.capacity}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                ))}
              </table>
              <p className="sc-foot">
                Рекомендация — p90: девять из десяти сотрудников профиля обойдутся без заезда. Красным — больше ёмкости
                рюкзака, такой запас физически не взять.
              </p>
            </div>
          )}
        </section>

        {/* ------------------------------------------------ пробки по часам */}
        <section className="sc-sec">
          <header className="sc-sec__head">
            <h3>Пробки по часам</h3>
            <span className="sc-sec__q">Что изменится, если планировать с учётом загруженности дорог по часам суток?</span>
          </header>
          <div className="sc-controls">
            <p className="sc-hint">
              Время в пути умножается на коэффициент часа выезда из матрицы (не меньше базового). Оба плана исполняются в
              пробках: видно, сколько визитов плана без коэффициентов опоздало бы. В живом планировании режим по умолчанию
              выключен и включается переменной <code>TRAFFIC_FACTORS=on</code>.
            </p>
            <Button size="s" label="Сравнить" disabled={disabled} onClick={calcTraffic} />
          </div>

          {running?.kind === 'traffic' && <Progress startedAt={running.startedAt} expected={running.expected} />}
          {error?.kind === 'traffic' && <p className="sc-error">{error.text}</p>}

          {traffic && (
            <div className={`sc-result${running?.kind === 'traffic' ? ' is-stale' : ''}`}>
              <div className="sc-meta">
                <span>
                  Коэффициенты:{' '}
                  {traffic.factors.length === 0 ? (
                    'в матрице нет'
                  ) : (
                    <span className="num">
                      {traffic.factors.map((f) => `${String(f.hour).padStart(2, '0')}:00 ×${f.factor}`).join(', ')}
                    </span>
                  )}
                  {' · '}сейчас {traffic.traffic_env ? 'включено' : 'выключено'}
                </span>
                <span className="num">расчёт {traffic.time_sec.toFixed(1)} с</span>
              </div>
              <Compare base={traffic.off} other={traffic.on} labels={['Без пробок', 'С пробками']} rows={TRAFFIC_ROWS} />
              <p className="sc-foot">
                Валидатор организаторов проверяет время в пути по базовой матрице, поэтому план с пробками ему тоже
                удовлетворяет: переезды в нём только длиннее.
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
