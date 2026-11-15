/*
 * Боковая панель «Данные» (slide-over справа): справочник сотрудников с ФИО.
 * Закрывается Esc, кликом по затемнению и крестиком. Вкладка «Наряды» — components/DataOrders.tsx.
 * Данные и сохранение — через StaffDirectoryContext (hooks/useStaffDirectory).
 * «Рюкзак АО» (старт/максимум ONT, роутер, STB): в карточке и «Задать всем» в списке;
 * применяется при пересборке дня (баннер с кнопкой «Пересобрать»).
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Badge, Button, SegmentedControl, TabsGroup } from './ui';
import { PROFILE_SHORT } from '../planModel';
import { useStaffDirectoryContext } from '../hooks/useStaffDirectory';
import { OrdersTab } from './DataOrders';
import { ModesTab } from './DataModes';
import type { RuntimeSettingsState } from '../hooks/useRuntimeSettings';
import { AccessBlock, SecretDialog, type Secret } from './AccessBlock';
import { useAuth } from '../auth/AuthContext';
import type {
  DemoStepItem,
  UiOrder,
  Equipment,
  EquipmentScope,
  Profile,
  RecommendedBackpack,
  ServiceCenter,
  StaffProfile,
  StaffProfileInput,
  StaffProfileSaved,
} from '../types/api';
import './dataPanel.css';

export type DataTab = 'staff' | 'orders' | 'modes';
type View = { kind: 'list' } | { kind: 'edit'; id: string } | { kind: 'new' };

interface Props {
  /** Открытая вкладка (управляется снаружи: кнопки «Данные», «Режимы» и бейджи режимов в шапке). */
  tab: DataTab;
  onTab: (tab: DataTab) => void;
  /** Режимы планировщика (вкладка «Режимы»). */
  settings: RuntimeSettingsState;
  serviceCenters: ServiceCenter[];
  onClose: () => void;
  /** Наряды дня со статусами (вкладка «Наряды»). */
  orders: UiOrder[];
  day?: string;
  /** Время плана (as_of). */
  asOf?: string;
  /** Событие отправлено с панели — обновить план и строку итога. */
  onPlanEvent: (item: DemoStepItem) => Promise<boolean>;
  /** Пересборка текущего дня по его нарядам (режимы и рюкзаки АО применяются только так); true — успешно. */
  onReplan: () => Promise<boolean>;
  replanBusy: boolean;
  /** «Очистить день» во вкладке «Режимы» (только диспетчер). */
  onResetDay?: () => Promise<boolean>;
  /** «Создать день» во вкладке «Режимы» (только диспетчер). */
  onCreateDay?: (date: string) => Promise<boolean>;
  dayLoaded?: boolean;
}

const PROFILES: Profile[] = ['LTP3', 'INSTALL'];

type Kind = keyof Equipment;
const KINDS: { k: Kind; label: string }[] = [
  { k: 'ONT', label: 'ONT' },
  { k: 'ROUTER', label: 'Роутер' },
  { k: 'STB', label: 'STB' },
];
const EQ_MAX = 99;
const SOURCE_HINT = 'Исходные значения берутся из справочника сотрудников (staff.json).';

