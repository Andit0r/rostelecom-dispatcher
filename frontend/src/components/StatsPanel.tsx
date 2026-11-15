/*
 * Окно «Статистика»: динамика плана по версиям, загрузка сотрудников, нераспределённые по причинам.
 * Собственный SVG без библиотек: цвета — токены --atmr-stat-*, текст — текстовые токены, тултипы — .gantt-tip.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { api, isUnavailable } from '../api/client';
import { REASON, hhmm } from '../format';
import { PROFILE_SHORT, minutesOf, minutesOfHm, type MetricPoint, type Selection, type StaffInfo } from '../planModel';
import type { ForecastModel, ForecastRisk, HistoryVersion, Plan, Profile, UiOrder, UnassignedReason, WorkType } from '../types/api';
import { SegmentedControl } from './ui';
import './stats.css';

/* ---------------------------------------------------------------- общие мелочи */

interface Tip {
  x: number;
  y: number;
  title: string;
  lines: string[];
}
type ShowTip = (e: ReactMouseEvent, title: string, lines: string[]) => void;

function TipView({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  const below = tip.y > window.innerHeight - 170;
  return createPortal(
    <div
      className="gantt-tip"
      role="tooltip"
      style={{
        left: Math.max(8, Math.min(tip.x + 14, window.innerWidth - 300)),
        top: below ? tip.y - 12 : tip.y + 16,
        transform: below ? 'translateY(-100%)' : undefined,
      }}
    >
      <strong>{tip.title}</strong>
      {tip.lines.map((l, i) => (
        <span key={i}>{l}</span>
      ))}
    </div>,
    document.body,
  );
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const num = (v: number) => v.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
const hm = (min: number) => {
  const m = Math.round(min);
  if (m < 60) return `${m} мин`;
  return m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
};

function Section({ title, meta, aside, children }: { title: string; meta?: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="st-sec">
      <header className="st-sec__head">
        <h3 className="st-sec__title">{title}</h3>
        {meta && <span className="st-sec__meta">{meta}</span>}
        {aside && <span className="st-sec__aside">{aside}</span>}
      </header>
      {children}
    </section>
  );
}

/* ---------------------------------------------------------------- 1. динамика по версиям */

type SeriesKey = 'visits' | 'unassigned' | 'lateRisk' | 'travelH';
interface Point {
  version: number;
  asOf: string | null;
  visits: number;
  unassigned: number;
  lateRisk: number;
  travelH: number;
}
const SERIES: { key: SeriesKey; label: string; fmt: (v: number) => string }[] = [
  { key: 'visits', label: 'Размещено визитов', fmt: (v) => String(Math.round(v)) },
  { key: 'unassigned', label: 'Не распределено', fmt: (v) => String(Math.round(v)) },
  { key: 'lateRisk', label: 'Риск опоздания', fmt: (v) => String(Math.round(v)) },
  { key: 'travelH', label: 'Пробег, ч', fmt: (v) => num(v) },
];

const fromSession = (m: MetricPoint): Point => ({
  version: m.version,
  asOf: m.asOf,
  visits: m.visits,
  unassigned: m.unassigned,
  lateRisk: m.lateRisk,
  travelH: m.travelSec / 3600,
});
const fromJournal = (v: HistoryVersion): Point => ({
  version: v.version,
  asOf: v.as_of,
  visits: v.visits_planned ?? NaN,
  unassigned: v.unassigned ?? NaN,
  lateRisk: v.late_risk_active ?? NaN,
  travelH: v.travel_sec_total != null ? v.travel_sec_total / 3600 : NaN,
});

/** Журнал БД по дню; null — журнал недоступен (503 без Postgres) или пуст. */
function useJournal(date: string, version: number): HistoryVersion[] | null {
  const [rows, setRows] = useState<{ date: string; rows: HistoryVersion[] | null } | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .historyVersions(date)
      .then((r) => alive && setRows({ date, rows: r.versions.length ? r.versions : null }))
      .catch(() => alive && setRows({ date, rows: null }));
    return () => {
      alive = false;
    };
  }, [date, version]);
  return rows?.date === date ? rows.rows : null;
}

