/*
 * Вкладка «Режимы» панели «Данные и режимы»: режимы планировщика на лету (GET/PUT /ui/settings).
 * Правки копятся в черновике и уходят одним PUT по «Применить» (только изменённые ключи).
 * Режимы действуют со следующей пересборки дня и на новые события; при plan_outdated
 * панель показывает баннер «Режимы изменены — пересоберите день».
 * Внизу — «Создать день» (POST /ui/day/create: пустой день на дату, наряды вносятся вручную) и
 * «Очистить день» (POST /ui/day/reset): день приходит по API или создаётся диспетчером.
 */
import { useState } from 'react';
import { Badge, Button, SegmentedControl, type BadgeScheme } from './ui';
import { CreateDayForm } from './CreateDay';
import type { RuntimeSettingsState } from '../hooks/useRuntimeSettings';
import type { RuntimeSetting, RuntimeSettingValue } from '../types/api';

const JOINT = 'JOINT_ORDERS';
const RESET_CONFIRM = 'Текущий план и события дня будут удалены. Справочники сохранятся.';
const JOINT_CONFIRM = 'Затронет и /api/v1; для работы с валидатором организаторов оставьте выключенным.';

const SOURCE: Record<RuntimeSetting['source'], { label: string; scheme: BadgeScheme }> = {
  default: { label: 'по умолчанию', scheme: 'neutral' },
  env: { label: 'из окружения', scheme: 'info' },
  override: { label: 'изменено', scheme: 'status-01' },
};

const same = (a: RuntimeSettingValue, b: RuntimeSettingValue) =>
  typeof a === 'number' || typeof b === 'number' ? Math.abs(Number(a) - Number(b)) < 1e-6 : a === b;

const sec = (v: RuntimeSettingValue) => `${Math.round(Number(v))} с`;

/** «Быстро / Стандарт / Точно» = минимум / умолчание / максимум из ответа; иное значение — отдельным сегментом. */
function presets(s: RuntimeSetting, current: number) {
  const min = s.min ?? Number(s.default);
  const max = s.max ?? Number(s.default);
  const def = Math.min(max, Math.max(min, Math.round(Number(s.default))));
  const out = [
    { index: 'fast', label: 'Быстро', v: Math.ceil(min) },
    { index: 'std', label: 'Стандарт', v: def },
    { index: 'precise', label: 'Точно', v: Math.floor(max) },
  ].filter((p, i, all) => all.findIndex((q) => q.v === p.v) === i);
  const hit = out.find((p) => same(p.v, current));
  return { items: hit ? out : [...out, { index: 'custom', label: 'Своё', v: current }], active: hit?.index ?? 'custom' };
}

interface Props {
  settings: RuntimeSettingsState;
  /** Сохранено без устаревания плана — короткое подтверждение в панели. */
  onNotice: (text: string) => void;
  /** «Очистить день»; true — успешно. Нет — кнопка не показывается. */
  onResetDay?: () => Promise<boolean>;
  /** На сервере есть загруженный день (иначе очищать нечего, но запрос идемпотентен). */
  /** «Создать день»; true — успешно. Нет — блок не показывается. */
  onCreateDay?: (date: string) => Promise<boolean>;
  dayLoaded?: boolean;
  resetBusy?: boolean;
}