export function DataPanel({
  tab,
  onTab: setTab,
  settings,
  serviceCenters,
  onClose,
  orders,
  day,
  asOf,
  onPlanEvent,
  onReplan,
  replanBusy,
  onResetDay,
  onCreateDay,
  dayLoaded,
}: Props) {
  const [view, setView] = useState<View>({ kind: 'list' });
  const [notice, setNotice] = useState<string | null>(null);
  const [secret, setSecret] = useState<Secret | null>(null);
  const dir = useStaffDirectoryContext();
  // Блок «Доступ» — только при включённой авторизации (иначе аккаунтов нет).
  const authOn = !!useAuth()?.user.auth_enabled;
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      prev?.focus?.();
    };
  }, [onClose]);

  // Подтверждение сохранения гаснет само.
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  const scName = useMemo(() => new Map(serviceCenters.map((s) => [s.service_center_id, s.name])), [serviceCenters]);

  return (
    <div className="dpanel-root">
      <div className="dpanel-scrim" onClick={onClose} aria-hidden />
      <aside className="dpanel" role="dialog" aria-label="Данные и режимы" tabIndex={-1} ref={panelRef}>
        <header className="dpanel__head">
          <h2 className="dpanel__title">Данные и режимы</h2>
          <button type="button" className="dpanel__close" onClick={onClose} aria-label="Закрыть панель" title="Закрыть (Esc)">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
              <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </header>
        <TabsGroup
          size="s"
          value={tab}
          onChange={(v) => setTab(v as DataTab)}
          items={[
            { index: 'staff', label: dir?.items.length ? `Сотрудники ${dir.items.length}` : 'Сотрудники' },
            { index: 'orders', label: orders.length ? `Наряды ${orders.length}` : 'Наряды' },
            { index: 'modes', label: 'Режимы' },
          ]}
        />

        {notice && (
          <p className="dpanel__notice" role="status">
            {notice}
          </p>
        )}

        {dayLoaded && (dir?.equipmentPending || settings.pending) && (
          <div className="dpanel__replan" role="status">
            <span>
              {dir?.equipmentPending && settings.pending
                ? 'Рюкзаки и режимы изменены — пересоберите день'
                : settings.pending
                  ? 'Режимы изменены — пересоберите день'
                  : 'Рюкзаки изменены — пересоберите день'}
            </span>
            <Button
              size="s"
              label={replanBusy ? 'Пересобираем…' : 'Пересобрать'}
              disabled={replanBusy}
              onClick={() =>
                void onReplan().then(
                  (ok) =>
                    ok &&
                    setNotice(
                      dir?.equipmentPending
                        ? 'День пересобран: план построен с новыми рюкзаками АО.'
                        : 'День пересобран: план построен в новых режимах.',
                    ),
                )
              }
            />
          </div>
        )}

        {tab === 'modes' && (
          <ModesTab
            settings={settings}
            onNotice={setNotice}
            onResetDay={onResetDay}
            onCreateDay={onCreateDay}
            dayLoaded={dayLoaded}
            resetBusy={replanBusy}
          />
        )}

        {tab === 'orders' && (
          <OrdersTab orders={orders} day={day} asOf={asOf} onPlanEvent={onPlanEvent} onNotice={setNotice} />
        )}

        {tab === 'staff' && !dir && <p className="empty">Справочник сотрудников недоступен.</p>}

        {tab === 'staff' && dir && view.kind === 'list' && (
          <StaffList
            items={dir.items}
            referenceLoaded={dir.referenceLoaded}
            loading={dir.loading}
            error={dir.error}
            onRetry={() => void dir.reload()}
            serviceCenters={serviceCenters}
            scName={scName}
            onOpen={(id) => setView({ kind: 'edit', id })}
            onAdd={() => setView({ kind: 'new' })}
            onNotice={setNotice}
          />
        )}

        {tab === 'staff' && dir && view.kind !== 'list' && (
          <StaffForm
            key={view.kind === 'edit' ? view.id : 'new'}
            profile={view.kind === 'edit' ? dir.items.find((p) => p.staff_id === view.id) ?? null : null}
            isNew={view.kind === 'new'}
            serviceCenters={serviceCenters}
            scName={scName}
            persisted={dir.persisted}
            recommended={dir.recommended}
            onBack={() => setView({ kind: 'list' })}
            showAccess={authOn}
            onSecret={setSecret}
            onResetEquipment={async (id) => {
              const saved = await dir.resetEquipment(id);
              setNotice(`Рюкзак ${saved.display_name} сброшен к данным.`);
              return saved;
            }}
            onSave={async (body) => {
              if (view.kind === 'edit') {
                const saved = await dir.update(view.id, body);
                setNotice(
                  saved.applies_from_next_replan
                    ? `Сохранено: ${saved.display_name}. Рюкзак АО применится при пересборке дня.`
                    : `Сохранено: ${saved.display_name}`,
                );
              } else {
                const saved = await dir.create(body);
                setNotice(`Добавлен ${saved.display_name} (${saved.staff_id}). В план попадёт при следующей пересборке дня.`);
                if (saved.login && saved.initial_password) {
                  setSecret({
                    title: 'Пароль для входа',
                    login: saved.login,
                    password: saved.initial_password,
                    name: saved.display_name,
                  });
                }
                setView({ kind: 'edit', id: saved.staff_id });
              }
            }}
          />
        )}
      </aside>
      {secret && <SecretDialog secret={secret} onClose={() => setSecret(null)} />}
    </div>
  );
}

/* ---------------------------------------------------------------- список */

