/*
 * Экран входа и обязательной смены пароля. Общая рамка: слева (на телефоне — сверху) фирменная
 * панель с логотипом и схемой маршрута, справа форма. Ошибки входа не раскрывают, что именно
 * неверно (сервер отвечает одинаково); 429 — обратный отсчёт до новой попытки.
 */
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type Ref } from 'react';
import { api, ApiFailure } from '../api/client';
import type { AuthSession, DemoHint } from '../types/api';
import './auth.css';

const MIN_PASSWORD = 8;
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Отсчёт до конца паузы после 429: возвращает оставшиеся секунды (0 — можно пробовать). */
function useCooldown(): [number, (sec: number) => void] {
  const [until, setUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until <= Date.now()) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [until]);
  const left = Math.max(0, Math.ceil((until - now) / 1000));
  return [left, (sec) => {
    setNow(Date.now());
    setUntil(Date.now() + sec * 1000);
  }];
}

function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <aside className="auth__brand">
        <div className="auth__brand-top">
          <img className="auth__logo" src="/logo-mark.png" alt="" aria-hidden />
          <span className="auth__name">Ростелеком Диспетчер</span>
        </div>
        <p className="auth__lede">Маршруты выездных бригад Екатеринбурга: план дня, отметки на адресах, пересчёт на ходу.</p>
        {/* Схема маршрута: сервисный центр → визиты → возврат. Чисто декоративная. */}
        <svg className="auth__route" viewBox="0 0 320 120" aria-hidden>
          <path d="M16 96 C 70 96, 70 34, 120 34 S 180 90, 220 72 S 280 20, 304 26" />
          <circle className="auth__depot" cx="16" cy="96" r="7" />
          <circle cx="120" cy="34" r="5" />
          <circle cx="220" cy="72" r="5" />
          <circle cx="304" cy="26" r="5" />
        </svg>
      </aside>
      <main className="auth__main">{children}</main>
    </div>
  );
}

