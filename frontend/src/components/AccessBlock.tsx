/*
 * Блок «Доступ» в карточке сотрудника (панель «Данные»): логин, статус аккаунта, сброс пароля,
 * отключение. Временный пароль показывается один раз в окне SecretDialog (оно же — для
 * initial_password нового сотрудника). API: /ui/directory/accounts/*.
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { Account } from '../types/api';
import { Badge, Button } from './ui';

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export interface Secret {
  title: string;
  login: string;
  password: string;
  name?: string;
}

/** Окно с паролем «один раз». Esc и клик мимо не закрывают — только явное «Готово». */
export function SecretDialog({ secret, onClose }: { secret: Secret; onClose: () => void }) {
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null);
  const pwRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    doneRef.current?.focus();
    // Esc закрывает панель «Данные» — здесь его перехватываем, чтобы пароль не пропал случайно.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(secret.password);
      setCopied('ok');
    } catch {
      pwRef.current?.select();
      setCopied('fail');
    }
  };

  return (
    <div className="dsecret" role="alertdialog" aria-modal="true" aria-labelledby="dsecret-title">
      <div className="dsecret__card">
        <h3 id="dsecret-title" className="dsecret__title">
          {secret.title}
        </h3>
        {secret.name && <p className="dsecret__who">{secret.name}</p>}
        <dl className="dsecret__pair">
          <div>
            <dt>Логин</dt>
            <dd className="num">{secret.login}</dd>
          </div>
          <div>
            <dt>Пароль</dt>
            <dd>
              <input
                ref={pwRef}
                className="dsecret__pw num"
                readOnly
                value={secret.password}
                onFocus={(e) => e.target.select()}
                aria-label="Временный пароль"
              />
            </dd>
          </div>
        </dl>
        <p className="dsecret__warn">Передайте сотруднику, повторно не показывается. При первом входе он задаст свой пароль.</p>
        {copied && (
          <p className="dsecret__copied" role="status">
            {copied === 'ok' ? 'Пароль скопирован' : 'Не удалось скопировать: пароль выделен, нажмите Ctrl+C'}
          </p>
        )}
        <div className="dsecret__actions">
          <Button size="m" variant="secondary" label="Копировать пароль" onClick={() => void copy()} />
          <button ref={doneRef} type="button" className="btn btn--m btn--primary btn--accent" onClick={onClose}>
            Готово, передал
          </button>
        </div>
      </div>
    </div>
  );
}

type Confirm = 'reset' | 'disable' | null;

export function AccessBlock({ staffId, name, onSecret }: { staffId: string; name: string; onSecret: (s: Secret) => void }) {
  const [acct, setAcct] = useState<Account | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'missing' | 'error'>('loading');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Confirm>(null);

  useEffect(() => {
    let alive = true;
    setState('loading');
    api
      .accountsList()
      .then((r) => {
        if (!alive) return;
        const a = r.accounts.find((x) => x.staff_id === staffId) ?? null;
        setAcct(a);
        setState(a ? 'ready' : 'missing');
      })
      .catch((e) => {
        if (!alive) return;
        setError(errText(e));
        setState('error');
      });
    return () => {
      alive = false;
    };
  }, [staffId]);

  const run = async (what: 'reset' | 'disable' | 'enable') => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (what === 'reset') {
        const r = await api.accountReset(staffId);
        setAcct(r);
        setState('ready');
        onSecret({ title: 'Новый временный пароль', login: r.login, password: r.temporary_password, name });
      } else {
        setAcct(await api.accountSetDisabled(staffId, what === 'disable'));
        setState('ready');
      }
      setConfirm(null);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <fieldset className="dform__group daccess">
      <legend>Доступ</legend>
      {state === 'loading' && <p className="daccess__note">Загружаем аккаунт…</p>}
      {state === 'missing' && (
        <p className="daccess__note">
          Аккаунта пока нет. «Сбросить пароль» создаст его и выдаст временный пароль.
        </p>
      )}
      {state === 'ready' && acct && (
        <div className="daccess__row">
          <div className="daccess__login">
            <span className="daccess__k">Логин</span>
            <span className="num daccess__v">{acct.login}</span>
          </div>
          <div className="daccess__badges">
            <Badge
              label={acct.disabled ? 'Отключён' : 'Активен'}
              colorScheme={acct.disabled ? 'neutral' : 'success'}
              variant="secondary"
              dot
            />
            {acct.must_change_password && !acct.disabled && (
              <Badge label="Ждёт смены пароля" colorScheme="warning" variant="secondary" />
            )}
          </div>
        </div>
      )}
      {state !== 'loading' && state !== 'error' && (
        <div className="daccess__actions">
          {confirm ? (
            <div className="daccess__confirm" role="group" aria-label="Подтверждение">
              <p>
                {confirm === 'reset'
                  ? 'Сбросить пароль? Сотрудника выбросит из приложения, войти можно будет только с новым паролем.'
                  : 'Отключить аккаунт? Сотрудник сразу потеряет доступ к маршруту.'}
              </p>
              <div className="daccess__btns">
                <Button
                  size="s"
                  label={busy ? 'Выполняем…' : confirm === 'reset' ? 'Сбросить' : 'Отключить'}
                  disabled={busy}
                  onClick={() => void run(confirm)}
                />
                <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" disabled={busy} onClick={() => setConfirm(null)} />
              </div>
            </div>
          ) : (
            <div className="daccess__btns">
              <Button size="s" variant="outline" label="Сбросить пароль" disabled={busy} onClick={() => setConfirm('reset')} />
              {acct &&
                (acct.disabled ? (
                  <Button size="s" variant="ghost" label={busy ? 'Включаем…' : 'Включить'} disabled={busy} onClick={() => void run('enable')} />
                ) : (
                  <Button size="s" variant="ghost" colorScheme="neutral" label="Отключить" disabled={busy} onClick={() => setConfirm('disable')} />
                ))}
            </div>
          )}
        </div>
      )}
      {error && (
        <p className="dform__error" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}
