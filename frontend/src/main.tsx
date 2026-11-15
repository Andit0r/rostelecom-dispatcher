import { lazy, StrictMode, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, ApiFailure, setAuthToken, setUnauthorizedHandler } from './api/client';
import { AuthContext, type AuthState } from './auth/AuthContext';
import { AuthWaiting, ChangePasswordScreen, LoginScreen } from './auth/AuthScreens';
import type { AuthMe, AuthSession } from './types/api';
import './theme/tokens.css';
import './styles.css';

/** Экран диспетчера и мобильный экран — разные чанки: телефону не нужны dockview и карта диспетчера. */
const App = lazy(() => import('./App').then((m) => ({ default: m.App })));
/** Мобильный экран сотрудника — отдельный чанк, грузится только по адресу #/m (или #/m/ST-001). */
const MobileApp = lazy(() => import('./mobile/MobileApp'));

const isMobileHash = () => /^#\/m(\/|$)/.test(window.location.hash);

type Phase =
  | { kind: 'checking'; error: string | null }
  | { kind: 'login' }
  | { kind: 'change'; user: AuthMe; knownPassword: string | null }
  | { kind: 'ready'; user: AuthMe };

const fromSession = (s: AuthSession): AuthMe => ({
  login: s.login,
  role: s.role,
  staff_id: s.staff_id,
  display_name: s.display_name,
  must_change_password: s.must_change_password,
  auth_enabled: true,
});

/** Сотрудник видит только свой маршрут: любой другой адрес заменяем на #/m/<свой staff_id>. */
function forceEmployeeHash(user: AuthMe) {
  if (user.role !== 'EMPLOYEE') return;
  const own = user.staff_id ? `#/m/${user.staff_id}` : '#/m';
  if (window.location.hash !== own) window.history.replaceState(null, '', own);
}

function Root() {
  const [phase, setPhase] = useState<Phase>({ kind: 'checking', error: null });
  const [mobile, setMobile] = useState(isMobileHash);

  const check = useCallback(() => {
    setPhase({ kind: 'checking', error: null });
    api
      .authMe()
      .then((me) =>
        setPhase(
          me.auth_enabled && me.must_change_password
            ? { kind: 'change', user: me, knownPassword: null }
            : { kind: 'ready', user: me },
        ),
      )
      .catch((e) =>
        setPhase(
          e instanceof ApiFailure && e.status === 401
            ? { kind: 'login' }
            : { kind: 'checking', error: e instanceof Error ? e.message : String(e) },
        ),
      );
  }, []);

  useEffect(() => {
    check();
    // Сессия истекла посреди работы: показываем вход, адрес (#/m/...) не трогаем — после входа вернёмся туда же.
    setUnauthorizedHandler(() => setPhase({ kind: 'login' }));
    const on = () => setMobile(isMobileHash());
    window.addEventListener('hashchange', on);
    return () => {
      setUnauthorizedHandler(null);
      window.removeEventListener('hashchange', on);
    };
  }, [check]);

  const user = phase.kind === 'ready' ? phase.user : null;

  // Сотрудник: всегда мобильный вид со своим маршрутом, диспетчерский интерфейс недоступен.
  useEffect(() => {
    if (!user || user.role !== 'EMPLOYEE') return;
    forceEmployeeHash(user);
    setMobile(true);
    const on = () => forceEmployeeHash(user);
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, [user]);

  const logout = useCallback(async () => {
    try {
      await api.authLogout();
    } catch {
      /* cookie всё равно удалится по истечении; экран входа покажем в любом случае */
    }
    setAuthToken(null);
    // После выхода сбрасываем адрес: иначе следующий вход диспетчера откроет мобильный вид чужого сотрудника
    // (#/m/ST-…). Сотрудника при входе всё равно переведёт на его маршрут forceEmployeeHash.
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    setMobile(false);
    setPhase({ kind: 'login' });
  }, []);

  const auth = useMemo<AuthState | null>(() => (user ? { user, logout } : null), [user, logout]);

  const onLoggedIn = (s: AuthSession, password: string) => {
    setAuthToken(s.token);
    const me = fromSession(s);
    setPhase(me.must_change_password ? { kind: 'change', user: me, knownPassword: password } : { kind: 'ready', user: me });
  };

  if (phase.kind === 'checking') return <AuthWaiting error={phase.error} onRetry={check} />;
  if (phase.kind === 'login') return <LoginScreen onLoggedIn={onLoggedIn} />;
  if (phase.kind === 'change')
    return (
      <ChangePasswordScreen
        login={phase.user.login}
        knownPassword={phase.knownPassword}
        onDone={(s) => {
          setAuthToken(s.token);
          setPhase({ kind: 'ready', user: fromSession(s) });
        }}
        onLogout={() => {
          void api.authLogout().catch(() => undefined);
          setAuthToken(null);
          setPhase({ kind: 'login' });
        }}
      />
    );

  const showMobile = user!.role === 'EMPLOYEE' || mobile;
  return (
    <AuthContext.Provider value={auth}>
      <Suspense fallback={null}>{showMobile ? <MobileApp /> : <App />}</Suspense>
    </AuthContext.Provider>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
