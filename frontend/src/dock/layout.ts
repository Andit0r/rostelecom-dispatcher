/*
 * Раскладка окон диспетчера на dockview: состав панелей, раскладка по умолчанию,
 * сохранение в localStorage и показ закрытых панелей.
 */
import type { AddPanelOptions, DockviewApi, IDockviewPanel, Position, SerializedDockview } from 'dockview-react';

export type PanelId = 'orders' | 'late' | 'unassigned' | 'scenarios' | 'map' | 'gantt' | 'stats';
export const LIST_PANELS: PanelId[] = ['orders', 'late', 'unassigned', 'scenarios'];
export const PANELS: { id: PanelId; title: string }[] = [
  { id: 'orders', title: 'Наряды' },
  { id: 'late', title: 'Риск опоздания' },
  { id: 'unassigned', title: 'Не распределено / ПГП' },
  { id: 'scenarios', title: 'Сценарии' },
  { id: 'map', title: 'Карта' },
  { id: 'gantt', title: 'Загрузка по времени' },
  // В раскладке по умолчанию нет: открывается из меню «Окна», чтобы не перегружать экран.
  { id: 'stats', title: 'Статистика' },
];
export const PANEL_TITLE = Object.fromEntries(PANELS.map((p) => [p.id, p.title])) as Record<PanelId, string>;
const isPanelId = (v: unknown): v is PanelId => typeof v === 'string' && v in PANEL_TITLE;

/** Версия в ключе: меняем при смене состава панелей, старые раскладки тогда игнорируются. */
const STORAGE_KEY = 'rtk-dispatch.layout.v3';

function panelOptions(id: PanelId, position?: AddPanelOptions['position'], extra?: Partial<AddPanelOptions>): AddPanelOptions {
  return { id, component: id, title: PANEL_TITLE[id], ...(position ? { position } : {}), ...extra } as AddPanelOptions;
}

/** Слева списки и «Сценарии» во вкладках, справа карта, снизу на всю ширину Гант (пропорции прежней сетки). */
export function buildDefaultLayout(api: DockviewApi) {
  api.clear();
  const w = api.width;
  const h = api.height;
  api.addPanel(panelOptions('orders'));
  api.addPanel(panelOptions('late', { referencePanel: 'orders', direction: 'within' }, { inactive: true }));
  api.addPanel(panelOptions('unassigned', { referencePanel: 'orders', direction: 'within' }, { inactive: true }));
  api.addPanel(panelOptions('scenarios', { referencePanel: 'orders', direction: 'within' }, { inactive: true }));
  api.addPanel(
    panelOptions('map', { referencePanel: 'orders', direction: 'right' }, w > 0 ? { initialWidth: Math.round(w * 0.4) } : {}),
  );
  api.addPanel(panelOptions('gantt', { direction: 'below' }, h > 0 ? { initialHeight: Math.round(h * 0.43) } : {}));
  api.getPanel('orders')?.api.setActive();
}

export function saveLayout(api: DockviewApi) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(api.toJSON()));
  } catch {
    /* приватный режим или переполнение — раскладка просто не запомнится */
  }
}

export function forgetLayout() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* нет доступа к хранилищу */
  }
}

/** Восстанавливает сохранённую раскладку; false — сохранённой нет или она битая. */
export function restoreLayout(api: DockviewApi): boolean {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    return false;
  }
  if (!raw) return false;
  try {
    const data = JSON.parse(raw) as SerializedDockview;
    const panels = Object.values(data?.panels ?? {});
    if (!data?.grid || !panels.length) throw new Error('empty layout');
    // Незнакомые компоненты (раскладка от другой версии экрана) — не восстанавливаем.
    if (!panels.every((p) => isPanelId(p.id) && (p.contentComponent ?? p.id) === p.id)) throw new Error('unknown panel');
    api.fromJSON(data);
    if (!api.panels.length) throw new Error('nothing restored');
    return true;
  } catch {
    try {
      api.clear();
    } catch {
      /* ignore */
    }
    forgetLayout();
    return false;
  }
}

/** Куда вернуть закрытую панель: к «своим» панелям, иначе к краю экрана. */
function defaultPosition(api: DockviewApi, id: PanelId): AddPanelOptions['position'] | undefined {
  const has = (p: PanelId) => !!api.getPanel(p);
  if (!api.panels.length) return undefined;
  if (LIST_PANELS.includes(id)) {
    const sibling = LIST_PANELS.find((p) => p !== id && has(p));
    if (sibling) return { referencePanel: sibling, direction: 'within' };
    if (has('map')) return { referencePanel: 'map', direction: 'left' };
    return { direction: 'left' };
  }
  if (id === 'stats') {
    if (has('map')) return { referencePanel: 'map', direction: 'right' };
    return { direction: 'right' };
  }
  if (id === 'map') {
    const list = LIST_PANELS.find(has);
    if (list) return { referencePanel: list, direction: 'right' };
    return { direction: 'right' };
  }
  return { direction: 'below' };
}

export function openPanel(api: DockviewApi, id: PanelId): IDockviewPanel {
  const existing = api.getPanel(id);
  if (existing) {
    existing.api.setActive();
    return existing;
  }
  const extra: Partial<AddPanelOptions> = {};
  if (id === 'gantt' && api.height > 0) extra.initialHeight = Math.round(api.height * 0.4);
  if (id === 'map' && api.width > 0) extra.initialWidth = Math.round(api.width * 0.4);
  if (id === 'stats' && api.width > 0) extra.initialWidth = Math.round(Math.min(560, Math.max(340, api.width * 0.3)));
  if (LIST_PANELS.includes(id) && api.width > 0 && !LIST_PANELS.some((p) => p !== id && api.getPanel(p))) {
    extra.initialWidth = Math.round(api.width * 0.55);
  }
  return api.addPanel(panelOptions(id, defaultPosition(api, id), extra));
}

/** Виден ли контент панели: она открыта, активна в своей группе и не скрыта развёрнутой чужой группой. */
export function isPanelShown(api: DockviewApi, id: PanelId): boolean {
  const p = api.getPanel(id);
  if (!p) return false;
  if (p.group.activePanel?.id !== id) return false;
  if (api.hasMaximizedGroup() && !p.group.api.isMaximized()) return false;
  return true;
}

/**
 * Показывает панель, на которую ведёт связка выбора: закрытую открывает, скрытую во вкладке активирует.
 * Не переключает вкладку в группе панели-источника — иначе пользователь потерял бы то, где кликнул.
 */
export function revealPanel(api: DockviewApi, id: PanelId, source?: PanelId) {
  const p = api.getPanel(id);
  if (!p) {
    openPanel(api, id);
    return;
  }
  if (p.group.activePanel?.id === id) return;
  const src = source ? api.getPanel(source) : undefined;
  if (src && src.group === p.group) return;
  p.api.setActive();
}

/** Куда закрепить плавающую группу: по самой «главной» панели в ней. */
export function dockPositionFor(ids: string[]): Position {
  if (ids.includes('gantt')) return 'bottom';
  if (ids.includes('map') || ids.includes('stats')) return 'right';
  return 'left';
}
