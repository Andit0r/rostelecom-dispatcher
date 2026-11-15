import { duration } from '../format';
import type { Plan, UiOrder } from '../types/api';
import type { AutoFlash } from '../hooks/usePlan';

export function KpiStrip({ plan, orders, flash }: { plan: Plan | null; orders: UiOrder[]; flash?: AutoFlash | null }) {
  const count = (s: UiOrder['status']) => orders.filter((o) => o.status === s).length;
  const late = plan?.alerts.filter((a) => a.type === 'LATE_RISK' && a.status === 'ACTIVE').length ?? 0;
  const travel =
    plan?.summary?.travel_sec_total ?? plan?.routes.reduce((acc, r) => acc + (r.totals?.travel_sec ?? 0), 0);
  const staffOnShift = plan?.routes.filter((r) => r.status === 'ACTIVE').length ?? 0;
  const v = (n: number) => (plan ? n : '—');

  const cells: { label: string; value: string | number; tone?: 'ok' | 'bad' | 'warn' | 'pgp'; hint?: string; flash?: boolean }[] = [
    { label: 'Нарядов всего', value: v(orders.length), hint: `Сотрудников на смене: ${staffOnShift}` },
    { label: 'В маршрутах', value: v(count('ROUTED')), tone: 'ok' },
    { label: 'Не распределено', value: v(count('UNASSIGNED')), tone: count('UNASSIGNED') ? 'bad' : undefined },
    { label: 'ПГП', value: v(count('PGP')), tone: count('PGP') ? 'pgp' : undefined, flash: flash?.pgp },
    { label: 'Отменено', value: v(count('CANCELLED')) },
    { label: 'Риск опоздания', value: v(late), tone: late ? 'warn' : undefined, flash: flash?.late },
    { label: 'Время в пути', value: plan ? duration(travel) : '—', hint: 'Сумма по всем маршрутам' },
  ];

  return (
    <section className="kpi" aria-label="Показатели плана">
      {cells.map((c) => (
        <div
          // новый ключ на каждое автообновление с новыми тревогами — CSS-анимация проигрывается заново
          key={c.flash ? `${c.label}-${flash?.nonce}` : c.label}
          className={`kpi__cell${c.tone ? ` kpi__cell--${c.tone}` : ''}${c.flash ? ' flash' : ''}`}
          title={c.hint}
        >
          <span className="kpi__value num">{c.value}</span>
          <span className="kpi__label">{c.label}</span>
        </div>
      ))}
    </section>
  );
}