function Dynamics({ plan, session, showTip, hideTip }: { plan: Plan; session: MetricPoint[]; showTip: ShowTip; hideTip: () => void }) {
  const journal = useJournal(plan.date, plan.plan_version);
  const [boxRef, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const points = useMemo(() => {
    const m = new Map<number, Point>();
    for (const s of session) if (s.date === plan.date) m.set(s.version, fromSession(s));
    // Журнал важнее: в нём все версии дня, а не только увиденные этой вкладкой.
    for (const v of journal ?? []) m.set(v.version, fromJournal(v));
    return [...m.values()].sort((a, b) => a.version - b.version).slice(-200);
  }, [session, journal, plan.date]);

  const cols = w >= 520 ? 2 : 1;
  const cardW = Math.max(120, Math.floor((w - (cols - 1) * 8) / cols) - 2);
  const H = 54;
  const PAD = 5;
  const v0 = points[0]?.version ?? 0;
  const v1 = points[points.length - 1]?.version ?? 0;
  const x = (v: number) => (v1 === v0 ? cardW / 2 : PAD + ((v - v0) / (v1 - v0)) * (cardW - 2 * PAD));

  const hovered = hover != null ? points.find((p) => p.version === hover) : undefined;

  const onMove = (e: ReactMouseEvent<SVGSVGElement>) => {
    if (!points.length) return;
    const r = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - r.left;
    let best = points[0];
    for (const p of points) if (Math.abs(x(p.version) - mx) < Math.abs(x(best.version) - mx)) best = p;
    setHover(best.version);
    showTip(e, `Версия ${best.version}${best.asOf ? ` · ${hhmm(best.asOf)}` : ''}`, SERIES.map((s) => `${s.label}: ${Number.isNaN(best[s.key]) ? '—' : s.fmt(best[s.key])}`));
  };
  const onLeave = () => {
    setHover(null);
    hideTip();
  };

  const sourceNote = journal ? 'журнал плана' : 'история сессии';
  return (
    <Section
      title="Динамика плана по версиям"
      meta={points.length ? `версии ${v0}–${v1}` : undefined}
      aside={
        <span className="st-note" title={journal ? 'Метрики из журнала PostgreSQL (/ui/history)' : 'Журнал недоступен: точки накоплены этой вкладкой с момента загрузки дня'}>
          {sourceNote}
        </span>
      }
    >
      <div ref={boxRef} className="st-multi" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
        {w > 0 &&
          SERIES.map((s) => {
            const vals = points.map((p) => p[s.key]).filter((v) => !Number.isNaN(v));
            const last = points.length ? points[points.length - 1][s.key] : NaN;
            const prev = points.length > 1 ? points[points.length - 2][s.key] : NaN;
            const delta = last - prev;
            let lo = vals.length ? Math.min(...vals) : 0;
            let hi = vals.length ? Math.max(...vals) : 1;
            if (hi === lo) {
              lo -= 1;
              hi += 1;
            }
            const y = (v: number) => PAD + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD);
            const valid = points.filter((p) => !Number.isNaN(p[s.key]));
            const d = valid.map((p, i) => `${i ? 'L' : 'M'}${x(p.version).toFixed(1)} ${y(p[s.key]).toFixed(1)}`).join('');
            const hv = hovered && !Number.isNaN(hovered[s.key]) ? hovered : undefined;
            return (
              <div key={s.key} className="st-card">
                <div className="st-card__head">
                  <span className="st-card__label">{s.label}</span>
                  <span className="st-card__value num">{Number.isNaN(last) ? '—' : s.fmt(last)}</span>
                  {!Number.isNaN(delta) && delta !== 0 && (
                    <span className="st-card__delta num" title="Изменение к предыдущей версии">
                      {delta > 0 ? '▲' : '▼'} {s.fmt(Math.abs(delta))}
                    </span>
                  )}
                </div>
                <svg width={cardW} height={H} className="st-spark" onMouseMove={onMove} onMouseLeave={onLeave} role="img" aria-label={`${s.label} по версиям плана`}>
                  <line className="st-spark__base" x1={0} x2={cardW} y1={H - PAD} y2={H - PAD} />
                  {hv && <line className="st-spark__cross" x1={x(hv.version)} x2={x(hv.version)} y1={0} y2={H} />}
                  {valid.length > 1 && <path className="st-spark__line" d={d} />}
                  {valid.length <= 40 &&
                    valid.map((p) => <circle key={p.version} className="st-spark__dot" cx={x(p.version)} cy={y(p[s.key])} r={2} />)}
                  {valid.length > 0 && (
                    <circle className="st-spark__last" cx={x(valid[valid.length - 1].version)} cy={y(valid[valid.length - 1][s.key])} r={3.5} />
                  )}
                  {hv && <circle className="st-spark__hover" cx={x(hv.version)} cy={y(hv[s.key])} r={4} />}
                </svg>
                <div className="st-card__range num">
                  <span>мин {s.fmt(vals.length ? Math.min(...vals) : 0)}</span>
                  <span>макс {s.fmt(vals.length ? Math.max(...vals) : 0)}</span>
                </div>
              </div>
            );
          })}
      </div>
      {points.length < 2 && <p className="st-hint">Линии появятся после новых версий плана: «+1», «+5» или событий из панели «Данные».</p>}
    </Section>
  );
}

