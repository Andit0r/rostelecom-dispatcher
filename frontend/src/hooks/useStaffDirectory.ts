// Справочник сотрудников с ФИО (/ui/directory/staff): кэш на всё приложение,
// map staff_id → «Фамилия И. О.», перезагрузка после правки.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api, ApiFailure } from '../api/client';
import type {
  EquipmentBulkInput,
  EquipmentBulkResult,
  EquipmentScope,
  RecommendedBackpack,
  StaffProfile,
  StaffProfileInput,
  StaffProfileSaved,
} from '../types/api';

export interface StaffDirectoryState {
  items: StaffProfile[];
  /** staff_id → «Фамилия И. О.»; пустая, пока справочник не загружен. */
  names: Map<string, string>;
  loading: boolean;
  error: string | null;
  /** false — сервер не смог записать файл, правки живут до перезапуска. */
  persisted: boolean;
  /** false — справочники не загружены (нет PUT reference / «Создать день»): список пуст. */
  referenceLoaded: boolean;
  reload: () => Promise<void>;
  update: (staffId: string, body: StaffProfileInput) => Promise<StaffProfileSaved>;
  create: (body: StaffProfileInput) => Promise<StaffProfileSaved>;
  /** Подсказка «рекомендуемый рюкзак» из последнего сценария «Рюкзак АО»; null — сценарий не запускали. */
  recommended: RecommendedBackpack | null;
  /** Рюкзаки АО изменены после последней загрузки дня: нужна пересборка. */
  equipmentPending: boolean;
  clearEquipmentPending: () => void;
  resetEquipment: (staffId: string) => Promise<StaffProfileSaved>;
  bulkEquipment: (body: EquipmentBulkInput) => Promise<EquipmentBulkResult>;
  bulkResetEquipment: (scope: EquipmentScope) => Promise<EquipmentBulkResult>;
}

type Loaded = {
  items: StaffProfile[];
  persisted: boolean;
  recommended: RecommendedBackpack | null;
  referenceLoaded: boolean;
};

/** Кэш модуля: экраны, смонтированные позже, не ждут повторной загрузки. */
let cache: Loaded | null = null;
let inflight: Promise<Loaded> | null = null;

function fetchDirectory(force = false) {
  if (cache && !force) return Promise.resolve(cache);
  if (!inflight || force) {
    inflight = api
      .directoryStaff()
      .then(
        (r) =>
          (cache = {
            items: r.staff,
            persisted: r.persisted,
            recommended: r.recommended_backpack ?? null,
            referenceLoaded: r.reference_loaded ?? true,
          }),
      )
      .finally(() => (inflight = null));
  }
  return inflight;
}

const message = (e: unknown) => (e instanceof ApiFailure || e instanceof Error ? e.message : String(e));

/** enabled=false — справочник не запрашивается (сотруднику /ui/directory/* недоступен, 403). */
export function useStaffDirectory(enabled = true): StaffDirectoryState {
  const [items, setItems] = useState<StaffProfile[]>(cache?.items ?? []);
  const [persisted, setPersisted] = useState(cache?.persisted ?? true);
  const [referenceLoaded, setReferenceLoaded] = useState(cache?.referenceLoaded ?? true);
  const [recommended, setRecommended] = useState<RecommendedBackpack | null>(cache?.recommended ?? null);
  const [equipmentPending, setEquipmentPending] = useState(false);
  const [loading, setLoading] = useState(enabled && !cache);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (force: boolean) => {
    setLoading(true);
    try {
      const r = await fetchDirectory(force);
      setItems(r.items);
      setPersisted(r.persisted);
      setReferenceLoaded(r.referenceLoaded);
      setRecommended(r.recommended);
      setError(null);
    } catch (e) {
      setError(message(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) void load(false);
  }, [load, enabled]);

  const reload = useCallback(() => load(true), [load]);

  const update = useCallback(
    async (staffId: string, body: StaffProfileInput) => {
      const saved = await api.directoryUpdate(staffId, body);
      if (saved.applies_from_next_replan) setEquipmentPending(true);
      await reload();
      return saved;
    },
    [reload],
  );

  const create = useCallback(
    async (body: StaffProfileInput) => {
      const saved = await api.directoryCreate(body);
      await reload();
      return saved;
    },
    [reload],
  );

  const resetEquipment = useCallback(
    async (staffId: string) => {
      const saved = await api.directoryEquipmentReset(staffId);
      setEquipmentPending(true);
      await reload();
      return saved;
    },
    [reload],
  );

  const bulkEquipment = useCallback(
    async (body: EquipmentBulkInput) => {
      const res = await api.directoryEquipmentBulk(body);
      setEquipmentPending(true);
      await reload();
      return res;
    },
    [reload],
  );

  const bulkResetEquipment = useCallback(
    async (scope: EquipmentScope) => {
      const res = await api.directoryEquipmentBulkReset(scope);
      if (res.reset) setEquipmentPending(true);
      await reload();
      return res;
    },
    [reload],
  );

  const clearEquipmentPending = useCallback(() => setEquipmentPending(false), []);

  const names = useMemo(() => new Map(items.map((p) => [p.staff_id, p.display_name])), [items]);

  return {
    items,
    names,
    loading,
    error,
    persisted,
    referenceLoaded,
    reload,
    update,
    create,
    recommended,
    equipmentPending,
    clearEquipmentPending,
    resetEquipment,
    bulkEquipment,
    bulkResetEquipment,
  };
}

/** Один справочник на дерево (App и мобильный экран кладут сюда результат useStaffDirectory). */
export const StaffDirectoryContext = createContext<StaffDirectoryState | null>(null);

export function useStaffDirectoryContext(): StaffDirectoryState | null {
  return useContext(StaffDirectoryContext);
}

const EMPTY = new Map<string, string>();

/** «Фамилия И. О.» по staff_id (без ФИО — «Сотрудник ST-xxx» с сервера); fallback — то, что пришло с сервером,
 * иначе сам staff_id. */
export function useStaffName(): (staffId: string | null | undefined, fallback?: string | null) => string {
  const names = useContext(StaffDirectoryContext)?.names ?? EMPTY;
  return useCallback(
    (staffId, fallback) => (staffId ? (names.get(staffId) ?? fallback ?? staffId) : (fallback ?? '—')),
    [names],
  );
}
