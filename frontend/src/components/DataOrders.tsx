/*
 * Вкладка «Наряды» панели «Данные»: наряды дня (поиск, фильтр по статусу, отмена с
 * подтверждением) и форма нового наряда (адрес, тип работ, окно, пожелание по исполнителю).
 * Сервер: /ui/orders/* — наряд уходит в план событием NEW_ORDER, отмена — ORDER_CANCELLED.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Badge, Button, SegmentedControl } from './ui';
import { api, ApiFailure } from '../api/client';
import { ACTION, hhmm, PLAN_TIME_HINT, REASON, STATUS, window2, WORK_TYPE_SHORT } from '../format';
import type {
  CreateOrderResult,
  DemoStepItem,
  OrderCatalog,
  OrderCatalogBuilding,
  OrderCatalogStaff,
  UiOrder,
  UiOrderStatus,
  WorkTypeCode,
} from '../types/api';

interface Props {
  orders: UiOrder[];
  day?: string;
  asOf?: string;
  onPlanEvent: (item: DemoStepItem) => Promise<boolean>;
  onNotice: (text: string) => void;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function OrdersTab({ orders, day, asOf, onPlanEvent, onNotice }: Props) {
  const [view, setView] = useState<'list' | 'new'>('list');
  if (!day) return <p className="empty">День ещё не загружен.</p>;
  if (view === 'new') {
    return <NewOrderForm asOf={asOf} onPlanEvent={onPlanEvent} onBack={() => setView('list')} />;
  }
  return <OrdersList orders={orders} asOf={asOf} onPlanEvent={onPlanEvent} onNotice={onNotice} onAdd={() => setView('new')} />;
}

/* ---------------------------------------------------------------- список */

const STATUS_FILTERS: { value: 'ALL' | UiOrderStatus; label: string }[] = [
  { value: 'ALL', label: 'Все статусы' },
  { value: 'ROUTED', label: STATUS.ROUTED.label },
  { value: 'UNASSIGNED', label: STATUS.UNASSIGNED.label },
  { value: 'PGP', label: STATUS.PGP.label },
  { value: 'CANCELLED', label: STATUS.CANCELLED.label },
];

/** Почему наряд нельзя отменить (null — можно). */
function cancelBlock(o: UiOrder, asOf?: string): string | null {
  if (o.status === 'CANCELLED') return 'Наряд уже отменён';
  if (o.stop_status === 'DONE') return 'Визит уже выполнен — отменить нельзя';
  if (o.stop_status === 'IN_PROGRESS' || (o.planned_start && asOf && o.planned_start < asOf)) {
    return 'Визит уже начат — по контракту отменяется только наряд, визит по которому не начат';
  }
  return null;
}

