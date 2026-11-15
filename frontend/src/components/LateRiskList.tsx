import { useRef } from 'react';
import { Badge } from './ui';
import { ACTION, hhmm, windowWithDay } from '../format';
import type { Plan, UiOrder } from '../types/api';
import type { Selection } from '../planModel';
import { useScrollToSelected } from '../hooks/useScrollToSelected';
import { useStaffName } from '../hooks/useStaffDirectory';

interface Props {
  plan: Plan;
  orders: UiOrder[];
  selected: Selection | null;
  onSelect: (id: string) => void;
}

export function LateRiskList({ plan, orders, selected, onSelect }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const staffName = useStaffName();
  useScrollToSelected(boxRef, selected, 'late');
  const byId = new Map(orders.map((o) => [o.order_id, o]));
  const all = plan.alerts.filter((a) => a.type === 'LATE_RISK');
  const active = all
    .filter((a) => a.status === 'ACTIVE')
    .sort((a, b) => (b.lateness_min ?? 0) - (a.lateness_min ?? 0));
  const resolved = all.length - active.length;

  return (
    <div className="list">
      <div className="toolbar toolbar--note">
        Активных: <b className="num">{active.length}</b>, закрыто за день: <span className="num">{resolved}</span>
      </div>
      <div className="scroll" ref={boxRef}>
        {!active.length && (
          <p className="empty">Опозданий не прогнозируется. Риски появятся после событий: задержек визитов и выбытия сотрудников.</p>
        )}
        <ul className="cards">
          {active.map((a) => {
            const o = a.order_id ? byId.get(a.order_id) : undefined;
            return (
              <li
                key={a.alert_id}
                data-order={a.order_id ?? undefined}
                className={`card card--warn card--select${a.order_id && selected?.id === a.order_id ? ' is-selected' : ''}`}
                onClick={() => a.order_id && onSelect(a.order_id)}
              >
                <div className="card__head">
                  <span className="mono-id">{a.order_id}</span>
                  <span className="late num">+{a.lateness_min ?? '?'} мин</span>
                </div>
                <div className="card__grid">
                  <span>Исполнитель</span>
                  <b title={a.staff_id ?? undefined}>{staffName(a.staff_id ?? o?.staff_id, o?.staff_name)}</b>
                  <span>Прогноз начала</span>
                  <b className="num">{hhmm(a.forecast_start)}</b>
                  <span>Начать не позже</span>
                  <b className="num">{hhmm(o?.latest_start)}</b>
                  <span>Слот</span>
                  <b className="num">{o ? windowWithDay(o.slot, plan.date) : '—'}</b>
                </div>
                <div className="card__action">
                  <Badge variant="secondary" size="s" colorScheme="warning" label={ACTION[a.suggested_action]} />
                  {a.suggested_action === 'REASSIGN' && a.suggested_staff_id && <span title={a.suggested_staff_id}>→ {staffName(a.suggested_staff_id)}</span>}
                  {a.suggested_action === 'RESCHEDULE' && a.proposed_slot && (
                    <span className="num">на {windowWithDay(a.proposed_slot, plan.date)}</span>
                  )}
                  {a.applied && <Badge variant="secondary" size="2xs" colorScheme="success" label="применено" />}
                </div>
                <p className="card__text">{a.message}</p>
                <p className="card__meta num">Выявлено в {hhmm(a.created_at)}</p>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
