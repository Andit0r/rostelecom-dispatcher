import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { createPortal } from 'react-dom';
import type { Plan, Route, ServiceCenter, Stop, UiOrder } from '../types/api';
import { PLAN_TIME_HINT, WORK_TYPE_SHORT, hhmm, window2 } from '../format';
import { PROFILE_SHORT, jointMainOf, jointPairs, minutesOf, minutesOfHm, routeVar, type Selection, type StaffInfo } from '../planModel';

/** Высота строки и ширина колонки с именем, px. */
const ROW = 26;
const LABEL = 184;
const OVERSCAN = 6;
const SHIFT_FROM = 9 * 60;
const SHIFT_TO = 21 * 60;

type Row =
  | { kind: 'group'; key: string; label: string; count: number }
  | { kind: 'staff'; key: string; info: StaffInfo; route?: Route };

interface Tip {
  x: number;
  y: number;
  title: string;
  lines: string[];
  warn?: string;
}

interface Props {
  plan: Plan;
  orders: UiOrder[];
  staff: Map<string, StaffInfo>;
  serviceCenters: ServiceCenter[];
  staffSel: Selection | null;
  orderSel: Selection | null;
  onSelectStaff: (id: string | null, fit: boolean) => void;
  onSelectOrder: (id: string) => void;
}

const STOP_STATUS: Record<string, string> = { PLANNED: 'запланирован', IN_PROGRESS: 'в работе', DONE: 'выполнен' };

