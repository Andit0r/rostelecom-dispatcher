import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type DockviewTheme } from 'dockview-react';
import 'dockview-react/dist/styles/dockview.css';
import './dock/dock.css';
import { TopBar } from './components/TopBar';
import { KpiStrip } from './components/KpiStrip';
import { WindowsMenu } from './components/WindowsMenu';
import { createMapMemory } from './components/DispatchMap';
import { usePlan } from './hooks/usePlan';
import { StaffDirectoryContext, useStaffDirectory } from './hooks/useStaffDirectory';
import { DataPanel, type DataTab } from './components/DataPanel';
import { isOn, useRuntimeSettings } from './hooks/useRuntimeSettings';
import { canLogout, useAuth } from './auth/AuthContext';
import { staffDirectory, type RouteMode, type Selection, type Source } from './planModel';
import type { UiOrder } from './types/api';
import {
  DOCK_COMPONENTS,
  DockDataContext,
  DockTab,
  DockWatermark,
  EmptyDay,
  GroupActions,
  type DockData,
} from './dock/DockPanels';
import {
  buildDefaultLayout,
  forgetLayout,
  isPanelShown,
  openPanel,
  restoreLayout,
  revealPanel,
  saveLayout,
  type PanelId,
} from './dock/layout';

/** Тема dockview: переменные — в dock/dock.css из токенов Атомаро; базовый класс light даёт служебные. */
const DOCK_THEME: DockviewTheme = {
  name: 'atomaro',
  className: 'dockview-theme-light dockview-theme-atomaro',
  colorScheme: 'light',
  gap: 12,
  dndOverlayMounting: 'relative',
  dndPanelOverlay: 'group',
  dndTabIndicator: 'line',
};

const SOURCE_PANEL: Record<Source, PanelId> = {
  table: 'orders',
  late: 'late',
  unassigned: 'unassigned',
  scenarios: 'scenarios',
  map: 'map',
  gantt: 'gantt',
  stats: 'stats',
};

/** Список, в котором стоит показать наряд, выбранный на карте или в Ганте. */
function listFor(api: DockviewApi, o: UiOrder | undefined): PanelId | null {
  if (o?.late_risk && isPanelShown(api, 'late')) return null;
  if (o && (o.status === 'UNASSIGNED' || o.status === 'PGP') && isPanelShown(api, 'unassigned')) return null;
  if (isPanelShown(api, 'orders')) return null;
  return 'orders';
}