function OrdersList({
  orders,
  asOf,
  onPlanEvent,
  onNotice,
  onAdd,
}: {
  orders: UiOrder[];
  asOf?: string;
  onPlanEvent: (item: DemoStepItem) => Promise<boolean>;
  onNotice: (text: string) => void;
  onAdd: () => void;
}) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<'ALL' | UiOrderStatus>('ALL');
  const [confirm, setConfirm] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ id: string; text: string } | null>(null);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return orders
      .filter((o) => status === 'ALL' || o.status === status)
      .filter(
        (o) => !q || [o.order_id, o.address, o.staff_name, o.staff_id, o.work_type_name].some((v) => (v ?? '').toLowerCase().includes(q)),
      )
      .sort((a, b) => a.slot.start.localeCompare(b.slot.start) || a.order_id.localeCompare(b.order_id));
  }, [orders, query, status]);
  const filtered = query.trim() !== '' || status !== 'ALL';

  const cancel = async (o: UiOrder) => {
    setBusy(o.order_id);
    setError(null);
    try {
      const res = await api.ordersCancel(o.order_id);
      setConfirm(null);
      onNotice(`${res.summary}. План v${res.plan_version}.`);
      await onPlanEvent(res.event);
    } catch (e) {
      setError({ id: o.order_id, text: errText(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="dpanel__body">
      <div className="dpanel__tools">
        <input
          className="input"
          type="search"
          placeholder="Номер, адрес или исполнитель"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Поиск наряда"
        />
        <Button size="s" label="+ Новый наряд" onClick={onAdd} />
      </div>
      <div className="dpanel__filters">
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value as 'ALL' | UiOrderStatus)} aria-label="Статус">
          {STATUS_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <span className="dpanel__count num">{filtered ? `${rows.length} из ${orders.length}` : `${orders.length} нарядов`}</span>
      </div>

      <div className="scroll">
        {!rows.length ? (
          <p className="empty">{orders.length ? 'Ничего не нашли. Измените поиск или фильтр.' : 'Нарядов на день нет.'}</p>
        ) : (
          <ul className="dord">
            {rows.map((o) => {
              const st = STATUS[o.status];
              const block = cancelBlock(o, asOf);
              const started = o.stop_status === 'IN_PROGRESS' ? 'в работе' : o.stop_status === 'DONE' ? 'выполнен' : null;
              return (
                <li key={o.order_id} className="dord__row">
                  <div className="dord__main">
                    <span className="dord__top">
                      <span className="dord__id num">{o.order_id.slice(-4)}</span>
                      <span className="dord__slot num">{window2(o.slot)}</span>
                      <span className="dord__wt">{WORK_TYPE_SHORT[o.work_type_code] ?? o.work_type_name}</span>
                    </span>
                    <span className="dord__addr" title={o.address}>
                      {o.address.replace(/^г\. Екатеринбург,\s*/, '')}
                    </span>
                    <span className="dord__who">
                      {o.staff_name ? (
                        <>
                          {o.staff_name}
                          {o.planned_start && <span className="num"> · {hhmm(o.planned_start)}</span>}
                        </>
                      ) : o.status === 'UNASSIGNED' && o.reason_code ? (
                        REASON[o.reason_code]
                      ) : (
                        '—'
                      )}
                    </span>
                  </div>
                  <div className="dord__side">
                    <Badge size="2xs" variant="secondary" colorScheme={st.scheme} label={started ?? st.label} />
                    {o.status !== 'CANCELLED' && (
                      <span title={block ?? 'Отменить наряд (по просьбе абонента)'}>
                        <Button
                          size="s"
                          variant="ghost"
                          colorScheme="neutral"
                          label="Отменить"
                          disabled={!!block || busy !== null}
                          onClick={() => {
                            setConfirm(o.order_id);
                            setError(null);
                          }}
                        />
                      </span>
                    )}
                  </div>
                  {confirm === o.order_id && (
                    <div className="dord__confirm" role="alertdialog" aria-label={`Отмена наряда ${o.order_id}`}>
                      <span>
                        Отменить {o.order_id}? Абонент отказался от визита; маршрут будет пересчитан.
                      </span>
                      <span className="dord__confirm-actions">
                        <Button
                          size="s"
                          label={busy === o.order_id ? 'Отменяем…' : 'Да, отменить'}
                          disabled={busy !== null}
                          onClick={() => void cancel(o)}
                        />
                        <Button size="s" variant="ghost" colorScheme="neutral" label="Нет" disabled={busy !== null} onClick={() => setConfirm(null)} />
                      </span>
                    </div>
                  )}
                  {error?.id === o.order_id && (
                    <p className="dform__error dord__error" role="alert">
                      {error.text}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- форма */

const SLOT_HOURS = [9, 11, 13, 15, 17, 19];
const toMin = (hm: string) => {
  const [h, m] = hm.split(':').map(Number);
  return h * 60 + m;
};
const pad = (n: number) => String(n).padStart(2, '0');

/** Ближайшее стандартное окно (как на сервере: начало не раньше чем через 30 мин). */
function nearestSlot(asOf?: string): string | null {
  if (!asOf) return null;
  const now = toMin(hhmm(asOf));
  const h = SLOT_HOURS.find((x) => x * 60 - now >= 30);
  return h === undefined ? null : `${pad(h)}:00–${pad(h + 2)}:00`;
}

const shortAddr = (a: string) => a.replace(/^г\. Екатеринбург,\s*/, '');

function NewOrderForm({
  asOf,
  onPlanEvent,
  onBack,
}: {
  asOf?: string;
  onPlanEvent: (item: DemoStepItem) => Promise<boolean>;
  onBack: () => void;
}) {
  const [catalog, setCatalog] = useState<OrderCatalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** true — на сервере нет справочников (409 REFERENCE_NOT_LOADED): форма заблокирована. */
  const [noReference, setNoReference] = useState(false);
  const [building, setBuilding] = useState<OrderCatalogBuilding | null>(null);
  const [workType, setWorkType] = useState<WorkTypeCode | ''>('');
  const [repeat, setRepeat] = useState(false);
  const [slotMode, setSlotMode] = useState<'nearest' | 'manual'>('nearest');
  const [slotStart, setSlotStart] = useState('');
  const [slotEnd, setSlotEnd] = useState('');
  const [preferred, setPreferred] = useState<OrderCatalogStaff | null>(null);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CreateOrderResult | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .ordersCatalog({})
      .then((c) => alive && setCatalog(c))
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiFailure && e.code === 'REFERENCE_NOT_LOADED') setNoReference(true);
        else setLoadError(errText(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const wt = catalog?.work_types.find((w) => w.code === workType) ?? null;
  const nearest = nearestSlot(asOf);
  const errors: Partial<Record<'building' | 'workType' | 'slot', string>> = {};
  if (!building) errors.building = 'Выберите адрес из списка';
  if (!workType) errors.workType = 'Выберите тип работ';
  else if (building && !building.work_types.includes(workType)) errors.workType = 'Этот тип работ не выполняется по адресу';
  if (slotMode === 'manual') {
    if (!slotStart || !slotEnd) errors.slot = 'Укажите начало и конец окна';
    else if (toMin(slotEnd) <= toMin(slotStart)) errors.slot = 'Конец окна позже начала';
  } else if (!nearest) errors.slot = 'Стандартных окон на сегодня не осталось — задайте вручную';
  const shown = touched ? errors : {};

  const reset = () => {
    setResult(null);
    setBuilding(null);
    setWorkType('');
    setRepeat(false);
    setPreferred(null);
    setTouched(false);
    setError(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length || saving || !building || !workType) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.ordersCreate({
        building_id: building.building_id,
        work_type: workType,
        slot: slotMode === 'manual' ? { start: slotStart, end: slotEnd } : null,
        is_repeat: !!wt?.repeat_allowed && repeat,
        preferred_staff_id: preferred?.staff_id ?? null,
      });
      setResult(res);
      await onPlanEvent(res.event);
    } catch (err) {
      setError(errText(err));
    } finally {
      setSaving(false);
    }
  };

  if (result) {
    return (
      <div className="dpanel__body">
        <div className="scroll dform__scroll">
          <OrderResult res={result} />
        </div>
        <div className="dform__foot">
          <div className="dform__actions">
            <Button size="m" label="Ещё наряд" onClick={reset} />
            <Button size="m" variant="ghost" colorScheme="neutral" label="К списку" onClick={onBack} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <form className="dpanel__body dform" onSubmit={submit} noValidate>
      <div className="dform__nav">
        <Button size="s" variant="ghost" colorScheme="neutral" label="Назад к списку" onClick={onBack} />
      </div>
      <div className="dform__hero">
        <div>
          <h3 className="dform__title">Новый наряд</h3>
          <p className="dform__sub">
            Поступит в план по времени плана <span className="num" title={PLAN_TIME_HINT}>{hhmm(asOf)}</span>, номер присвоится сам
          </p>
        </div>
      </div>

      <div className="scroll dform__scroll">
        {noReference && (
          <p className="dform__warn" role="status">
            Справочник не загружен: адреса и типы работ берутся из справочников, пришедших по API. Пришлите
            PUT /api/v1/reference или нажмите «Создать день» (вкладка «Режимы»).
          </p>
        )}
        {loadError && <p className="dform__error">Справочники не загрузились: {loadError}</p>}
        <fieldset className="dform__group" disabled={noReference}>
          <legend>Что и где</legend>
          <OField label="Адрес" required wide error={shown.building}>
            <BuildingPicker
              value={building}
              onChange={(b) => {
                setBuilding(b);
                setError(null);
                if (b && workType && !b.work_types.includes(workType)) setWorkType('');
              }}
              total={catalog?.buildings_count}
            />
          </OField>
          <OField label="Тип работ" required wide error={shown.workType}>
            <select
              className="select"
              value={workType}
              onChange={(e) => {
                setWorkType(e.target.value as WorkTypeCode);
                setError(null);
              }}
              disabled={!catalog}
            >
              <option value="">Выберите</option>
              {catalog?.work_types.map((w) => {
                const off = !!building && !building.work_types.includes(w.code);
                return (
                  <option key={w.code} value={w.code} disabled={off}>
                    {w.name} · {w.duration_min} мин{off ? ' (не для этого адреса)' : ''}
                  </option>
                );
              })}
            </select>
          </OField>
          {wt?.repeat_allowed && (
            <label className="dord__check dfield--wide">
              <input type="checkbox" checked={repeat} onChange={(e) => setRepeat(e.target.checked)} />
              Повторное обращение (сокращённый контрольный срок, повышенный приоритет)
            </label>
          )}
        </fieldset>

        <fieldset className="dform__group">
          <legend>Окно визита</legend>
          <div className="dfield dfield--wide">
            <SegmentedControl
              size="s"
              value={slotMode}
              onChange={(v) => setSlotMode(v as 'nearest' | 'manual')}
              items={[
                { index: 'nearest', label: 'Ближайшее окно' },
                { index: 'manual', label: 'Вручную' },
              ]}
            />
            {slotMode === 'nearest' && nearest && (
              <span className="dfield__hint">
                <span className="num">{nearest}</span> — первое стандартное окно, до которого не меньше 30 мин
              </span>
            )}
            {shown.slot && <span className="dfield__msg">{shown.slot}</span>}
          </div>
          {slotMode === 'manual' && (
            <>
              <OField label="С">
                <input className="input num" type="time" value={slotStart} onChange={(e) => setSlotStart(e.target.value)} />
              </OField>
              <OField label="До">
                <input className="input num" type="time" value={slotEnd} onChange={(e) => setSlotEnd(e.target.value)} />
              </OField>
            </>
          )}
        </fieldset>

        <fieldset className="dform__group">
          <legend>Исполнитель</legend>
          <OField
            label="Предпочтительный исполнитель"
            wide
            hint="Пожелание, а не приказ: учтём, если сотрудник подходит и успевает без срыва своих визитов"
          >
            <StaffPicker staff={catalog?.staff ?? []} value={preferred} onChange={setPreferred} />
          </OField>
        </fieldset>
      </div>

      <div className="dform__foot">
        {error && (
          <p className="dform__error" role="alert">
            {error}
          </p>
        )}
        <div className="dform__actions">
          <button type="submit" className="btn btn--m btn--primary btn--accent" disabled={saving || !catalog}>
            {saving ? 'Планируем…' : 'Создать и назначить'}
          </button>
          <Button size="m" variant="ghost" colorScheme="neutral" label="Отмена" onClick={onBack} />
        </div>
      </div>
    </form>
  );
}

function OField({
  label,
  required,
  error,
  hint,
  wide,
  children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  hint?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`dfield${wide ? ' dfield--wide' : ''}${error ? ' dfield--error' : ''}`}>
      <span className="dfield__label">
        {label}
        {required && <span className="dfield__req" aria-hidden> *</span>}
      </span>
      {children}
      {error ? <span className="dfield__msg">{error}</span> : hint ? <span className="dfield__hint">{hint}</span> : null}
    </div>
  );
}

/* ---- адрес: поиск по подстроке на сервере (≤ 20 вариантов) */

function BuildingPicker({
  value,
  onChange,
  total,
}: {
  value: OrderCatalogBuilding | null;
  onChange: (b: OrderCatalogBuilding | null) => void;
  total?: number;
}) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<OrderCatalogBuilding[]>([]);
  const [found, setFound] = useState(0);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    if (value || q.trim().length < 2) {
      setItems([]);
      setFound(0);
      return;
    }
    const n = ++seq.current;
    setLoading(true);
    const t = setTimeout(() => {
      api
        .ordersCatalog({ q: q.trim() })
        .then((c) => {
          if (n !== seq.current) return;
          setItems(c.buildings);
          setFound(c.buildings_total);
        })
        .catch(() => n === seq.current && setItems([]))
        .finally(() => n === seq.current && setLoading(false));
    }, 250);
    return () => clearTimeout(t);
  }, [q, value]);

  if (value) {
    return (
      <div className="dord__picked">
        <span>
          <b>{shortAddr(value.address)}</b>
          <span className="dord__picked-meta">
            {value.building_type === 'MKD' ? 'МКД' : value.overhead_line ? 'частный дом, ВЛС' : 'частный дом'} ·{' '}
            {value.technologies.join('/')} · {value.building_id}
          </span>
        </span>
        <Button size="s" variant="ghost" colorScheme="neutral" label="Изменить" onClick={() => onChange(null)} />
      </div>
    );
  }
  return (
    <div className="dord__combo">
      <input
        className="input"
        type="search"
        placeholder={total ? `Улица и дом — ${total} адресов в зоне` : 'Улица и дом'}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        aria-label="Поиск адреса"
        autoComplete="off"
      />
      {open && q.trim().length >= 2 && (
        <ul className="dord__options" role="listbox">
          {loading && !items.length ? (
            <li className="dord__option dord__option--muted">Ищем…</li>
          ) : !items.length ? (
            <li className="dord__option dord__option--muted">Адрес не найден</li>
          ) : (
            items.map((b) => (
              <li key={b.building_id} role="option" aria-selected={false}>
                <button
                  type="button"
                  className="dord__option"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onChange(b);
                    setOpen(false);
                  }}
                >
                  {shortAddr(b.address)}
                  <span className="dord__option-meta">
                    {b.building_type === 'MKD' ? 'МКД' : 'частный'} · {b.technologies.join('/')}
                  </span>
                </button>
              </li>
            ))
          )}
          {found > items.length && <li className="dord__option dord__option--muted">ещё {found - items.length} — уточните запрос</li>}
        </ul>
      )}
    </div>
  );
}

/* ---- предпочтительный исполнитель: выпадающий список с поиском по ФИО */

function StaffPicker({
  staff,
  value,
  onChange,
}: {
  staff: OrderCatalogStaff[];
  value: OrderCatalogStaff | null;
  onChange: (s: OrderCatalogStaff | null) => void;
}) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const items = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return staff
      .filter((s) => !needle || [s.display_name, s.staff_id].some((v) => (v ?? '').toLowerCase().includes(needle)))
      .sort((a, b) => Number(b.on_shift) - Number(a.on_shift) || (a.display_name ?? a.staff_id).localeCompare(b.display_name ?? b.staff_id, 'ru'))
      .slice(0, 30);
  }, [staff, q]);

  if (value) {
    return (
      <div className="dord__picked">
        <span>
          <b>{value.display_name ?? value.staff_id}</b>
          <span className="dord__picked-meta">
            {value.staff_id} · {value.profiles.join('/')} · {value.technologies.join('/')}
            {value.height_permit ? ' · допуск к высоте' : ''}
          </span>
        </span>
        <Button size="s" variant="ghost" colorScheme="neutral" label="Убрать" onClick={() => onChange(null)} />
      </div>
    );
  }
  return (
    <div className="dord__combo">
      <input
        className="input"
        type="search"
        placeholder="Не важно — назначит планировщик"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        aria-label="Поиск сотрудника по ФИО"
        autoComplete="off"
      />
      {open && (
        <ul className="dord__options" role="listbox">
          {!items.length ? (
            <li className="dord__option dord__option--muted">Никого не нашли</li>
          ) : (
            items.map((s) => (
              <li key={s.staff_id} role="option" aria-selected={false}>
                <button
                  type="button"
                  className="dord__option"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onChange(s);
                    setQ('');
                    setOpen(false);
                  }}
                >
                  {s.display_name ?? s.staff_id}
                  <span className="dord__option-meta">
                    {s.profiles.join('/')} · {s.staff_id}
                    {s.on_shift ? '' : ' · не на смене'}
                  </span>
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}

/* ---- итог создания */

function OrderResult({ res }: { res: CreateOrderResult }) {
  const o = res.order;
  const a = res.assignment;
  const u = res.unassigned;
  return (
    <div className="dres">
      <div className={`dres__card dres__card--${res.outcome === 'ASSIGNED' ? 'ok' : res.outcome === 'PGP' ? 'info' : 'bad'}`}>
        {a ? (
          <>
            <p className="dres__title">
              Назначен: {a.staff_name ?? a.staff_id}, ETA <span className="num">{hhmm(a.eta)}</span>
            </p>
            <p className="dres__line num">
              Визит {hhmm(a.planned_start)}–{hhmm(a.planned_end)} · {a.staff_id}
            </p>
          </>
        ) : res.pgp ? (
          <>
            <p className="dres__title">Групповое повреждение (ПГП) {res.pgp.group_id}</p>
            <p className="dres__line">{res.pgp.message}</p>
            {res.pgp.comment && <p className="dres__line">{res.pgp.comment}</p>}
          </>
        ) : (
          <>
            <p className="dres__title">Не удалось назначить{u ? `: ${REASON[u.reason_code] ?? u.reason_code}` : ''}</p>
            {u && <p className="dres__line">{u.reason_text}</p>}
            {u && (
              <p className="dres__line">
                Что делать: {ACTION[u.suggested_action] ?? u.suggested_action}
                {u.proposed_slot && <span className="num"> · предложен перенос на {u.proposed_slot.start.slice(8, 10)}.{u.proposed_slot.start.slice(5, 7)} {window2(u.proposed_slot)}</span>}
              </p>
            )}
          </>
        )}
      </div>

      {res.preferred && (
        <p className={`dres__pref dres__pref--${res.preferred.honored ? 'ok' : 'warn'}`}>{res.preferred.message}</p>
      )}

      <dl className="dres__facts">
        <dt>Наряд</dt>
        <dd className="num">{res.order_id}</dd>
        <dt>Адрес</dt>
        <dd>{shortAddr(o.address)}</dd>
        <dt>Тип работ</dt>
        <dd>{res.work_type_name}</dd>
        <dt>Окно</dt>
        <dd className="num">
          {window2(o.slot)}, крайнее начало {hhmm(o.latest_start)}
        </dd>
        <dt>План</dt>
        <dd className="num">версия {res.plan_version}</dd>
      </dl>

      {res.warnings.length > 0 && (
        <div className="dres__warn">
          <p className="dres__warn-title">Затронуло другие наряды</p>
          <ul>
            {res.warnings.slice(0, 4).map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