/* ---------------------------------------------------------------- 2. загрузка сотрудников */

interface LoadRow {
  id: string;
  name: string;
  profiles: Profile[];
  shift: number;
  work: number;
  restock: number;
  road: number;
  brk: number;
  idle: number;
  load: number;
  visits: number;
  off: boolean;
  /** На смене и доступен: есть маршрут в плане и он не UNAVAILABLE (absent в план не попадают). */
  working: boolean;
}

function loadRows(plan: Plan, staff: Map<string, StaffInfo>): LoadRow[] {
  const routes = new Map(plan.routes.map((r) => [r.staff_id, r]));
  const ids = new Set([...staff.keys(), ...routes.keys()]);
  const rows: LoadRow[] = [];
  for (const id of ids) {
    const info = staff.get(id);
    const route = routes.get(id);
    let work = 0;
    let restock = 0;
    let road = 0;
    let brk = 0;
    let visits = 0;
    let first = NaN;
    let last = NaN;
    for (const s of route?.stops ?? []) {
      const a = minutesOf(s.start);
      const b = minutesOf(s.end);
      if (Number.isNaN(first) || a < first) first = a;
      if (Number.isNaN(last) || b > last) last = b;
      if (s.type !== 'DEPOT_START') road += (s.travel_sec ?? 0) / 60;
      if (s.type === 'VISIT') {
        work += Math.max(0, b - a);
        visits++;
      } else if (s.type === 'RESTOCK') restock += Math.max(0, b - a);
      else if (s.type === 'BREAK') brk += Math.max(0, b - a);
    }
    let shift = minutesOfHm(info?.shift?.end) - minutesOfHm(info?.shift?.start);
    if (!(shift > 0)) shift = last - first > 0 ? last - first : 12 * 60;
    const used = work + restock + road + brk;
    // Как utilization в плане: (работа + дорога) / чистая смена (без перерыва); берём из totals, если есть.
    const load = route?.totals?.utilization ?? (visits ? (work + road) / Math.max(1, shift - brk) : 0);
    rows.push({
      id,
      name: info?.name ?? id,
      profiles: info?.profiles ?? [],
      shift,
      work,
      restock,
      road,
      brk,
      idle: Math.max(0, shift - used),
      load,
      visits,
      off: route?.status === 'UNAVAILABLE',
      working: route !== undefined && route.status !== 'UNAVAILABLE',
    });
  }
  return rows.sort((a, b) => Number(a.off) - Number(b.off) || b.load - a.load || a.name.localeCompare(b.name, 'ru'));
}

type ProfileFilter = 'ALL' | Profile;

interface ProfileStat {
  profile: Profile;
  n: number;
  mean: number;
  min: number;
  max: number;
  cv: number;
  high: number;
  low: number;
}