export function App() {
  const auth = useAuth();
  // Режимы — только диспетчеру (сотрудник сюда не попадает, но страхуемся от лишнего 403).
  const isDispatcher = auth?.user.role !== 'EMPLOYEE';
  const plan = usePlan();
  const directory = useStaffDirectory();
  // Панель «Данные и режимы»: null — закрыта, иначе открытая вкладка.
  const [dataTab, setDataTab] = useState<DataTab | null>(null);
  const settings = useRuntimeSettings(isDispatcher);
  const activeModes = useMemo(
    () =>
      [
        isOn(settings.items, 'TRAFFIC_FACTORS') && 'Пробки',
        isOn(settings.items, 'JOINT_ORDERS') && 'Совместные ВЛС',
      ].filter((m): m is string => !!m),
    [settings.items],
  );
  // Справочники на сервере сменились (новый день, PUT reference): записи справочника сотрудников могли добавиться.
  const reloadDirectory = directory.reload;
  useEffect(() => {
    if (plan.reference) void reloadDirectory();
  }, [plan.reference, reloadDirectory]);
  // ФИО из справочника поверх staff_name нарядов: правка в панели «Данные» видна сразу, без пересчёта плана.
  const names = directory.names;
  const orders = useMemo(
    () =>
      names.size
        ? plan.orders.map((o) => (o.staff_id && names.has(o.staff_id) ? { ...o, staff_name: names.get(o.staff_id)! } : o))
        : plan.orders,
    [plan.orders, names],
  );
  const s = useMemo(() => ({ ...plan, orders }), [plan, orders]);
  const [orderSel, setOrderSel] = useState<Selection | null>(null);
  const [staffSel, setStaffSel] = useState<Selection | null>(null);
  const [routeMode, setRouteMode] = useState<RouteMode>('all');
  const [openIds, setOpenIds] = useState<ReadonlySet<PanelId>>(() => new Set());
  const nonce = useRef(0);
  const apiRef = useRef<DockviewApi | null>(null);
  const mapMemory = useRef(createMapMemory()).current;

  // dockview 8.8: ресайз плавающего окна запоминает исходную точку на ПЕРВОМ pointermove, а не на
  // pointerdown, и считает, что курсор ровно на краю. Если первое движение уже далеко (быстрый рывок,
  // мало событий), смещение до него теряется: окно сдвигается вместо того, чтобы растягиваться.
  // Сразу после pointerdown по рукоятке шлём pointermove с координатой самого края — отсчёт верный.
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const h = e.target instanceof HTMLElement ? e.target : null;
      const dir = h?.className.match(/^dv-resize-handle-(\w+)$/)?.[1];
      const box = h?.parentElement;
      if (!dir || !box?.classList.contains('dv-resize-container')) return;
      const r = box.getBoundingClientRect();
      const x = dir.includes('left') ? r.left : dir.includes('right') ? r.right : e.clientX;
      const y = dir.startsWith('top') ? r.top : dir.startsWith('bottom') ? r.bottom : e.clientY;
      window.dispatchEvent(
        new PointerEvent('pointermove', { clientX: x, clientY: y, pointerId: e.pointerId, buttons: 1, bubbles: true }),
      );
    };
    // фаза всплытия на window: обработчик dockview на рукоятке уже подписался на pointermove
    window.addEventListener('pointerdown', onDown);
    return () => window.removeEventListener('pointerdown', onDown);
  }, []);

  const staff = useMemo(() => staffDirectory(s.reference, s.plan, names), [s.reference, s.plan, names]);
  const ordersRef = useRef(s.orders);
  ordersRef.current = s.orders;

  /** Связка между панелями: если нужная панель закрыта или скрыта во вкладке — показываем её. */
  const revealFor = useCallback((source: Source, o: UiOrder | undefined) => {
    const api = apiRef.current;
    if (!api) return;
    const from = SOURCE_PANEL[source];
    if (source !== 'map') revealPanel(api, 'map', from);
    // Из «Статистики» выбирают нераспределённые наряды — показываем их список.
    if (source === 'stats' && o && (o.status === 'UNASSIGNED' || o.status === 'PGP')) revealPanel(api, 'unassigned', from);
    if (source === 'map' || source === 'gantt') {
      const list = listFor(api, o);
      if (list) revealPanel(api, list, from);
    }
  }, []);

  const selectOrder = useCallback(
    (id: string, source: Source) => {
      const o = ordersRef.current.find((x) => x.order_id === id);
      const n = ++nonce.current;
      setOrderSel({ id, source, nonce: n });
      if (o?.staff_id) setStaffSel({ id: o.staff_id, source, nonce: n, fit: false });
      revealFor(source, o);
    },
    [revealFor],
  );

  const selectStaff = useCallback((id: string | null, source: Source, fit: boolean) => {
    setStaffSel(id ? { id, source, nonce: ++nonce.current, fit } : null);
    if (id) setRouteMode((m) => (m === 'none' ? 'all' : m));
    if (id && fit && (source === 'gantt' || source === 'stats') && apiRef.current) revealPanel(apiRef.current, 'map', source);
  }, []);

  const syncOpen = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    setOpenIds(new Set(api.panels.map((p) => p.id as PanelId)));
  }, []);

  const onReady = useCallback(
    (e: DockviewReadyEvent) => {
      const api = e.api;
      apiRef.current = api;
      if (!api.panels.length && !restoreLayout(api)) buildDefaultLayout(api);
      syncOpen();
      api.onDidAddPanel(syncOpen);
      api.onDidRemovePanel(syncOpen);
      api.onDidLayoutFromJSON(syncOpen);
      // Сохраняем с задержкой (перетаскивание границы шлёт много событий). Проверка apiRef:
      // в StrictMode первый экземпляр dockview уничтожается, его таймер не должен затереть раскладку.
      let t: ReturnType<typeof setTimeout> | undefined;
      const flush = () => {
        clearTimeout(t);
        t = undefined;
        if (apiRef.current === api) saveLayout(api);
      };
      // dockview 8.8: минимальный размер плавающего окна — статика Overlay (20x20) и не настраивается.
      // Берём класс у любого плавающего окна и поднимаем минимум до 240x160 (иначе при сжатии
      // за левый/верхний край окно «уезжает», а CSS min-size лишь маскирует размер).
      const patchFloatMin = () => {
        const fg = (api as unknown as { component?: { floatingGroups?: { overlay?: { constructor: unknown } }[] } })
          .component?.floatingGroups;
        const O = fg?.[0]?.overlay?.constructor as { MINIMUM_WIDTH?: number; MINIMUM_HEIGHT?: number } | undefined;
        if (O) {
          O.MINIMUM_WIDTH = 240;
          O.MINIMUM_HEIGHT = 160;
        }
      };
      patchFloatMin();
      api.onDidLayoutChange(() => {
        patchFloatMin();
        clearTimeout(t);
        t = setTimeout(flush, 250);
      });
      window.addEventListener('pagehide', () => {
        if (t !== undefined) flush();
      });
    },
    [syncOpen],
  );

  const togglePanel = useCallback((id: PanelId, show: boolean) => {
    const api = apiRef.current;
    if (!api) return;
    if (show) openPanel(api, id);
    else api.getPanel(id)?.api.close();
  }, []);

  /** «Пересобрать»: план текущего дня с нуля по его нарядам, режимам и рюкзакам АО. */
  const rebuildDay = useCallback(async () => {
    setOrderSel(null);
    setStaffSel(null);
    const ok = await plan.rebuildDay();
    if (ok) {
      directory.clearEquipmentPending();
      settings.clearPending();
      await directory.reload();
    }
    return ok;
  }, [plan, directory, settings]);

  /** «Создать день»: пустой день на дату (POST /ui/day/create). */
  const createDay = useCallback(
    async (date: string) => {
      setOrderSel(null);
      setStaffSel(null);
      const ok = await plan.createDay(date);
      if (ok) {
        directory.clearEquipmentPending();
        settings.clearPending();
        await directory.reload();
      }
      return ok;
    },
    [plan, directory, settings],
  );

  const resetDay = useCallback(async () => {
    setOrderSel(null);
    setStaffSel(null);
    return plan.resetDay();
  }, [plan]);

  const resetLayout = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    forgetLayout();
    buildDefaultLayout(api);
    saveLayout(api);
  }, []);

  const late = s.plan?.alerts.filter((a) => a.type === 'LATE_RISK' && a.status === 'ACTIVE').length ?? 0;
  const unassigned = (s.plan?.unassigned.length ?? 0) + (s.plan?.pgp_groups.length ?? 0);

  const dockData: DockData = {
    s,
    staff,
    orderSel,
    staffSel,
    routeMode,
    setRouteMode,
    selectOrder,
    selectStaff,
    mapMemory,
    counts: {
      orders: { n: s.orders.length },
      late: { n: late, warn: late > 0 },
      unassigned: { n: unassigned },
    },
    openPanel: (id) => togglePanel(id, true),
  };

  return (
    <StaffDirectoryContext.Provider value={directory}>
    <div className="app">
      <TopBar
        demo={s.demo}
        plan={s.plan}
        busy={s.busy}
        onRefresh={() => void s.refresh()}
        lastStep={s.lastStep}
        windows={<WindowsMenu open={openIds} onToggle={togglePanel} onReset={resetLayout} />}
        onData={() => setDataTab((t) => (t && t !== 'modes' ? t : 'staff'))}
        dataOpen={!!dataTab && dataTab !== 'modes'}
        onModes={isDispatcher ? () => setDataTab('modes') : undefined}
        modesOpen={dataTab === 'modes'}
        activeModes={activeModes}
        clockFollows={isOn(settings.items, 'CLOCK_FOLLOWS_WALL')}
        onLogout={canLogout(auth) ? () => void auth!.logout() : undefined}
        autoRefresh={s.autoRefresh}
        onAutoRefresh={s.setAutoRefresh}
        live={s.live}
        flashNonce={s.flash?.nonce}
      />
      <KpiStrip plan={s.plan} orders={s.orders} flash={s.flash} />
      {s.error && (
        <div className="error" role="alert">
          {s.error}
        </div>
      )}

      <main className="work">
        <DockDataContext.Provider value={dockData}>
          <DockviewReact
            className="dock"
            theme={DOCK_THEME}
            components={DOCK_COMPONENTS}
            defaultTabComponent={DockTab}
            rightHeaderActionsComponent={GroupActions}
            watermarkComponent={DockWatermark}
            defaultRenderer="always"
            floatingGroupBounds="boundedWithinViewport"
            onReady={onReady}
          />
          {/* Дня нет: карточка поверх окон; раскладка dockview остаётся смонтированной и не сбрасывается. */}
          {s.noDay && !s.plan && (
            <EmptyDay busy={s.busy === 'load'} onCreate={isDispatcher ? createDay : undefined} />
          )}
        </DockDataContext.Provider>
      </main>
      {dataTab && (
        <DataPanel
          tab={dataTab}
          onTab={setDataTab}
          settings={settings}
          serviceCenters={s.reference?.service_centers ?? []}
          onClose={() => setDataTab(null)}
          orders={s.orders}
          day={s.demo?.date}
          asOf={s.plan?.as_of ?? s.demo?.as_of}
          onPlanEvent={s.applyEvent}
          replanBusy={s.busy === 'load'}
          // Пересборка текущего дня по его нарядам с новыми режимами и рюкзаками.
          onReplan={rebuildDay}
          onResetDay={isDispatcher ? resetDay : undefined}
          onCreateDay={isDispatcher ? createDay : undefined}
          dayLoaded={!!s.plan}
        />
      )}
    </div>
    </StaffDirectoryContext.Provider>
  );
}
