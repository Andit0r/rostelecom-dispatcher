import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { RuntimeSetting, RuntimeSettingsResult, RuntimeSettingValue } from '../types/api';

/** Режимы планировщика (GET/PUT /ui/settings, POST /ui/settings/reset) для диспетчерского интерфейса.
 *  enabled=false — не запрашивать (сотрудник: эндпоинты только для диспетчера). */
export function useRuntimeSettings(enabled = true) {
  const [items, setItems] = useState<RuntimeSetting[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Применённые режимы делают построенный план устаревшим — показать баннер «пересоберите день». */
  const [pending, setPending] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems((await api.settingsGet()).settings);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) void reload();
  }, [enabled, reload]);

  const take = useCallback((r: RuntimeSettingsResult) => {
    setItems(r.settings);
    if (r.plan_outdated) setPending(true);
    return r;
  }, []);

  /** Ошибки (422/403/429…) пробрасываются — показывает вызывающий. */
  const apply = useCallback(
    async (changes: Record<string, RuntimeSettingValue>) => take(await api.settingsPut(changes)),
    [take],
  );
  const reset = useCallback(async () => take(await api.settingsReset()), [take]);
  const clearPending = useCallback(() => setPending(false), []);

  return { items, loading, error, reload, apply, reset, pending, clearPending };
}

export type RuntimeSettingsState = ReturnType<typeof useRuntimeSettings>;

/** Включён ли режим on/off. */
export const isOn = (items: RuntimeSetting[] | null, key: string) => items?.find((s) => s.key === key)?.value === 'on';
