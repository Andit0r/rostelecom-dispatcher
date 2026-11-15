import { useRef } from 'react';
import { Badge } from './ui';
import { ACTION, REASON, WORK_TYPE_SHORT, hhmm, window2, windowWithDay } from '../format';
import type { Plan, UiOrder, UiReference } from '../types/api';
import type { Selection } from '../planModel';
import { useScrollToSelected } from '../hooks/useScrollToSelected';

export function UnassignedList({
  plan,
  orders,
  reference,
  selected,
  onSelect,
}: {
  plan: Plan;
  orders: UiOrder[];
  reference: UiReference | null;
  selected: Selection | null;
  onSelect: (id: string) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  useScrollToSelected(boxRef, selected, 'unassigned');
  const byId = new Map(orders.map((o) => [o.order_id, o]));
  const nodes = new Map((reference?.access_nodes ?? []).map((n) => [n.access_node_id, n]));
  const unassigned = [...plan.unassigned].sort((a, b) =>
    (byId.get(a.order_id)?.slot.start ?? '').localeCompare(byId.get(b.order_id)?.slot.start ?? ''),
  );

  return (
    <div className="list">
      <div className="split scroll" ref={boxRef}>
        <section className="split__col">
          <h3 className="section-title">
            Группы ПГП <span className="num">{plan.pgp_groups.length}</span>
          </h3>
          {!plan.pgp_groups.length && (
            <p className="empty">Групповых проблем не выявлено: нет трёх неисправностей с одного узла за час.</p>
          )}
          <ul className="cards">
            {plan.pgp_groups.map((g) => {
              const node = nodes.get(g.access_node_id);
              return (
                <li key={g.group_id} className="card card--pgp">
                  <div className="card__head">
                    <span className="mono-id">{g.group_id}</span>
                    <Badge variant="secondary" size="2xs" colorScheme="status-01" label={ACTION[g.action]} />
                  </div>
                  <div className="card__grid">
                    <span>Узел доступа</span>
                    <b>
                      {g.access_node_id}
                      {node && <span className="sub">{node.name}</span>}
                    </b>
                    <span>Выявлено</span>
                    <b className="num">{hhmm(g.detected_at)}</b>
                    <span>Нарядов</span>
                    <b className="num">{g.order_ids.length}</b>
                  </div>
                  <ul className="chips">
                    {g.order_ids.map((id) => {
                      const o = byId.get(id);
                      return (
                        <li
                          key={id}
                          data-order={id}
                          title={o?.address}
                          className={selected?.id === id ? 'is-selected' : undefined}
                          onClick={() => onSelect(id)}
                        >
                          {id.slice(-4)}
                          {o && <span className="sub num">{hhmm(o.registered_at)}</span>}
                        </li>
                      );
                    })}
                  </ul>
                  {g.comment && <p className="card__text">{g.comment}</p>}
                </li>
              );
            })}
          </ul>
        </section>

        <section className="split__col">
          <h3 className="section-title">
            Не распределено <span className="num">{unassigned.length}</span>
          </h3>
          {!unassigned.length && <p className="empty">Все наряды дня распределены по маршрутам.</p>}
          <ul className="cards">
            {unassigned.map((u) => {
              const o = byId.get(u.order_id);
              return (
                <li
                  key={u.order_id}
                  data-order={u.order_id}
                  className={`card card--select${selected?.id === u.order_id ? ' is-selected' : ''}`}
                  onClick={() => onSelect(u.order_id)}
                >
                  <div className="card__head">
                    <span className="mono-id">{u.order_id}</span>
                    <Badge variant="secondary" size="2xs" colorScheme="error" label={REASON[u.reason_code]} />
                  </div>
                  {o && (
                    <div className="card__line">
                      {WORK_TYPE_SHORT[o.work_type_code]}, {o.technology}
                      {o.is_repeat && <span className="flag">повт.</span>}
                      <span className="num"> · слот {window2(o.slot)}</span>
                    </div>
                  )}
                  {o && <div className="card__line card__line--muted">{o.address}</div>}
                  <p className="card__text">{u.reason_text}</p>
                  <div className="card__action">
                    <Badge variant="secondary" size="s" colorScheme="info" label={ACTION[u.suggested_action]} />
                    {u.proposed_slot && <span className="num">на {windowWithDay(u.proposed_slot, plan.date)}</span>}
                    {u.since && <span className="card__meta num">с {hhmm(u.since)}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </div>
  );
}
