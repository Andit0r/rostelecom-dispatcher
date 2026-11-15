import { useEffect, useMemo, useRef, useState } from 'react';
import { Badge, SegmentedControl } from './ui';
import { STATUS, WORK_TYPE_SHORT, hhmm, window2 } from '../format';
import type { UiOrder, UiOrderStatus, WorkTypeCode } from '../types/api';
import type { Selection } from '../planModel';
import { useScrollToSelected } from '../hooks/useScrollToSelected';

type Filter = 'ALL' | UiOrderStatus;

interface Props {
  orders: UiOrder[];
  selected: Selection | null;
  onSelect: (id: string) => void;
}

export function OrdersTable({ orders, selected, onSelect }: Props) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<Filter>('ALL');
  const [type, setType] = useState<'ALL' | WorkTypeCode>('ALL');
  const boxRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return orders
      .filter((o) => status === 'ALL' || o.status === status)
      .filter((o) => type === 'ALL' || o.work_type_code === type)
      .filter(
        (o) =>
          !q ||
          o.order_id.toLowerCase().includes(q) ||
          o.address.toLowerCase().includes(q) ||
          (o.staff_id ?? '').toLowerCase().includes(q) ||
          (o.staff_name ?? '').toLowerCase().includes(q),
      )
      .sort((a, b) => a.slot.start.localeCompare(b.slot.start) || a.order_id.localeCompare(b.order_id));
  }, [orders, query, status, type]);

  // Наряд выбран на карте или в Ганте, но скрыт фильтром — сбрасываем фильтры, чтобы показать строку.
  useEffect(() => {
    if (!selected || selected.source === 'table') return;
    if (!rows.some((o) => o.order_id === selected.id) && orders.some((o) => o.order_id === selected.id)) {
      setQuery('');
      setStatus('ALL');
      setType('ALL');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.nonce]);
  useScrollToSelected(boxRef, selected, 'table');

  const n = (s: UiOrderStatus) => orders.filter((o) => o.status === s).length;

  return (
    <div className="list">
      <div className="toolbar">
        <input
          className="input"
          type="search"
          placeholder="Наряд, адрес или исполнитель"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Поиск нарядов"
        />
        <SegmentedControl
          size="s"
          value={status}
          onChange={(v) => setStatus(v as Filter)}
          items={[
            { index: 'ALL', label: `Все ${orders.length}` },
            { index: 'ROUTED', label: `В маршруте ${n('ROUTED')}` },
            { index: 'UNASSIGNED', label: `Не распр. ${n('UNASSIGNED')}` },
            { index: 'PGP', label: `ПГП ${n('PGP')}` },
            { index: 'CANCELLED', label: `Отменены ${n('CANCELLED')}` },
          ]}
        />
        <select
          className="select"
          value={type}
          onChange={(e) => setType(e.target.value as 'ALL' | WorkTypeCode)}
          aria-label="Тип работ"
        >
          <option value="ALL">Все типы работ</option>
          {(Object.keys(WORK_TYPE_SHORT) as WorkTypeCode[]).map((c) => (
            <option key={c} value={c}>
              {WORK_TYPE_SHORT[c]}
            </option>
          ))}
        </select>
      </div>

      <div className="scroll" ref={boxRef}>
        <table className="table table--select">
          <thead>
            <tr>
              <th>Наряд</th>
              <th>Тип работ</th>
              <th>Адрес</th>
              <th className="num">Слот</th>
              <th>Статус</th>
              <th>Исполнитель</th>
              <th className="num">Плановое время</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((o) => {
              const cls = [o.status === 'CANCELLED' && 'is-muted', selected?.id === o.order_id && 'is-selected']
                .filter(Boolean)
                .join(' ');
              return (
                <tr
                  key={o.order_id}
                  data-order={o.order_id}
                  className={cls || undefined}
                  aria-selected={selected?.id === o.order_id}
                  onClick={() => onSelect(o.order_id)}
                >
                  <td className="mono-id">
                    {o.order_id.slice(-4)}
                    {o.is_repeat && (
                      <span className="flag" title="Повторное обращение, вес 2">
                        повт.
                      </span>
                    )}
                  </td>
                  <td title={o.work_type_name}>
                    {WORK_TYPE_SHORT[o.work_type_code]}
                    <span className="sub">{o.technology}</span>
                  </td>
                  <td className="addr" title={o.address}>
                    {o.address.replace(/^г\. Екатеринбург,\s*/, '')}
                  </td>
                  <td className="num">{window2(o.slot)}</td>
                  <td>
                    <Badge variant="secondary" size="2xs" colorScheme={STATUS[o.status].scheme} label={STATUS[o.status].label} />
                    {o.partner_staff_id && (
                      <span className="flag" title={`Совместный наряд: напарник ${o.partner_staff_name ?? o.partner_staff_id} (${o.partner_staff_id}) в то же время`}>
                        <Badge variant="secondary" size="2xs" colorScheme="status-01" label="совместный" />
                      </span>
                    )}
                    {o.late_risk && (
                      <span className="flag flag--warn" title="Активный риск опоздания">
                        +{o.late_risk.lateness_min ?? '?'} мин
                      </span>
                    )}
                  </td>
                  <td title={o.explanation ?? o.reason_text ?? undefined}>
                    {o.staff_id ? (
                      <>
                        {o.staff_name ?? o.staff_id}
                        {o.staff_name && <span className="sub">{o.staff_id}</span>}
                        {o.partner_staff_id && <span className="sub">+ напарник {o.partner_staff_id}</span>}
                      </>
                    ) : (
                      (o.group_id ?? '—')
                    )}
                  </td>
                  <td className="num">
                    {o.planned_start ? (
                      <>
                        {hhmm(o.planned_start)}–{hhmm(o.planned_end)}
                        <span className="sub">прибытие {hhmm(o.planned_arrival)}</span>
                      </>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && <p className="empty">Нарядов по этим условиям нет. Сбросьте поиск или фильтр.</p>}
      </div>
    </div>
  );
}