/** Сводка по профилю: работающие сотрудники профиля (пустые маршруты с 0, недоступные не считаются). */
function profileStat(rows: LoadRow[], profile: Profile): ProfileStat | null {
  const u = rows.filter((r) => r.working && r.profiles.includes(profile));
  if (!u.length) return null;
  const loads = u.map((r) => r.load);
  const mean = loads.reduce((a, b) => a + b, 0) / loads.length;
  const sd = Math.sqrt(loads.reduce((a, b) => a + (b - mean) ** 2, 0) / loads.length);
  return {
    profile,
    n: u.length,
    mean,
    min: Math.min(...loads),
    max: Math.max(...loads),
    cv: u.length >= 2 && mean > 0 ? sd / mean : 0,
    high: loads.filter((l) => l > 0.9).length,
    low: loads.filter((l) => l < 0.3).length,
  };
}

interface InstallDemand {
  demandMin: number;
  capacityMin: number;
  unassigned: number;
  ratio: number;
}

/** Спрос на инсталляторов (нормативные минуты нарядов, которые умеет только INSTALL: размещённые + нераспределённые)
 *  против ёмкости смен (смена без перерыва) работающих сотрудников с профилем INSTALL. null — данных нет. */
function installDemand(rows: LoadRow[], orders: UiOrder[], workTypes: WorkType[]): InstallDemand | null {
  const only = new Map<string, number>();
  for (const w of workTypes) if (w.profiles.length > 0 && w.profiles.every((p) => p === 'INSTALL')) only.set(w.code, w.duration_min);
  if (!only.size) return null;
  let demandMin = 0;
  let unassigned = 0;
  for (const o of orders) {
    const d = only.get(o.work_type_code);
    if (d === undefined) continue;
    if (o.status === 'ROUTED') demandMin += d;
    else if (o.status === 'UNASSIGNED') {
      demandMin += d;
      unassigned++;
    }
  }
  const capacityMin = rows.filter((r) => r.working && r.profiles.includes('INSTALL')).reduce((n, r) => n + Math.max(0, r.shift - r.brk), 0);
  if (!(capacityMin > 0) || !(demandMin > 0)) return null;
  return { demandMin, capacityMin, unassigned, ratio: demandMin / capacityMin };
}

