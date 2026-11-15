// Текущий пользователь и выход: общий контекст для экрана диспетчера и мобильного вида.
import { createContext, useContext } from 'react';
import type { AuthMe } from '../types/api';

export interface AuthState {
  user: AuthMe;
  /** POST /ui/auth/logout и возврат к экрану входа. */
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState | null {
  return useContext(AuthContext);
}

/** Кнопка «Выйти» нужна только при включённой авторизации. */
export const canLogout = (a: AuthState | null) => !!a?.user.auth_enabled;