function initials(p: Pick<StaffProfile, 'first_name' | 'last_name'>): string {
  return ((p.last_name?.[0] ?? '') + (p.first_name?.[0] ?? '')).toUpperCase() || '?';
}

function Monogram({ p, size = 's' }: { p: Pick<StaffProfile, 'first_name' | 'last_name' | 'profile'>; size?: 's' | 'l' }) {
  return (
    <span className={`dmono dmono--${size} dmono--${p.profile === 'INSTALL' ? 'install' : 'ltp'}`} aria-hidden>
      {initials(p)}
    </span>
  );
}

function StaffList({
  items,
  referenceLoaded,
  loading,
  error,
  onRetry,
  serviceCenters,
  scName,
  onOpen,
  onAdd,
  onNotice,
}: {
  items: StaffProfile[];
  /** false — справочники не загружены: показываем пустое состояние с подсказкой. */
  referenceLoaded: boolean;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  serviceCenters: ServiceCenter[];
  scName: Map<string, string>;
  onOpen: (id: string) => void;
  onAdd: () => void;
  onNotice: (text: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [bulkOpen, setBulkOpen] = useState(false);
  const [profile, setProfile] = useState<'ALL' | Profile>('ALL');
  const [sc, setSc] = useState('ALL');

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items
      .filter((p) => profile === 'ALL' || p.profiles.includes(profile))
      .filter((p) => sc === 'ALL' || p.service_center_id === sc)
      .filter(
        (p) =>
          !q ||
          [p.full_name, p.display_name, p.staff_id, p.personnel_number, p.phone].some((v) =>
            (v ?? '').toLowerCase().includes(q),
          ),
      )
      .sort((a, b) => (a.full_name || a.display_name).localeCompare(b.full_name || b.display_name, 'ru'));
  }, [items, query, profile, sc]);

  const filtered = query.trim() !== '' || profile !== 'ALL' || sc !== 'ALL';

  return (
    <div className="dpanel__body">
      <div className="dpanel__tools">
        <input
          className="input"
          type="search"
          placeholder="ФИО, табельный номер или телефон"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Поиск сотрудника"
        />
        <Button
          size="s"
          variant={bulkOpen ? 'secondary' : 'outline'}
          label="Задать всем"
          title="Рюкзак АО для всех или одного профиля"
          onClick={() => setBulkOpen((v) => !v)}
        />
        <Button size="s" label="+ Добавить" onClick={onAdd} />
      </div>
      {bulkOpen && (
        <BulkEquipment
          overridden={items.filter((p) => p.equipment_override).length}
          onDone={(text) => {
            onNotice(text);
            setBulkOpen(false);
          }}
          onCancel={() => setBulkOpen(false)}
        />
      )}
      <div className="dpanel__filters">
        <select className="select" value={profile} onChange={(e) => setProfile(e.target.value as 'ALL' | Profile)} aria-label="Профиль">
          <option value="ALL">Все профили</option>
          {PROFILES.map((p) => (
            <option key={p} value={p}>
              {PROFILE_SHORT[p]}
            </option>
          ))}
        </select>
        <select className="select" value={sc} onChange={(e) => setSc(e.target.value)} aria-label="Сервисный центр">
          <option value="ALL">Все СЦ</option>
          {serviceCenters.map((s) => (
            <option key={s.service_center_id} value={s.service_center_id}>
              {s.name}
            </option>
          ))}
        </select>
        {filtered && (
          <span className="dpanel__count num">
            {rows.length} из {items.length}
          </span>
        )}
      </div>

      <div className="scroll">
        {error && !items.length ? (
          <div className="dpanel__state">
            <p>Справочник не загрузился: {error}</p>
            <Button size="s" variant="outline" label="Повторить" onClick={onRetry} />
          </div>
        ) : loading && !items.length ? (
          <p className="empty">Загружаем справочник…</p>
        ) : !referenceLoaded ? (
          <p className="empty">
            Справочник не загружен: пришлите PUT /api/v1/reference или нажмите «Создать день» (вкладка «Режимы»).
          </p>
        ) : !rows.length ? (
          <p className="empty">{items.length ? 'Никого не нашли. Измените поиск или фильтры.' : 'В справочнике пока никого нет. Добавьте первого сотрудника.'}</p>
        ) : (
          <ul className="dstaff">
            {rows.map((p) => (
              <li key={p.staff_id}>
                <button type="button" className="dstaff__row" onClick={() => onOpen(p.staff_id)}>
                  <Monogram p={p} />
                  <span className="dstaff__who">
                    <span className={`dstaff__name${p.full_name ? '' : ' is-empty'}`}>{p.full_name || p.display_name}</span>
                    <span className="dstaff__meta">
                      {p.position ?? (p.profile ? PROFILE_SHORT[p.profile] : '')}
                      {p.service_center_id ? `, ${scName.get(p.service_center_id) ?? p.service_center_id}` : ''}
                    </span>
                  </span>
                  <span className="dstaff__ids num">
                    {p.staff_id}
                    {p.personnel_number && <span className="sub">таб. {p.personnel_number}</span>}
                  </span>
                  {p.equipment_override && <Badge size="2xs" variant="secondary" colorScheme="info" label="рюкзак изменён" />}
                  {!(p.in_reference ?? p.in_planner) && (
                    <Badge size="2xs" variant="secondary" colorScheme="warning" label="нет в текущем справочнике" />
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- форма */

interface FormState {
  last_name: string;
  first_name: string;
  middle_name: string;
  phone: string;
  position: string;
  personnel_number: string;
  note: string;
  profile: Profile | '';
  service_center_id: string;
}

const fromProfile = (p: StaffProfile | null): FormState => ({
  last_name: p?.last_name ?? '',
  first_name: p?.first_name ?? '',
  middle_name: p?.middle_name ?? '',
  phone: p?.phone ?? '',
  position: p?.position ?? '',
  personnel_number: p?.personnel_number ?? '',
  note: p?.note ?? '',
  profile: p?.profile ?? '',
  service_center_id: p?.service_center_id ?? '',
});

function validate(f: FormState, isNew: boolean): Partial<Record<keyof FormState, string>> {
  const e: Partial<Record<keyof FormState, string>> = {};
  // Новому сотруднику ФИО обязательно; у записи из справочника его можно ввести позже (пока «Сотрудник ST-xxx»).
  if (isNew && !f.last_name.trim()) e.last_name = 'Укажите фамилию';
  if (isNew && !f.first_name.trim()) e.first_name = 'Укажите имя';
  if (!isNew && !!f.first_name.trim() !== !!f.last_name.trim())
    e[f.last_name.trim() ? 'first_name' : 'last_name'] = 'Укажите фамилию и имя (или оставьте оба пустыми)';
  if (f.phone.trim()) {
    const digits = f.phone.replace(/\D/g, '');
    const ok = digits.length === 10 || (digits.length === 11 && /^[78]/.test(digits));
    if (!ok) e.phone = 'Формат: +7 (9xx) xxx-xx-xx';
  }
  if (isNew && !f.profile) e.profile = 'Выберите профиль';
  if (isNew && !f.service_center_id) e.service_center_id = 'Выберите СЦ';
  return e;
}

function StaffForm({
  profile,
  isNew,
  serviceCenters,
  scName,
  persisted,
  recommended,
  onBack,
  onSave,
  onResetEquipment,
  showAccess,
  onSecret,
}: {
  profile: StaffProfile | null;
  isNew: boolean;
  serviceCenters: ServiceCenter[];
  scName: Map<string, string>;
  persisted: boolean;
  recommended: RecommendedBackpack | null;
  onBack: () => void;
  onSave: (body: StaffProfileInput) => Promise<void>;
  onResetEquipment: (staffId: string) => Promise<StaffProfileSaved>;
  showAccess: boolean;
  onSecret: (s: Secret) => void;
}) {
  const [form, setForm] = useState<FormState>(() => fromProfile(profile));
  const [eq, setEq] = useState<EqForm>(() => eqFrom(profile?.equipment_start, profile?.equipment_capacity));
  const [eqResetting, setEqResetting] = useState(false);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const firstRef = useRef<HTMLInputElement>(null);

  useEffect(() => firstRef.current?.focus(), []);

  if (!isNew && !profile) {
    return (
      <div className="dpanel__body">
        <div className="dpanel__state">
          <p>Сотрудник не найден в справочнике.</p>
          <Button size="s" variant="outline" label="К списку" onClick={onBack} />
        </div>
      </div>
    );
  }

  const hasEq = !isNew && !!profile?.equipment_start && !!profile.equipment_capacity;
  const eqInitial = eqFrom(profile?.equipment_start, profile?.equipment_capacity);
  const eqDirty = hasEq && !eqSame(eq, eqInitial);
  const eqErrors = hasEq ? validateEq(eq) : {};
  const errors = validate(form, isNew);
  const shown = touched ? errors : {};
  const locked = !isNew && !!profile?.in_planner; // профиль и СЦ задаёт справочник планировщика
  const initial = fromProfile(profile);
  const dirty = isNew || eqDirty || (Object.keys(form) as (keyof FormState)[]).some((k) => form[k] !== initial[k]);
  const set = (k: keyof FormState) => (e: { target: { value: string } }) => {
    setForm((f) => ({ ...f, [k]: e.target.value }));
    setError(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length || Object.keys(eqErrors).length || saving) return;
    const body: StaffProfileInput = {
      last_name: form.last_name.trim() || null,
      first_name: form.first_name.trim() || null,
      middle_name: form.middle_name.trim() || null,
      phone: form.phone.trim() || null,
      position: form.position.trim() || null,
      personnel_number: form.personnel_number.trim() || null,
      note: form.note.trim() || null,
    };
    if (!locked) {
      if (form.profile) body.profile = form.profile;
      if (form.service_center_id) body.service_center_id = form.service_center_id;
    }
    if (eqDirty) {
      body.equipment_start = eqNumbers(eq.start);
      body.equipment_capacity = eqNumbers(eq.cap);
    }
    setSaving(true);
    try {
      await onSave(body);
      setTouched(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const preview = {
    first_name: form.first_name,
    last_name: form.last_name,
    profile: (form.profile || null) as Profile | null,
  };
  const title = [form.last_name, form.first_name, form.middle_name].map((s) => s.trim()).filter(Boolean).join(' ');

  return (
    <form className="dpanel__body dform" onSubmit={submit} noValidate>
      <div className="dform__nav">
        <Button size="s" variant="ghost" colorScheme="neutral" label="Назад к списку" onClick={onBack} />
      </div>

      <div className="dform__hero">
        <Monogram p={preview} size="l" />
        <div>
          <h3 className="dform__title">{title || (isNew ? 'Новый сотрудник' : profile?.display_name)}</h3>
          <p className="dform__sub">
            {isNew ? 'Номер ST-xxx присвоится при сохранении' : profile?.staff_id}
            {!isNew && profile?.service_center_id && `, ${scName.get(profile.service_center_id) ?? profile.service_center_id}`}
          </p>
        </div>
      </div>

      {isNew && <p className="dform__warn">Новый сотрудник попадёт в план при следующей пересборке дня.</p>}
      {!isNew && profile && !(profile.in_reference ?? profile.in_planner) && (
        <p className="dform__warn">
          Нет в текущем справочнике (PUT reference): запись и ФИО сохранены, в план сотрудник не попадает.
        </p>
      )}

      <div className="scroll dform__scroll">
        <fieldset className="dform__group">
          <legend>ФИО</legend>
          <Field label="Фамилия" required={isNew} error={shown.last_name}>
            <input ref={firstRef} className="input" value={form.last_name} onChange={set('last_name')} maxLength={60} autoComplete="off" />
          </Field>
          <Field label="Имя" required={isNew} error={shown.first_name}>
            <input className="input" value={form.first_name} onChange={set('first_name')} maxLength={60} autoComplete="off" />
          </Field>
          <Field label="Отчество">
            <input className="input" value={form.middle_name} onChange={set('middle_name')} maxLength={60} autoComplete="off" />
          </Field>
        </fieldset>

        <fieldset className="dform__group">
          <legend>Работа</legend>
          <Field label="Профиль" required={isNew} error={shown.profile} hint={locked ? 'Задаётся планировщиком' : undefined}>
            <select className="select" value={form.profile} onChange={set('profile')} disabled={locked}>
              {!form.profile && <option value="">Выберите</option>}
              {PROFILES.map((p) => (
                <option key={p} value={p}>
                  {PROFILE_SHORT[p]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Сервисный центр" required={isNew} error={shown.service_center_id} hint={locked ? 'Задаётся планировщиком' : undefined}>
            <select className="select" value={form.service_center_id} onChange={set('service_center_id')} disabled={locked}>
              {!form.service_center_id && <option value="">Выберите</option>}
              {serviceCenters.map((s) => (
                <option key={s.service_center_id} value={s.service_center_id}>
                  {s.name}
                </option>
              ))}
              {form.service_center_id && !scName.has(form.service_center_id) && (
                <option value={form.service_center_id}>{form.service_center_id}</option>
              )}
            </select>
          </Field>
          <Field label="Должность" hint={isNew ? 'По умолчанию — по профилю' : undefined}>
            <input className="input" value={form.position} onChange={set('position')} maxLength={80} />
          </Field>
          <Field label="Табельный номер" hint={isNew ? 'Присвоится сам, если не указать' : undefined}>
            <input className="input num" value={form.personnel_number} onChange={set('personnel_number')} maxLength={20} />
          </Field>
        </fieldset>

        <fieldset className="dform__group">
          <legend>Связь</legend>
          <Field label="Телефон" error={shown.phone}>
            <input
              className="input num"
              type="tel"
              inputMode="tel"
              placeholder="+7 (9xx) xxx-xx-xx"
              value={form.phone}
              onChange={set('phone')}
              maxLength={24}
            />
          </Field>
          <Field label="Заметка" wide>
            <textarea className="input dform__note" value={form.note} onChange={set('note')} maxLength={500} rows={3} />
          </Field>
        </fieldset>

        {showAccess && !isNew && profile && (
          <AccessBlock staffId={profile.staff_id} name={profile.display_name} onSecret={onSecret} />
        )}

        {hasEq && profile && (
          <EquipmentBlock
            profile={profile}
            eq={eq}
            errors={eqErrors}
            recommended={recommended}
            resetting={eqResetting}
            onChange={(next) => {
              setEq(next);
              setError(null);
            }}
            onReset={async () => {
              if (!profile.equipment_override) {
                // переопределения нет — просто вернуть поля к данным
                setEq(eqFrom(profile.equipment_start_data, profile.equipment_capacity_data));
                return;
              }
              setEqResetting(true);
              try {
                const saved = await onResetEquipment(profile.staff_id);
                setEq(eqFrom(saved.equipment_start, saved.equipment_capacity));
              } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
              } finally {
                setEqResetting(false);
              }
            }}
          />
        )}
      </div>

      <div className="dform__foot">
        {error && (
          <p className="dform__error" role="alert">
            {error}
          </p>
        )}
        {!persisted && <p className="dform__hint">Сервер хранит правки только до перезапуска.</p>}
        <div className="dform__actions">
          <button type="submit" className="btn btn--m btn--primary btn--accent" disabled={saving || !dirty}>
            {saving ? 'Сохраняем…' : isNew ? 'Добавить сотрудника' : 'Сохранить'}
          </button>
          <Button size="m" variant="ghost" colorScheme="neutral" label="Отмена" onClick={onBack} />
        </div>
      </div>
    </form>
  );
}

function Field({
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
    <label className={`dfield${wide ? ' dfield--wide' : ''}${error ? ' dfield--error' : ''}`}>
      <span className="dfield__label">
        {label}
        {required && <span className="dfield__req" aria-hidden> *</span>}
      </span>
      {children}
      {error ? <span className="dfield__msg">{error}</span> : hint ? <span className="dfield__hint">{hint}</span> : null}
    </label>
  );
}

/* ---------------------------------------------------------------- рюкзак АО */

type EqRow = Record<Kind, string>;
interface EqForm {
  start: EqRow;
  cap: EqRow;
}
type EqErrors = Partial<Record<Kind, string>>;

const eqRow = (e: Equipment | null | undefined): EqRow => ({
  ONT: e ? String(e.ONT) : '',
  ROUTER: e ? String(e.ROUTER) : '',
  STB: e ? String(e.STB) : '',
});
const eqFrom = (start: Equipment | null | undefined, cap: Equipment | null | undefined): EqForm => ({
  start: eqRow(start),
  cap: eqRow(cap),
});
const eqSame = (a: EqForm, b: EqForm) => KINDS.every(({ k }) => a.start[k] === b.start[k] && a.cap[k] === b.cap[k]);
const eqNumbers = (row: EqRow): Equipment => ({ ONT: Number(row.ONT), ROUTER: Number(row.ROUTER), STB: Number(row.STB) });

/** Целое 0…99 из строки поля; null — пусто или не число. */
function parseCount(v: string): number | null {
  const t = v.trim();
  if (!/^\d{1,2}$/.test(t)) return null;
  const n = Number(t);
  return n <= EQ_MAX ? n : null;
}

function validateEq(eq: EqForm): EqErrors {
  const e: EqErrors = {};
  for (const { k, label } of KINDS) {
    const s = parseCount(eq.start[k]);
    const c = parseCount(eq.cap[k]);
    if (s === null || c === null) e[k] = `${label}: целое от 0 до ${EQ_MAX}`;
    else if (c < s) e[k] = `${label}: максимум меньше старта`;
  }
  return e;
}

function formatEq(e: Partial<Equipment>): string {
  return KINDS.filter(({ k }) => e[k] !== undefined)
    .map(({ k, label }) => `${label} ${e[k]}`)
    .join(' · ');
}

function recommendedFor(rec: RecommendedBackpack | null, profile: Profile | null | undefined): Partial<Equipment> | null {
  if (!rec || !profile) return null;
  const r = rec.by_profile[profile];
  return r && Object.keys(r).length ? r : null;
}

/** Сетка «Старт / Максимум» × ONT, роутер, STB; data — строка «В данных» (только в карточке). */
function EqGrid({
  eq,
  errors = {},
  placeholder,
  data,
  onChange,
}: {
  eq: EqForm;
  errors?: EqErrors;
  placeholder?: string;
  data?: EqForm;
  onChange: (row: 'start' | 'cap', k: Kind, value: string) => void;
}) {
  const rows: { row: 'start' | 'cap'; label: string }[] = [
    { row: 'start', label: 'Старт' },
    { row: 'cap', label: 'Максимум' },
  ];
  return (
    <div className="deq__grid" role="group" aria-label="Стартовое и максимальное количество АО">
      <span />
      {KINDS.map(({ k, label }) => (
        <span key={k} className="deq__col">
          {label}
        </span>
      ))}
      {rows.map(({ row, label: rowLabel }) => [
        <span key={row} className="deq__row">
          {rowLabel}
        </span>,
        ...KINDS.map(({ k, label }) => (
          <input
            key={`${row}-${k}`}
            className={`input num deq__input${errors[k] ? ' deq__input--error' : ''}`}
            inputMode="numeric"
            placeholder={placeholder}
            value={eq[row][k]}
            onChange={(e) => onChange(row, k, e.target.value)}
            maxLength={2}
            aria-label={`${label}: ${rowLabel.toLowerCase()}`}
          />
        )),
      ])}
      {data && (
        <>
          <span className="deq__row deq__row--data">В данных</span>
          {KINDS.map(({ k }) => (
            <span key={k} className="deq__data num" title="старт / максимум в staff.json">
              {data.start[k]} / {data.cap[k]}
            </span>
          ))}
        </>
      )}
    </div>
  );
}

function EquipmentBlock({
  profile,
  eq,
  errors,
  recommended,
  resetting,
  onChange,
  onReset,
}: {
  profile: StaffProfile;
  eq: EqForm;
  errors: EqErrors;
  recommended: RecommendedBackpack | null;
  resetting: boolean;
  onChange: (next: EqForm) => void;
  onReset: () => void;
}) {
  const data = eqFrom(profile.equipment_start_data, profile.equipment_capacity_data);
  const differs = !eqSame(eq, data);
  const rec = recommendedFor(recommended, profile.profile);
  const errorList = Object.values(errors);

  return (
    <fieldset className="dform__group deq">
      <legend>
        Рюкзак АО
        {profile.equipment_override && (
          <span className="deq__badge">
            <Badge size="2xs" variant="secondary" colorScheme="info" label="изменено" />
          </span>
        )}
      </legend>
      <EqGrid
        eq={eq}
        errors={errors}
        data={data}
        onChange={(row, k, value) => onChange({ ...eq, [row]: { ...eq[row], [k]: value } })}
      />
      {errorList.length > 0 && <p className="dfield__msg deq__msg">{errorList.join('; ')}</p>}
      {rec && (
        <p className="deq__rec">
          Рекомендуемый старт (p{recommended?.percentile ?? 90} расхода за смену, сценарий «Рюкзак АО»):{' '}
          <b className="num">{formatEq(rec)}</b>
        </p>
      )}
      <p className="dfield__hint deq__hint">{SOURCE_HINT} Новые значения применяются при пересборке дня.</p>
      {(profile.equipment_override || differs) && (
        <div className="deq__actions">
          <Button
            size="s"
            variant="ghost"
            colorScheme="neutral"
            label={resetting ? 'Сбрасываем…' : 'Сбросить к данным'}
            disabled={resetting}
            onClick={onReset}
          />
        </div>
      )}
    </fieldset>
  );
}

const SCOPES: { index: EquipmentScope; label: string; reset: string }[] = [
  { index: 'ALL', label: 'Всем', reset: 'Сбросить у всех' },
  { index: 'INSTALL', label: 'Инсталляторам', reset: 'Сбросить у инсталляторов' },
  { index: 'LTP3', label: 'Только ЛТП', reset: 'Сбросить у ЛТП' },
];

/** «Задать всем»: старт/максимум (пусто — не менять) для всех или одного профиля. */
function BulkEquipment({
  overridden,
  onDone,
  onCancel,
}: {
  overridden: number;
  onDone: (notice: string) => void;
  onCancel: () => void;
}) {
  const dir = useStaffDirectoryContext();
  const [scope, setScope] = useState<EquipmentScope>('ALL');
  const [eq, setEq] = useState<EqForm>(() => eqFrom(null, null));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!dir) return null;

  const pick = (row: EqRow): Partial<Equipment> | null => {
    const out: Partial<Equipment> = {};
    for (const { k } of KINDS) {
      const n = parseCount(row[k]);
      if (n !== null) out[k] = n;
    }
    return Object.keys(out).length ? out : null;
  };
  const invalid = KINDS.some(({ k }) => [eq.start[k], eq.cap[k]].some((v) => v.trim() !== '' && parseCount(v) === null));
  const start = pick(eq.start);
  const cap = pick(eq.cap);
  const crossed = KINDS.filter(({ k }) => {
    const s = start?.[k];
    const c = cap?.[k];
    return s !== undefined && c !== undefined && c < s;
  });
  const canApply = !busy && !invalid && !crossed.length && !!(start || cap);
  const rec = scope === 'ALL' ? null : recommendedFor(dir.recommended, scope);
  const scopeInfo = SCOPES.find((s) => s.index === scope) ?? SCOPES[0];

  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await fn());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="dbulk" aria-label="Рюкзак АО для группы сотрудников">
      <div className="dbulk__head">
        <span className="dbulk__title">Рюкзак АО</span>
        <SegmentedControl
          size="s"
          value={scope}
          onChange={(v) => setScope(v as EquipmentScope)}
          items={SCOPES.map(({ index, label }) => ({ index, label }))}
        />
      </div>
      <EqGrid
        eq={eq}
        placeholder="—"
        onChange={(row, k, value) => {
          setEq((f) => ({ ...f, [row]: { ...f[row], [k]: value } }));
          setError(null);
        }}
      />
      <p className="dfield__hint deq__hint">
        Пустое поле — не менять. {SOURCE_HINT}
        {overridden > 0 && ` Сейчас изменено у ${overridden} сотр.`}
      </p>
      {rec && (
        <p className="deq__rec">
          Рекомендуемый старт (p{dir.recommended?.percentile ?? 90}): <b className="num">{formatEq(rec)}</b>{' '}
          <button
            type="button"
            className="dbulk__link"
            onClick={() =>
              setEq((f) => ({
                ...f,
                start: { ...f.start, ...Object.fromEntries(Object.entries(rec).map(([k, v]) => [k, String(v)])) },
              }))
            }
          >
            подставить
          </button>
        </p>
      )}
      {(invalid || crossed.length > 0) && (
        <p className="dfield__msg deq__msg">
          {invalid
            ? `Целые числа от 0 до ${EQ_MAX}`
            : `Максимум меньше старта: ${crossed.map((c) => c.label).join(', ')}`}
        </p>
      )}
      {error && (
        <p className="dform__error" role="alert">
          {error}
        </p>
      )}
      <div className="dbulk__actions">
        <Button
          size="s"
          label={busy ? 'Применяем…' : 'Применить'}
          disabled={!canApply}
          onClick={() =>
            void act(async () => {
              const r = await dir.bulkEquipment({ start: start ?? undefined, capacity: cap ?? undefined, profile: scope });
              return `Рюкзак АО задан: ${r.updated ?? 0} сотр. Применится при пересборке дня.`;
            })
          }
        />
        <Button
          size="s"
          variant="outline"
          colorScheme="neutral"
          label={scopeInfo.reset}
          disabled={busy}
          onClick={() =>
            void act(async () => {
              const r = await dir.bulkResetEquipment(scope);
              return r.reset ? `Рюкзак сброшен к данным: ${r.reset} сотр.` : 'Изменённых рюкзаков нет — сбрасывать нечего.';
            })
          }
        />
        <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" onClick={onCancel} />
      </div>
    </section>
  );
}