function StaffLoad({
  plan,
  orders,
  workTypes,
  staff,
  staffSel,
  onSelectStaff,
  showTip,
  hideTip,
}: {
  plan: Plan;
  orders: UiOrder[];
  workTypes: WorkType[];
  staff: Map<string, StaffInfo>;
  staffSel: Selection | null;
  onSelectStaff: (id: string | null) => void;
  showTip: ShowTip;
  hideTip: () => void;
}) {
  const [profile, setProfile] = useState<ProfileFilter>('ALL');
  const [boxRef, w] = useWidth<HTMLDivElement>();
  const all = useMemo(() => loadRows(plan, staff), [plan, staff]);
  const stats = useMemo(() => (['INSTALL', 'LTP3'] as Profile[]).map((p) => profileStat(all, p)).filter((s): s is ProfileStat => !!s), [all]);
  const demand = useMemo(() => installDemand(all, orders, workTypes), [all, orders, workTypes]);
  const sInst = stats.find((s) => s.profile === 'INSTALL');
  const sLtp = stats.find((s) => s.profile === 'LTP3');
  const explain = !!(demand && sInst && sLtp && demand.unassigned > 0 && sInst.mean - sLtp.mean >= 0.1);
  const rows = profile === 'ALL' ? all : all.filter((r) => r.profiles.includes(profile));

  // Разброс загрузки — коэффициент вариации по всем работающим сотрудникам смены, включая пустые
  // маршруты (как utilization_cv в сводке плана); при фильтре профиля — внутри профиля.
  const used = rows.filter((r) => r.working);
  const mean = used.length ? used.reduce((n, r) => n + r.load, 0) / used.length : 0;
  const sd = used.length ? Math.sqrt(used.reduce((n, r) => n + (r.load - mean) ** 2, 0) / used.length) : 0;
  const cv = used.length >= 2 && mean > 0 ? sd / mean : 0;

  const labelW = Math.round(Math.min(190, Math.max(96, w * 0.34)));
  const trackW = Math.max(60, w - labelW - 46 - 12);
  const scale = Math.max(1, ...rows.map((r) => r.shift));
  const sx = (m: number) => (m / scale) * trackW;
  const sel = staffSel?.id ?? null;

  return (
    <Section
      title="Загрузка сотрудников"
      meta={`${rows.length} чел.`}
      aside={
        <SegmentedControl
          size="s"
          variant="secondary"
          value={profile}
          onChange={(v) => setProfile(v as ProfileFilter)}
          items={[
            { index: 'ALL', label: 'Все' },
            { index: 'LTP3', label: 'ЛТП' },
            { index: 'INSTALL', label: 'Инсталл.' },
          ]}
        />
      }
    >
      <div className="st-kpis">
        <div className="st-kpi" title="Средняя загрузка работающих сотрудников, включая пустые маршруты: (работа + дорога) / смена без перерыва">
          <span className="st-kpi__value num">{used.length ? pct(mean) : '—'}</span>
          <span className="st-kpi__label">средняя загрузка</span>
        </div>
        <div className="st-kpi" title="Коэффициент вариации загрузки (σ / среднее): чем меньше, тем ровнее распределены наряды">
          <span className="st-kpi__value num">{used.length >= 2 ? cv.toFixed(2) : '—'}</span>
          <span className="st-kpi__label">разброс загрузки</span>
        </div>
        <ul className="st-legend" aria-label="Обозначения">
          <li><i className="st-sw st-sw--work" />Работа</li>
          <li><i className="st-sw st-sw--road" />Дорога</li>
          <li><i className="st-sw st-sw--break" />Перерыв</li>
          <li><i className="st-sw st-sw--idle" />Простой</li>
        </ul>
      </div>
      {stats.length > 0 && (
        <div className="st-profs">
          {stats.map((s) => (
            <div key={s.profile} className="st-prof" title="Работающие сотрудники профиля (пустые маршруты с 0%, недоступные не считаются); сотрудник с двумя профилями входит в оба">
              <span className="st-prof__name">{PROFILE_SHORT[s.profile]}</span>
              <span className="st-prof__stat num">{s.n} чел.</span>
              <span className="st-prof__stat num">средняя <b>{pct(s.mean)}</b></span>
              <span className="st-prof__stat num">{pct(s.min)}–{pct(s.max)}</span>
              <span className="st-prof__stat num">CV <b>{s.cv.toFixed(2)}</b></span>
              {s.high > 0 && <span className="st-chip st-chip--high num" title="Загрузка выше 90%">&gt;90%: {s.high}</span>}
              {s.low > 0 && <span className="st-chip st-chip--low num" title="Загрузка ниже 30%">&lt;30%: {s.low}</span>}
            </div>
          ))}
        </div>
      )}
      {explain && demand && (
        <p className="st-hint st-why">
          Спрос на инсталляторов выше ёмкости смен: {hm(demand.demandMin)} работы по нарядам только для инсталляторов (размещённым и нераспределённым)
          против {hm(demand.capacityMin)} рабочего времени инсталляторов — в {num(demand.ratio)} раза больше. ЛТП-3 не могут брать инсталляцию (ограничение профиля), поэтому выровнять между
          профилями нельзя.
        </p>
      )}
      <div ref={boxRef} className="st-load" onMouseLeave={hideTip}>
        {!rows.length && <p className="st-hint">Нет сотрудников с таким профилем.</p>}
        {w > 0 &&
          rows.map((r) => {
            const isSel = r.id === sel;
            const segs: { cls: string; m: number }[] = [
              { cls: 'work', m: r.work + r.restock },
              { cls: 'road', m: r.road },
              { cls: 'break', m: r.brk },
            ];
            let at = 0;
            return (
              <button
                key={r.id}
                type="button"
                className={`st-row${isSel ? ' is-selected' : ''}${r.off ? ' is-off' : ''}`}
                aria-pressed={isSel}
                onClick={() => onSelectStaff(isSel ? null : r.id)}
                onMouseEnter={(e) =>
                  showTip(e, r.name, [
                    `${r.profiles.map((p) => PROFILE_SHORT[p]).join(', ') || 'профиль не указан'}${r.off ? ' · недоступен' : ''}`,
                    `Загрузка ${pct(r.load)}, визитов ${r.visits}`,
                    `Работа ${hm(r.work)}${r.restock ? `, пополнение в СЦ ${hm(r.restock)}` : ''}`,
                    `Дорога ${hm(r.road)} · перерыв ${hm(r.brk)}`,
                    `Простой ${hm(r.idle)} из смены ${hm(r.shift)}`,
                  ])
                }
                onMouseMove={(e) => showTip(e, r.name, [])}
              >
                <span className="st-row__name" style={{ width: labelW }}>
                  {r.name}
                </span>
                <svg width={trackW} height={14} className="st-row__bar" aria-hidden>
                  <rect className="st-bar--idle" x={0} y={1} width={Math.max(0, sx(r.shift))} height={12} rx={3} />
                  {segs.map((s) => {
                    const wpx = sx(s.m);
                    const x0 = sx(at);
                    at += s.m;
                    // 2px разрыв между сегментами (линия поверхности), чтобы границы читались без контура.
                    return wpx >= 1 ? <rect key={s.cls} className={`st-bar--${s.cls}`} x={x0} y={1} width={Math.max(1, wpx - 2)} height={12} rx={2} /> : null;
                  })}
                </svg>
                <span className="st-row__pct num">{r.off ? '—' : pct(r.load)}</span>
              </button>
            );
          })}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- 3. нераспределённые по причинам */

function Reasons({
  plan,
  orders,
  orderSel,
  onSelectOrder,
  showTip,
  hideTip,
}: {
  plan: Plan;
  orders: UiOrder[];
  orderSel: Selection | null;
  onSelectOrder: (id: string) => void;
  showTip: ShowTip;
  hideTip: () => void;
}) {
  const [boxRef, w] = useWidth<HTMLDivElement>();
  const groups = useMemo(() => {
    const slot = new Map(orders.map((o) => [o.order_id, o.slot.start]));
    const m = new Map<UnassignedReason, string[]>();
    for (const u of plan.unassigned) (m.get(u.reason_code) ?? m.set(u.reason_code, []).get(u.reason_code)!).push(u.order_id);
    for (const ids of m.values()) ids.sort((a, b) => (slot.get(a) ?? '').localeCompare(slot.get(b) ?? ''));
    return [...m.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [plan.unassigned, orders]);
  const total = plan.unassigned.length;
  const pgp = plan.pgp_groups.reduce((n, g) => n + g.order_ids.length, 0);
  const max = Math.max(1, ...groups.map(([, ids]) => ids.length));
  const labelW = Math.round(Math.min(210, Math.max(110, w * 0.42)));
  const trackW = Math.max(40, w - labelW - 70 - 12);
  const sel = orderSel?.id ?? null;

  return (
    <Section title="Нераспределённые по причинам" meta={pgp ? `ещё ${pgp} в группах ПГП` : undefined}>
      <div className="st-reasons">
        <div className="st-hero">
          <span className="st-hero__value num">{total}</span>
          <span className="st-hero__label">{total ? 'не распределено' : 'все наряды распределены'}</span>
        </div>
        <div ref={boxRef} className="st-reasons__bars" onMouseLeave={hideTip}>
          {w > 0 &&
            groups.map(([code, ids]) => {
              const active = !!sel && ids.includes(sel);
              const label = REASON[code] ?? code;
              const select = () => {
                // Повторный клик проходит по нарядам причины по порядку слотов.
                const i = sel ? ids.indexOf(sel) : -1;
                onSelectOrder(ids[(i + 1) % ids.length]);
              };
              return (
                <button
                  key={code}
                  type="button"
                  className={`st-row st-row--reason${active ? ' is-selected' : ''}`}
                  aria-pressed={active}
                  onClick={select}
                  onMouseEnter={(e) =>
                    showTip(e, label, [
                      `${ids.length} из ${total} (${pct(ids.length / Math.max(1, total))})`,
                      ids.slice(0, 4).join(', ') + (ids.length > 4 ? ` и ещё ${ids.length - 4}` : ''),
                      'Клик — выбрать наряд в списке, повторный — следующий',
                    ])
                  }
                  onMouseMove={(e) => showTip(e, label, [])}
                >
                  <span className="st-row__name" style={{ width: labelW }}>
                    {label}
                  </span>
                  <svg width={trackW} height={14} className="st-row__bar" aria-hidden>
                    <rect className="st-bar--reason" x={0} y={1} width={Math.max(2, (ids.length / max) * trackW)} height={12} rx={2} />
                  </svg>
                  <span className="st-row__pct num">
                    {ids.length} <small>{pct(ids.length / Math.max(1, total))}</small>
                  </span>
                </button>
              );
            })}
        </div>
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- 4. прогноз длительности */

function Forecast({
  plan,
  orderSel,
  onSelectOrder,
  showTip,
  hideTip,
}: {
  plan: Plan;
  orderSel: Selection | null;
  onSelectOrder: (id: string) => void;
  showTip: ShowTip;
  hideTip: () => void;
}) {
  const [boxRef, w] = useWidth<HTMLDivElement>();
  const [model, setModel] = useState<ForecastModel | null>(null);
  const [risk, setRisk] = useState<ForecastRisk | null>(null);
  const [riskOff, setRiskOff] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api.forecastModel().then((m) => alive && setModel(m)).catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);
  const modelOk = !!model?.available;
  useEffect(() => {
    let alive = true;
    if (!modelOk) {
      setRisk(null);
      return;
    }
    api
      .forecastRisk(plan.date)
      .then((r) => {
        if (!alive) return;
        setRisk(r.available ? r : null);
        setRiskOff(!r.available);
      })
      .catch((e) => {
        if (!alive) return;
        setRisk(null);
        setRiskOff(isUnavailable(e));
      });
    return () => {
      alive = false;
    };
  }, [plan.date, plan.plan_version, modelOk]);

  const rows = model?.rows ?? [];
  const max = Math.max(1, ...rows.map((r) => r.p80_min));
  const labelW = Math.round(Math.min(190, Math.max(96, w * 0.34)));
  const trackW = Math.max(60, w - labelW - 70 - 12);
  const sx = (m: number) => (m / max) * trackW;
  const q = model?.quality;
  const sel = orderSel?.id ?? null;

  return (
    <Section
      title="Прогноз длительности"
      meta={modelOk ? `по ${model!.n_total} выполненным нарядам` : undefined}
      aside={
        <span className="st-note" title="Прогноз по истории нарядов — только информация: в плане длительность визита остаётся нормативной (её проверяет валидатор)">
          план — по нормативу
        </span>
      }
    >
      {failed && <p className="st-hint">Прогноз недоступен: не удалось обучить модель.</p>}
      {!model && !failed && <p className="st-hint">Загрузка…</p>}
      {model && !model.available && (
        <p className="st-hint" title={model.reason}>
          Недостаточно истории для прогноза
          {model.reason && model.reason !== 'Недостаточно истории для прогноза' ? `: ${model.reason.toLowerCase()}` : ''}.
          Прогноз появится, когда накопятся выполненные наряды (или задайте файл истории HISTORY_FILE).
        </p>
      )}
      {modelOk && (
        <>
          <ul className="st-legend st-legend--left" aria-label="Обозначения">
            <li><i className="st-sw st-sw--idle" />Норматив</li>
            <li><i className="st-sw st-sw--work" />Среднее</li>
            <li><i className="st-sw st-sw--road" />p80</li>
          </ul>
          <div ref={boxRef} className="st-load" onMouseLeave={hideTip}>
            {w > 0 &&
              rows.map((r) => (
                <div
                  key={r.work_type_code}
                  className="st-row st-row--static"
                  onMouseEnter={(e) =>
                    showTip(e, r.work_type_name, [
                      `Норматив ${r.normative_min} мин · среднее ${num(r.mean_min)} (${r.deviation_pct > 0 ? '+' : ''}${num(r.deviation_pct)}%)`,
                      `Медиана ${num(r.median_min)} · p80 ${num(r.p80_min)} мин · n = ${r.n}`,
                    ])
                  }
                  onMouseMove={(e) => showTip(e, r.work_type_name, [])}
                >
                  <span className="st-row__name" style={{ width: labelW }}>
                    {r.work_type_name}
                  </span>
                  <svg width={trackW} height={14} className="st-row__bar" aria-hidden>
                    <rect className="st-bar--idle" x={0} y={1} width={sx(r.normative_min)} height={12} rx={2} />
                    <rect className="st-bar--road" x={0} y={5} width={sx(r.p80_min)} height={4} rx={1} />
                    <rect className="st-bar--work" x={0} y={3} width={sx(r.mean_min)} height={8} rx={2} />
                  </svg>
                  <span className="st-row__pct num">
                    {num(r.mean_min)} <small>/ {num(r.p80_min)}</small>
                  </span>
                </div>
              ))}
          </div>
          {q?.available && (
            <p
              className="st-hint num"
              title={`Обучение на первых днях, проверка на последних (${q.test_days?.join(', ')}); n = ${q.n_train} / ${q.n_test}. Покрытие p80: ${pct(q.p80_coverage ?? 0)} против ${pct(q.normative_coverage ?? 0)} у норматива.`}
            >
              Ошибка: норматив {num(q.mae_normative_min ?? 0)} мин → прогноз {num(q.mae_forecast_min ?? 0)} мин (на отложенных днях)
              {Math.abs(q.improvement_pct ?? 0) < 2 ? ' — разницы почти нет' : ''}
            </p>
          )}
          {!risk && riskOff && <p className="st-hint">Риск опоздания при p80 недоступен для этого дня.</p>}
          {risk && (
            <div className="st-risk">
              <div className="st-hero" title="Если каждый визит затянется до p80, у этих нарядов накопленная задержка уведёт начало за latest_start">
                <span className="st-hero__value num">{risk.at_risk}</span>
                <span className="st-hero__label">под риском при p80</span>
              </div>
              {risk.orders.length > 0 && (
                <div className="st-reasons__bars st-risk__list" onMouseLeave={hideTip}>
                  {risk.orders.slice(0, 12).map((o) => (
                    <button
                      key={o.order_id}
                      type="button"
                      className={`st-row${o.order_id === sel ? ' is-selected' : ''}`}
                      aria-pressed={o.order_id === sel}
                      onClick={() => onSelectOrder(o.order_id)}
                      onMouseEnter={(e) => showTip(e, o.order_id, [o.reason, `Исполнитель ${o.staff_id}, latest_start ${hhmm(o.latest_start)}`])}
                      onMouseMove={(e) => showTip(e, o.order_id, [])}
                    >
                      <span className="st-row__name" style={{ flex: 1 }}>{o.order_id}</span>
                      <span className="st-row__pct num">+{Math.round(o.accumulated_delay_min)} мин</span>
                    </button>
                  ))}
                  {risk.orders.length > 12 && <p className="st-hint">и ещё {risk.orders.length - 12}</p>}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- окно */

export function StatsPanel({
  plan,
  orders,
  workTypes = [],
  staff,
  session,
  staffSel,
  orderSel,
  onSelectStaff,
  onSelectOrder,
}: {
  plan: Plan;
  orders: UiOrder[];
  /** Справочник типов работ (профили, норматив) из /ui reference. */
  workTypes?: WorkType[];
  staff: Map<string, StaffInfo>;
  session: MetricPoint[];
  staffSel: Selection | null;
  orderSel: Selection | null;
  onSelectStaff: (id: string | null) => void;
  onSelectOrder: (id: string) => void;
}) {
  const [tip, setTip] = useState<Tip | null>(null);
  // Пустые lines при движении мыши — только сдвиг уже показанного тултипа.
  const showTip: ShowTip = (e, title, lines) =>
    setTip((t) => (lines.length || !t ? { x: e.clientX, y: e.clientY, title, lines } : { ...t, x: e.clientX, y: e.clientY }));
  const hideTip = () => setTip(null);

  return (
    <div className="stats" onScroll={hideTip}>
      <Dynamics plan={plan} session={session} showTip={showTip} hideTip={hideTip} />
      <Reasons plan={plan} orders={orders} orderSel={orderSel} onSelectOrder={onSelectOrder} showTip={showTip} hideTip={hideTip} />
      <StaffLoad plan={plan} orders={orders} workTypes={workTypes} staff={staff} staffSel={staffSel} onSelectStaff={onSelectStaff} showTip={showTip} hideTip={hideTip} />
      <Forecast plan={plan} orderSel={orderSel} onSelectOrder={onSelectOrder} showTip={showTip} hideTip={hideTip} />
      <TipView tip={tip} />
    </div>
  );
}