export function ModesTab({ settings, onNotice, onResetDay, onCreateDay, dayLoaded, resetBusy }: Props) {
  const { items, loading, error, reload } = settings;
  const [draft, setDraft] = useState<Record<string, RuntimeSettingValue>>({});
  const [confirmJoint, setConfirmJoint] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState<null | 'apply' | 'reset'>(null);
  const [fail, setFail] = useState<string | null>(null);

  if (!items) {
    return (
      <div className="dpanel__body">
        {error ? (
          <div className="dpanel__state">
            <p>Режимы не загрузились: {error}</p>
            <Button size="s" variant="outline" label="Повторить" onClick={() => void reload()} />
          </div>
        ) : (
          <p className="empty">{loading ? 'Загружаем режимы…' : 'Режимы недоступны.'}</p>
        )}
      </div>
    );
  }

  const valueOf = (s: RuntimeSetting) => (s.key in draft ? draft[s.key] : s.value);
  const dirty = items.filter((s) => s.key in draft && !same(draft[s.key], s.value));
  const anyOverride = items.some((s) => s.source === 'override');

  const set = (key: string, v: RuntimeSettingValue) => {
    setFail(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };

  const done = (r: { changed: string[]; plan_outdated: boolean }, what: string) => {
    setDraft({});
    if (!r.changed.length) onNotice(`${what}: значения не изменились.`);
    else if (!r.plan_outdated) onNotice(`${what}. Действует со следующей пересборки дня и новых событий.`);
  };

  const run = async (kind: 'apply' | 'reset', fn: () => Promise<void>) => {
    setBusy(kind);
    setFail(null);
    try {
      await fn();
    } catch (e) {
      setFail(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const apply = () =>
    run('apply', async () => {
      const body = Object.fromEntries(dirty.map((s) => [s.key, draft[s.key]]));
      done(await settings.apply(body), 'Сохранено');
    });

  const reset = () =>
    run('reset', async () => {
      setConfirmReset(false);
      done(await settings.reset(), 'Режимы сброшены');
    });

  return (
    <div className="dpanel__body">
      <div className="scroll dmodes">
        {items.map((s) => {
          const v = valueOf(s);
          const changed = s.key in draft && !same(draft[s.key], s.value);
          const src = SOURCE[s.source] ?? SOURCE.default;
          const isJoint = s.key === JOINT;
          return (
            <section key={s.key} className={`dmode${changed ? ' dmode--dirty' : ''}`} aria-label={s.label}>
              <div className="dmode__head">
                <h3 className="dmode__title">{s.label}</h3>
                <span className="dmode__badges">
                  {changed && <Badge size="2xs" variant="secondary" colorScheme="warning" label="изменено, не применено" />}
                  <Badge size="2xs" variant="outline" colorScheme={src.scheme} label={src.label} />
                </span>
              </div>
              <p className="dmode__desc">{s.description}</p>

              {s.type === 'enum' ? (
                <SegmentedControl
                  size="s"
                  value={String(v)}
                  disabledAll={!!busy}
                  onChange={(next) => {
                    if (isJoint && next === 'on' && s.value !== 'on') {
                      setConfirmJoint(true);
                      return;
                    }
                    setConfirmJoint(false);
                    set(s.key, next);
                  }}
                  items={[
                    { index: 'off', label: 'Выкл' },
                    { index: 'on', label: 'Вкл' },
                  ]}
                />
              ) : (
                (() => {
                  const p = presets(s, Number(v));
                  return (
                    <SegmentedControl
                      size="s"
                      value={p.active}
                      disabledAll={!!busy}
                      onChange={(idx) => {
                        const hit = p.items.find((it) => it.index === idx);
                        if (hit) set(s.key, hit.v);
                      }}
                      items={p.items.map((it) => ({
                        index: it.index,
                        label: (
                          <>
                            {it.label} <span className="num dmode__sec">{sec(it.v)}</span>
                          </>
                        ),
                      }))}
                    />
                  );
                })()
              )}

              {isJoint && confirmJoint && (
                <div className="daccess__confirm" role="alertdialog" aria-label="Включить совместные наряды">
                  <p>
                    <b>Включить совместные наряды?</b> {JOINT_CONFIRM}
                  </p>
                  <div className="daccess__btns">
                    <Button
                      size="s"
                      label="Включить"
                      onClick={() => {
                        setConfirmJoint(false);
                        set(s.key, 'on');
                      }}
                    />
                    <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" onClick={() => setConfirmJoint(false)} />
                  </div>
                </div>
              )}

              {s.risk && (
                <p className={`dmode__risk${v === 'on' ? ' dmode__risk--on' : ''}`} role={v === 'on' ? 'alert' : undefined}>
                  {s.risk}
                </p>
              )}
            </section>
          );
        })}

        {onCreateDay && (
          <section className="dmode" aria-label="Создать день">
            <div className="dmode__head">
              <h3 className="dmode__title">Создать день</h3>
            </div>
            <p className="dmode__desc">
              Открывает пустой день на выбранную дату с текущими справочниками и сотрудниками. Наряды по звонкам вносятся
              во вкладке «Наряды» кнопкой «+ Новый наряд». Если день на эту дату уже есть, сначала очистите его.
            </p>
            {createOpen ? (
              <CreateDayForm
                onCreate={onCreateDay}
                busy={resetBusy}
                onCancel={() => setCreateOpen(false)}
                onDone={() => {
                  setCreateOpen(false);
                  onNotice('День создан: пустой план, версия 1. Добавляйте наряды во вкладке «Наряды».');
                }}
              />
            ) : (
              <Button
                size="s"
                variant="outline"
                label="Создать день"
                disabled={resetBusy || !!busy}
                onClick={() => setCreateOpen(true)}
              />
            )}
          </section>
        )}

        {onResetDay && (
          <section className="dmode dmode--danger" aria-label="Очистить день">
            <div className="dmode__head">
              <h3 className="dmode__title">Очистить день</h3>
            </div>
            <p className="dmode__desc">
              Удаляет текущий день, план, события и отметки на сервере (и сохранённое состояние в БД). Справочники и
              матрица остаются — новый день можно сразу получить через API (PUT /api/v1/days, POST plan) или создать вручную.
            </p>
            {confirmClear ? (
              <div className="daccess__confirm" role="alertdialog" aria-label="Очистить день">
                <p>
                  <b>Очистить день?</b> {RESET_CONFIRM}
                </p>
                <div className="daccess__btns">
                  <Button
                    size="s"
                    label={resetBusy ? 'Очищаем…' : 'Очистить'}
                    disabled={resetBusy}
                    onClick={() =>
                      void onResetDay().then((ok) => {
                        setConfirmClear(false);
                        if (ok) onNotice('День очищен. Ожидаем данные через API или создайте новый день.');
                      })
                    }
                  />
                  <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" onClick={() => setConfirmClear(false)} />
                </div>
              </div>
            ) : (
              <Button
                size="s"
                variant="outline"
                colorScheme="neutral"
                label="Очистить день"
                title={dayLoaded ? 'Удалить текущий план и события дня' : 'День не загружен — очищать нечего'}
                disabled={resetBusy || !!busy}
                onClick={() => setConfirmClear(true)}
              />
            )}
          </section>
        )}
      </div>

      <div className="dform__foot">
        {fail && (
          <p className="dform__error" role="alert">
            {fail}
          </p>
        )}
        {confirmReset && (
          <div className="daccess__confirm" role="alertdialog" aria-label="Сбросить режимы">
            <p>
              Сбросить все режимы к умолчаниям? Изменения из интерфейса удалятся, значения из окружения сервера
              останутся.
            </p>
            <div className="daccess__btns">
              <Button size="s" label={busy === 'reset' ? 'Сбрасываем…' : 'Сбросить'} disabled={!!busy} onClick={() => void reset()} />
              <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" onClick={() => setConfirmReset(false)} />
            </div>
          </div>
        )}
        <div className="dform__actions">
          <Button
            size="m"
            label={busy === 'apply' ? 'Применяем…' : dirty.length ? `Применить (${dirty.length})` : 'Применить'}
            disabled={!dirty.length || !!busy}
            onClick={() => void apply()}
          />
          {dirty.length > 0 && (
            <Button
              size="m"
              variant="ghost"
              colorScheme="neutral"
              label="Отменить правки"
              disabled={!!busy}
              onClick={() => {
                setDraft({});
                setConfirmJoint(false);
                setFail(null);
              }}
            />
          )}
          <span className="dmodes__spacer" />
          <Button
            size="m"
            variant="outline"
            colorScheme="neutral"
            label="Сбросить к умолчаниям"
            title={anyOverride ? 'Убрать изменения, сделанные из интерфейса' : 'Изменений из интерфейса нет'}
            disabled={!anyOverride || !!busy}
            onClick={() => setConfirmReset(true)}
          />
        </div>
        <p className="dform__hint">Режимы общие для сервера: действуют со следующей пересборки дня и на новые события.</p>
      </div>
    </div>
  );
}