export function GanttChart({ plan, orders, staff, serviceCenters, staffSel, orderSel, onSelectStaff, onSelectOrder }: Props) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [top, setTop] = useState(0);
  const [tip, setTip] = useState<Tip | null>(null);

  const ordersById = useMemo(() => new Map(orders.map((o) => [o.order_id, o])), [orders]);
  const pairs = useMemo(() => jointPairs(plan), [plan]);

  const rows = useMemo<Row[]>(() => {
    const routes = new Map(plan.routes.map((r) => [r.staff_id, r]));
    const scName = new Map(serviceCenters.map((s) => [s.service_center_id, s.name]));
    const ids = new Set([...staff.keys(), ...routes.keys()]);
    const by = new Map<string, Row[]>();
    for (const id of ids) {
      const route = routes.get(id);
      const info = staff.get(id) ?? { id, name: id, scId: route?.service_center_id ?? '', profiles: [], color: 0 };
      const sc = route?.service_center_id ?? info.scId;
      (by.get(sc) ?? by.set(sc, []).get(sc)!).push({ kind: 'staff', key: id, info, route });
    }
    const res: Row[] = [];
    [...by.entries()]
      .sort((a, b) => (scName.get(a[0]) ?? a[0]).localeCompare(scName.get(b[0]) ?? b[0], 'ru'))
      .forEach(([sc, list]) => {
        res.push({ kind: 'group', key: `g-${sc}`, label: scName.get(sc) ?? sc, count: list.length });
        list.sort((a, b) => (a.kind === 'staff' && b.kind === 'staff' ? a.info.name.localeCompare(b.info.name, 'ru') : 0));
        res.push(...list);
      });
    return res;
  }, [plan.routes, staff, serviceCenters]);

  // Шкала смены 09:00–21:00; расширяем до целого часа, если план выходит за неё.
  const [t0, t1] = useMemo(() => {
    let lo = SHIFT_FROM;
    let hi = SHIFT_TO;
    for (const r of plan.routes)
      for (const s of r.stops) {
        const a = minutesOf(s.arrival) - (s.travel_sec ?? 0) / 60;
        const e = minutesOf(s.end);
        if (a < lo) lo = Math.floor(a / 60) * 60;
        if (e > hi) hi = Math.ceil(e / 60) * 60;
      }
    return [Math.max(0, lo), Math.min(24 * 60, hi)];
  }, [plan.routes]);

  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const trackW = Math.max(240, size.w - LABEL - 12);
  const span = t1 - t0;
  const x = (m: number) => ((Math.min(Math.max(m, t0), t1) - t0) / span) * trackW;
  const hours = Array.from({ length: span / 60 + 1 }, (_, i) => t0 + i * 60);
  const gridLines = (
    <g key="grid" className="g-grid" aria-hidden>
      {hours.map((h) => (
        <line key={h} x1={x(h)} x2={x(h)} y1={0} y2={ROW} />
      ))}
    </g>
  );
  const now = minutesOf(plan.as_of);
  const nowX = now >= t0 && now <= t1 ? x(now) : null;

  // Прокрутка к строке выбранного сотрудника (выбор пришёл из таблицы, списков или карты).
  const scrollToStaff = (staffId: string | null | undefined) => {
    const el = bodyRef.current;
    if (!el || !staffId) return;
    const i = rows.findIndex((r) => r.kind === 'staff' && r.info.id === staffId);
    if (i < 0) return;
    const y = i * ROW;
    if (y < el.scrollTop + ROW || y + ROW > el.scrollTop + el.clientHeight - ROW) {
      el.scrollTop = Math.max(0, y - el.clientHeight / 2 + ROW / 2);
    }
  };
  useEffect(() => {
    if (orderSel && orderSel.source !== 'gantt') scrollToStaff(ordersById.get(orderSel.id)?.staff_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderSel?.nonce]);
  useEffect(() => {
    if (staffSel && staffSel.source !== 'gantt') scrollToStaff(staffSel.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [staffSel?.nonce]);

  const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const last = Math.min(rows.length, Math.ceil((top + size.h) / ROW) + OVERSCAN);

  const showTip = (e: ReactMouseEvent, t: Omit<Tip, 'x' | 'y'>) => setTip({ ...t, x: e.clientX, y: e.clientY });
  const moveTip = (e: ReactMouseEvent) => setTip((t) => (t ? { ...t, x: e.clientX, y: e.clientY } : t));
  const hideTip = () => setTip(null);

  const selectedStaff = staffSel?.id ?? null;
  const selectedOrder = orderSel?.id ?? null;
  const visits = plan.routes.reduce((n, r) => n + r.stops.filter((s) => s.type === 'VISIT' && !jointMainOf(s.order_id)).length, 0);

  function renderStops(r: Route, info: StaffInfo) {
    const out: JSX.Element[] = [];
    const mid = 11;
    const barY = 4;
    const barH = 15;
    const shiftFrom = minutesOfHm(info.shift?.start);
    const shiftTo = minutesOfHm(info.shift?.end);
    if (!Number.isNaN(shiftFrom) && !Number.isNaN(shiftTo)) {
      out.push(<rect key="shift" className="g-shift" x={x(shiftFrom)} y={1} width={Math.max(0, x(shiftTo) - x(shiftFrom))} height={ROW - 2} />);
    }
    out.push(gridLines);
    if (r.status === 'UNAVAILABLE' || r.unavailable_from) {
      const from = minutesOf(r.unavailable_from);
      const until = minutesOf(r.unavailable_until);
      const a = Number.isNaN(from) ? t0 : from;
      const b = Number.isNaN(until) ? (Number.isNaN(shiftTo) ? t1 : shiftTo) : until;
      out.push(
        <rect
          key="off"
          className="g-off"
          x={x(a)}
          y={2}
          width={Math.max(2, x(b) - x(a))}
          height={ROW - 4}
          onMouseEnter={(e) =>
            showTip(e, {
              title: `${info.name}: недоступен`,
              lines: [`С ${hhmm(r.unavailable_from)}${r.unavailable_until ? ` до ${hhmm(r.unavailable_until)}` : ' до конца смены'}`],
            })
          }
          onMouseMove={moveTip}
          onMouseLeave={hideTip}
        />,
      );
    }
    r.stops.forEach((s: Stop) => {
      const k = `${s.seq}-${s.type}`;
      const arr = minutesOf(s.arrival);
      const st = minutesOf(s.start);
      const en = minutesOf(s.end);
      if (s.travel_sec && s.travel_sec > 0 && s.type !== 'DEPOT_START') {
        const a = arr - s.travel_sec / 60;
        out.push(
          <rect
            key={`t${k}`}
            className="g-travel"
            x={x(a)}
            y={mid - 2.5}
            width={Math.max(1, x(arr) - x(a))}
            height={5}
            onMouseEnter={(e) =>
              showTip(e, {
                title: 'Дорога',
                lines: [
                  `${Math.round(s.travel_sec! / 60)} мин${s.distance_m ? `, ${(s.distance_m / 1000).toFixed(1)} км` : ''}`,
                  `Прибытие ${hhmm(s.arrival)}`,
                ],
              })
            }
            onMouseMove={moveTip}
            onMouseLeave={hideTip}
          />,
        );
      }
      if (st - arr > 0.5 && (s.type === 'VISIT' || s.type === 'RESTOCK')) {
        out.push(<line key={`w${k}`} className="g-wait" x1={x(arr)} x2={x(st)} y1={mid} y2={mid} />);
      }
      if (s.type === 'VISIT') {
        // визит напарника совместного наряда (ORD-…-B) показывает основной наряд и выбирает его
        const partnerOf = jointMainOf(s.order_id);
        const id = partnerOf ?? s.order_id;
        const pair = id ? pairs.get(id) : undefined;
        const o = id ? ordersById.get(id) : undefined;
        if (o) {
          const sa = x(minutesOf(o.slot.start));
          const sb = x(minutesOf(o.slot.end));
          out.push(
            <path key={`s${k}`} className="g-slot" d={`M${sa} ${ROW - 5}v3H${sb}v-3`} />,
          );
        }
        const late = !!o?.late_risk;
        const status = (s.status ?? 'PLANNED').toLowerCase().replace('_', '-');
        const w = Math.max(3, x(en) - x(st));
        const cls = `g-visit g-visit--${status}${late ? ' g-visit--late' : ''}${pair ? ' g-visit--joint' : ''}${id === selectedOrder ? ' is-selected' : ''}`;
        out.push(
          <g
            key={`v${k}`}
            className={cls}
            onClick={() => id && onSelectOrder(id)}
            onMouseEnter={(e) =>
              showTip(e, {
                title: `${id ?? 'Визит'}${o ? ` · ${WORK_TYPE_SHORT[o.work_type_code]}` : ''}${pair ? ' · совместный' : ''}`,
                lines: [
                  ...(o ? [o.address.replace(/^г\. Екатеринбург,\s*/, ''), `Слот ${window2(o.slot)}`] : []),
                  ...(pair
                    ? [partnerOf ? `Напарник; основной исполнитель ${pair.main ?? '—'}` : `Напарник ${pair.partner ?? '—'} в то же время`]
                    : []),
                  `Прибытие ${hhmm(s.arrival)}, работа ${hhmm(s.start)}–${hhmm(s.end)}`,
                  `Статус: ${STOP_STATUS[s.status ?? 'PLANNED'] ?? s.status}`,
                ],
                warn: late
                  ? `Риск опоздания${o?.late_risk?.lateness_min != null ? `: +${Math.round(o.late_risk.lateness_min)} мин` : ''}`
                  : undefined,
              })
            }
            onMouseMove={moveTip}
            onMouseLeave={hideTip}
          >
            <rect x={x(st)} y={barY} width={w} height={barH} rx={3} />
            {w > 34 && s.order_id && (
              <text x={x(st) + 4} y={barY + barH / 2} dy="0.35em">
                {(id ?? s.order_id).slice(-4)}
                {pair ? ' ×2' : ''}
              </text>
            )}
          </g>,
        );
      }
      if (s.type === 'BREAK' || s.type === 'RESTOCK') {
        const isBreak = s.type === 'BREAK';
        out.push(
          <rect
            key={`b${k}`}
            className={isBreak ? 'g-break' : 'g-restock'}
            x={x(st)}
            y={barY + 2}
            width={Math.max(3, x(en) - x(st))}
            height={barH - 4}
            rx={2}
            onMouseEnter={(e) =>
              showTip(e, {
                title: isBreak ? 'Перерыв' : 'Пополнение оборудования в СЦ',
                lines: [`${hhmm(s.start)}–${hhmm(s.end)}`, ...(s.explanation ? [s.explanation] : [])],
              })
            }
            onMouseMove={moveTip}
            onMouseLeave={hideTip}
          />,
        );
      }
    });
    return out;
  }

  return (
    <section className="panel gantt" aria-label="Диаграмма загрузки сотрудников">
      <svg width="0" height="0" className="gantt__defs" aria-hidden>
        <defs>
          <pattern id="g-hatch-road" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="5" height="5" className="g-hatch-road-bg" />
            <line x1="0" y1="0" x2="0" y2="5" className="g-hatch-road-line" />
          </pattern>
          <pattern id="g-hatch-off" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <rect width="6" height="6" className="g-hatch-off-bg" />
            <line x1="0" y1="0" x2="0" y2="6" className="g-hatch-off-line" />
          </pattern>
        </defs>
      </svg>

      <div className="gantt__head">
        <span className="gantt__meta num">
          {plan.routes.length} маршрутов, {visits} визитов
        </span>
        <ul className="gantt__legend" aria-label="Обозначения">
          <li><i className="lg lg--visit" />Визит</li>
          <li><i className="lg lg--progress" />В работе</li>
          <li><i className="lg lg--done" />Выполнен</li>
          <li><i className="lg lg--late" />Риск опоздания</li>
          <li><i className="lg lg--break" />Перерыв</li>
          <li><i className="lg lg--restock" />Пополнение в СЦ</li>
          <li><i className="lg lg--road" />Дорога</li>
          <li><i className="lg lg--slot" />Слот</li>
          <li><i className="lg lg--off" />Недоступен</li>
        </ul>
      </div>

      <div className="gantt__axis" aria-hidden>
        <div className="gantt__axis-label" style={{ width: LABEL }}>
          Сотрудник
        </div>
        <svg width={trackW} height={22} className="gantt__axis-svg">
          {hours.map((h) => (
            <g key={h} transform={`translate(${x(h)},0)`}>
              <line y1={16} y2={22} className="g-tick" />
              <text y={11} textAnchor={h === t0 ? 'start' : h === t1 ? 'end' : 'middle'}>
                {String(Math.floor(h / 60)).padStart(2, '0')}:00
              </text>
            </g>
          ))}
          {nowX != null && (
            <g transform={`translate(${nowX},0)`} className="g-now-label">
              <title>{`Время плана: ${PLAN_TIME_HINT.toLowerCase()}`}</title>
              <rect x={-21} y={0} width={42} height={15} rx={3} />
              <text y={11} textAnchor="middle">
                {hhmm(plan.as_of)}
              </text>
            </g>
          )}
        </svg>
      </div>

      <div ref={bodyRef} className="gantt__body" onScroll={(e) => setTop(e.currentTarget.scrollTop)} onMouseLeave={hideTip}>
        {!rows.length && <p className="empty">В плане нет сотрудников.</p>}
        <div className="gantt__canvas" style={{ height: rows.length * ROW }}>
          {rows.slice(first, last).map((row, j) => {
            const i = first + j;
            if (row.kind === 'group') {
              return (
                <div key={row.key} className="gantt__group" style={{ top: i * ROW, height: ROW }}>
                  {row.label}
                  <span className="num">{row.count}</span>
                </div>
              );
            }
            const { info, route } = row;
            const sel = info.id === selectedStaff;
            const off = route?.status === 'UNAVAILABLE';
            return (
              <div
                key={row.key}
                className={`gantt__row${sel ? ' is-selected' : ''}`}
                style={{ top: i * ROW, height: ROW }}
              >
                <button
                  type="button"
                  className="gantt__who"
                  style={{ width: LABEL }}
                  aria-pressed={sel}
                  title={sel ? 'Снять выбор маршрута' : 'Показать маршрут на карте'}
                  onClick={() => onSelectStaff(sel ? null : info.id, !sel)}
                >
                  <span className="gantt__swatch" style={{ background: routeVar(info.color) }} />
                  <span className="gantt__name">{info.name}</span>
                  <span className="gantt__profile">
                    {off ? 'недоступен' : info.profiles.map((p) => PROFILE_SHORT[p]).join(', ')}
                  </span>
                </button>
                <svg width={trackW} height={ROW} className="gantt__track">
                  {route ? renderStops(route, info) : gridLines}
                </svg>
              </div>
            );
          })}
          {nowX != null && <div className="gantt__now" style={{ left: LABEL + nowX }} aria-hidden />}
        </div>
      </div>

      {/* Портал в body: контейнер панели dockview создаёт свой контекст (transform/contain),
          внутри него position: fixed считался бы от панели и обрезался. */}
      {tip &&
        createPortal(
        <div
          className="gantt-tip"
          role="tooltip"
          style={{
            left: Math.min(tip.x + 14, window.innerWidth - 300),
            top: tip.y > window.innerHeight - 170 ? tip.y - 12 : tip.y + 16,
            transform: tip.y > window.innerHeight - 170 ? 'translateY(-100%)' : undefined,
          }}
        >
          <strong>{tip.title}</strong>
          {tip.lines.map((l, i) => (
            <span key={i}>{l}</span>
          ))}
          {tip.warn && <span className="gantt-tip__warn">{tip.warn}</span>}
        </div>,
          document.body,
        )}
    </section>
  );
}