function PasswordField({
  label,
  value,
  onChange,
  autoComplete,
  inputRef,
  error,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  inputRef?: Ref<HTMLInputElement>;
  error?: boolean;
}) {
  const [shown, setShown] = useState(false);
  const id = useId();
  return (
    <div className="auth__field">
      <label htmlFor={id}>{label}</label>
      <div className="auth__pw">
        <input
          id={id}
          ref={inputRef}
          className="auth__input"
          type={shown ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete={autoComplete}
          autoCapitalize="none"
          spellCheck={false}
          aria-invalid={error || undefined}
          maxLength={256}
        />
        <button
          type="button"
          className="auth__eye"
          aria-pressed={shown}
          aria-label={shown ? 'Скрыть пароль' : 'Показать пароль'}
          onClick={() => setShown((v) => !v)}
        >
          {shown ? 'Скрыть' : 'Показать'}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- вход

export function LoginScreen({ onLoggedIn }: { onLoggedIn: (s: AuthSession, password: string) => void }) {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<DemoHint | null>(null);
  const [wait, startWait] = useCooldown();
  const loginRef = useRef<HTMLInputElement>(null);
  const pwRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.title = 'Вход — Ростелеком Диспетчер';
    loginRef.current?.focus();
    let alive = true;
    api
      .authDemoHint()
      .then((h) => alive && setHint(h))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || wait) return;
    if (!login.trim() || !password) {
      setError('Введите логин и пароль');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const s = await api.authLogin(login.trim(), password);
      onLoggedIn(s, password);
    } catch (err) {
      if (err instanceof ApiFailure && err.status === 429) {
        startWait(err.retryAfter ?? 60);
        setError(err.message);
      } else if (err instanceof ApiFailure && err.status === 401) {
        setError('Неверный логин или пароль');
      } else if (err instanceof ApiFailure && err.code === 'AUTH_DISABLED') {
        setError('Вход отключён на сервере — обновите страницу');
      } else {
        setError(errText(err));
      }
      setPassword('');
      pwRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  const fill = (v: string) => {
    setLogin(v);
    setError(null);
    pwRef.current?.focus();
  };

  return (
    <AuthFrame>
      <form className="auth__form" onSubmit={submit} noValidate>
        <h1 className="auth__title">Вход</h1>
        <p className="auth__sub">Диспетчер — служебный логин, сотрудник — табельный номер.</p>
        <div className="auth__field">
          <label htmlFor="auth-login">Логин</label>
          <input
            id="auth-login"
            ref={loginRef}
            className="auth__input"
            value={login}
            onChange={(e) => {
              setLogin(e.target.value);
              setError(null);
            }}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            inputMode="text"
            maxLength={128}
            aria-invalid={!!error || undefined}
          />
        </div>
        <PasswordField
          label="Пароль"
          value={password}
          onChange={(v) => {
            setPassword(v);
            setError(null);
          }}
          autoComplete="current-password"
          inputRef={pwRef}
          error={!!error}
        />
        {error && (
          <p className="auth__error" role="alert">
            {wait ? `Слишком много попыток, подождите ${wait} с` : error}
          </p>
        )}
        <button type="submit" className="btn btn--l btn--primary btn--accent auth__submit" disabled={busy || wait > 0}>
          {busy ? 'Входим…' : wait ? `Повторить через ${wait} с` : 'Войти'}
        </button>

        {hint?.enabled && (
          <div className="auth__demo">
            <p>
              Тестовые учётные записи: нажмите, чтобы подставить логин. Пароль выдаёт администратор.
            </p>
            <div className="auth__chips">
              {hint.dispatcher_login && (
                <button type="button" className="auth__chip" onClick={() => fill(hint.dispatcher_login!)}>
                  <span>Диспетчер</span> {hint.dispatcher_login}
                </button>
              )}
              {(hint.employee_sample_logins ?? []).map((l) => (
                <button type="button" key={l} className="auth__chip" onClick={() => fill(l)}>
                  <span>Сотрудник</span> {l}
                </button>
              ))}
            </div>
          </div>
        )}
      </form>
    </AuthFrame>
  );
}

// ---------------------------------------------------------------- смена пароля

export function ChangePasswordScreen({
  login,
  knownPassword,
  onDone,
  onLogout,
}: {
  login: string;
  /** Пароль, с которым только что вошли: второй раз не спрашиваем. */
  knownPassword: string | null;
  onDone: (s: AuthSession) => void;
  onLogout: () => void;
}) {
  const [oldPw, setOldPw] = useState('');
  const [pw, setPw] = useState('');
  const [pw2, setPw2] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [wait, startWait] = useCooldown();

  useEffect(() => {
    document.title = 'Смена пароля — Ростелеком Диспетчер';
  }, []);

  const current = knownPassword ?? oldPw;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || wait) return;
    if (!current) return setError('Введите текущий пароль');
    if (pw.length < MIN_PASSWORD) return setError(`Новый пароль — не короче ${MIN_PASSWORD} символов`);
    if (pw !== pw2) return setError('Пароли не совпадают');
    if (pw === current) return setError('Новый пароль должен отличаться от текущего');
    setBusy(true);
    setError(null);
    try {
      onDone(await api.authChangePassword(current, pw));
    } catch (err) {
      if (err instanceof ApiFailure && err.status === 429) startWait(err.retryAfter ?? 60);
      setError(err instanceof ApiFailure && err.status === 401 ? 'Неверный текущий пароль' : errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame>
      <form className="auth__form" onSubmit={submit} noValidate>
        <h1 className="auth__title">Задайте свой пароль</h1>
        <p className="auth__sub">
          Пароль для <b>{login}</b> выдан временно. Придумайте новый, не короче {MIN_PASSWORD} символов, — он заменит
          временный на всех устройствах.
        </p>
        {knownPassword === null && (
          <PasswordField label="Текущий пароль" value={oldPw} onChange={setOldPw} autoComplete="current-password" />
        )}
        <PasswordField label="Новый пароль" value={pw} onChange={setPw} autoComplete="new-password" />
        <PasswordField label="Повторите новый пароль" value={pw2} onChange={setPw2} autoComplete="new-password" />
        {error && (
          <p className="auth__error" role="alert">
            {wait ? `Слишком много попыток, подождите ${wait} с` : error}
          </p>
        )}
        <button type="submit" className="btn btn--l btn--primary btn--accent auth__submit" disabled={busy || wait > 0}>
          {busy ? 'Сохраняем…' : 'Сохранить и продолжить'}
        </button>
        <button type="button" className="auth__link" onClick={onLogout} disabled={busy}>
          Выйти
        </button>
      </form>
    </AuthFrame>
  );
}

/** Пока проверяем сессию (GET /ui/auth/me) или сервер недоступен. */
export function AuthWaiting({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <AuthFrame>
      <div className="auth__form">
        {error ? (
          <>
            <h1 className="auth__title">Нет связи с сервером</h1>
            <p className="auth__sub">{error}</p>
            <button type="button" className="btn btn--l btn--primary btn--accent auth__submit" onClick={onRetry}>
              Повторить
            </button>
          </>
        ) : (
          <p className="auth__sub" role="status">
            Проверяем вход…
          </p>
        )}
      </div>
    </AuthFrame>
  );
}
