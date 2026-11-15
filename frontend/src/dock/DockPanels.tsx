/*
 * Панели док-раскладки. Данные и выбор живут в App и приходят через контекст:
 * панель можно закрыть, перенести или открепить — выбор и масштаб карты не теряются.
 */
import { createContext, useContext, useEffect, useState, type FunctionComponent, type PointerEvent } from 'react';
import type { IDockviewHeaderActionsProps, IDockviewPanelHeaderProps, IDockviewPanelProps, IWatermarkPanelProps } from 'dockview-react';
import { OrdersTable } from '../components/OrdersTable';
import { LateRiskList } from '../components/LateRiskList';
import { UnassignedList } from '../components/UnassignedList';
import { DispatchMap, type MapMemory } from '../components/DispatchMap';
import { GanttChart } from '../components/GanttChart';
import { ScenariosPanel as ScenariosView } from '../components/ScenariosPanel';
import { StatsPanel as StatsView } from '../components/StatsPanel';
import { Button } from '../components/ui';
import { CreateDayForm } from '../components/CreateDay';
import type { usePlan } from '../hooks/usePlan';
import type { RouteMode, Selection, Source, StaffInfo } from '../planModel';
import type { Route, ServiceCenter } from '../types/api';
import { PANEL_TITLE, dockPositionFor, type PanelId } from './layout';

const EMPTY_SC: ServiceCenter[] = [];
const EMPTY_ROUTES: Route[] = [];

export interface DockData {
  s: ReturnType<typeof usePlan>;
  staff: Map<string, StaffInfo>;
  orderSel: Selection | null;
  staffSel: Selection | null;
  routeMode: RouteMode;
  setRouteMode: (m: RouteMode) => void;
  selectOrder: (id: string, source: Source) => void;
  selectStaff: (id: string | null, source: Source, fit: boolean) => void;
  mapMemory: MapMemory;
  counts: Partial<Record<PanelId, { n: number; warn?: boolean }>>;
  openPanel: (id: PanelId) => void;
}

export const DockDataContext = createContext<DockData | null>(null);
function useDock(): DockData {
  const d = useContext(DockDataContext);
  if (!d) throw new Error('DockDataContext missing');
  return d;
}

/* ---------------------------------------------------------------- содержимое панелей */

function Welcome() {
  const { s } = useDock();
  return (
    <div className="welcome">
      {s.busy === 'load' ? (
        <>
          <h2>Строим план на день</h2>
          <p>Распределяем наряды по маршрутам сотрудников. Обычно до 15 секунд.</p>
        </>
      ) : s.busy === 'init' ? (
        <p>Подключаемся к сервису планирования…</p>
      ) : (
        <>
          <h2>День не загружен</h2>
          <p>
            Ожидаем данные через API (PUT /api/v1/days) или откройте новый день. Как только день появится на
            сервере, окна обновятся сами.
          </p>
        </>
      )}
    </div>
  );
}

/** Пустое состояние поверх окон, когда на сервере нет дня (день ещё не пришёл по API или очищен). */
export function EmptyDay({ busy, onCreate }: { busy: boolean; onCreate?: (date: string) => Promise<boolean> }) {
  const [form, setForm] = useState(false);
  return (
    <div className="empty-day" role="status" aria-live="polite">
      <div className="empty-day__card">
        {busy ? (
          <>
            <h2>Создаём день</h2>
            <p>Строим пустой план на день. Это занимает несколько секунд.</p>
          </>
        ) : (
          <>
            <h2>День не загружен</h2>
            <p>
              Ожидаем данные через API (<code>PUT /api/v1/days</code>) или откройте новый день. Как только день
              появится на сервере, план покажется сам — без перезагрузки страницы.
            </p>
            {onCreate &&
              (form ? (
                <CreateDayForm onCreate={onCreate} busy={busy} onCancel={() => setForm(false)} />
              ) : (
                <Button size="m" label="Создать день" onClick={() => setForm(true)} />
              ))}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyNote({ text }: { text: string }) {
  return <p className="empty">{text}</p>;
}

function OrdersPanel() {
  const { s, orderSel, selectOrder } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? <OrdersTable orders={s.orders} selected={orderSel} onSelect={(id) => selectOrder(id, 'table')} /> : <Welcome />}
    </div>
  );
}

function LatePanel() {
  const { s, orderSel, selectOrder } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? (
        <LateRiskList plan={s.plan} orders={s.orders} selected={orderSel} onSelect={(id) => selectOrder(id, 'late')} />
      ) : (
        <EmptyNote text="Наряды с риском опоздания появятся после загрузки дня." />
      )}
    </div>
  );
}

function UnassignedPanel() {
  const { s, orderSel, selectOrder } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? (
        <UnassignedList
          plan={s.plan}
          orders={s.orders}
          reference={s.reference}
          selected={orderSel}
          onSelect={(id) => selectOrder(id, 'unassigned')}
        />
      ) : (
        <EmptyNote text="Нераспределённые наряды и группы ПГП появятся после загрузки дня." />
      )}
    </div>
  );
}

function ScenariosPanel() {
  const { s, orderSel, selectOrder } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? (
        <ScenariosView
          date={s.demo?.date ?? s.plan.date}
          planVersion={s.plan.plan_version}
          reference={s.reference}
          selected={orderSel}
          onSelect={(id) => selectOrder(id, 'scenarios')}
        />
      ) : (
        <EmptyNote text="Сценарии «меньше сотрудников» и «запас АО» считаются по загруженному дню." />
      )}
    </div>
  );
}

function MapPanel() {
  const { s, staff, staffSel, orderSel, routeMode, setRouteMode, selectOrder, selectStaff, mapMemory } = useDock();
  return (
    <div className="dock-panel map-panel">
      <DispatchMap
        memory={mapMemory}
        serviceCenters={s.reference?.service_centers ?? EMPTY_SC}
        orders={s.orders}
        routes={s.plan?.routes ?? EMPTY_ROUTES}
        staff={staff}
        day={s.plan?.date}
        staffSel={staffSel}
        orderSel={orderSel}
        routeMode={routeMode}
        onRouteMode={setRouteMode}
        onSelectStaff={(id, fit) => selectStaff(id, 'map', fit)}
        onSelectOrder={(id) => selectOrder(id, 'map')}
      />
    </div>
  );
}

function GanttPanel() {
  const { s, staff, staffSel, orderSel, selectOrder, selectStaff } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? (
        <GanttChart
          plan={s.plan}
          orders={s.orders}
          staff={staff}
          serviceCenters={s.reference?.service_centers ?? EMPTY_SC}
          staffSel={staffSel}
          orderSel={orderSel}
          onSelectStaff={(id, fit) => selectStaff(id, 'gantt', fit)}
          onSelectOrder={(id) => selectOrder(id, 'gantt')}
        />
      ) : (
        <section className="panel gantt gantt--empty">
          <p>Визиты, перерывы и заезды в СЦ по сотрудникам на шкале смены появятся после загрузки дня.</p>
        </section>
      )}
    </div>
  );
}

function StatsPanel() {
  const { s, staff, staffSel, orderSel, selectOrder, selectStaff } = useDock();
  return (
    <div className="dock-panel">
      {s.plan ? (
        <StatsView
          plan={s.plan}
          orders={s.orders}
          workTypes={s.reference?.work_types}
          staff={staff}
          session={s.sessionMetrics}
          staffSel={staffSel}
          orderSel={orderSel}
          onSelectStaff={(id) => selectStaff(id, 'stats', !!id)}
          onSelectOrder={(id) => selectOrder(id, 'stats')}
        />
      ) : s.busy === 'load' || s.busy === 'init' ? (
        <Welcome />
      ) : (
        <EmptyNote text="Загрузите день: здесь появятся динамика плана по версиям, загрузка сотрудников и причины нераспределённых нарядов." />
      )}
    </div>
  );
}

export const DOCK_COMPONENTS: Record<PanelId, FunctionComponent<IDockviewPanelProps>> = {
  orders: OrdersPanel,
  late: LatePanel,
  unassigned: UnassignedPanel,
  scenarios: ScenariosPanel,
  map: MapPanel,
  gantt: GanttPanel,
  stats: StatsPanel,
};

/* ---------------------------------------------------------------- вкладка */

const CloseIcon = () => (
  <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
    <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** Вкладка в стиле TabsItem Атомаро: название, счётчик, крестик; средняя кнопка мыши тоже закрывает. */
export function DockTab({ api }: IDockviewPanelHeaderProps) {
  const { counts } = useDock();
  const id = api.id as PanelId;
  const count = counts[id];
  const title = PANEL_TITLE[id] ?? api.title ?? id;
  const close = () => api.close();
  const onPointerUp = (e: PointerEvent) => {
    if (e.button === 1) {
      e.preventDefault();
      close();
    }
  };
  return (
    <div className="dk-tab" title={title} onPointerUp={onPointerUp}>
      <span className="dk-tab__title">{title}</span>
      {count && <span className={`tabs__count${count.warn ? ' tabs__count--warn' : ''}`}>{count.n}</span>}
      <button
        type="button"
        className="dk-tab__close"
        aria-label={`Закрыть панель «${title}»`}
        title="Закрыть панель"
        onPointerDown={(e) => e.preventDefault()}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          close();
        }}
      >
        <CloseIcon />
      </button>
    </div>
  );
}

/* ---------------------------------------------------------------- действия группы */

const FloatIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <rect x="2.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
    <path d="M6 3.5V3a.5.5 0 0 1 .5-.5h7a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5H13" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
  </svg>
);
const DockIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
    <path d="M9.5 2.5v11" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);
const MaxIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9 7M2.5 13.5 7 9" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const RestoreIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path d="M13.5 6.5h-4v-4M2.5 9.5h4v4M9.5 6.5 14 2M6.5 9.5 2 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** Справа в заголовке группы: открепить в плавающее окно / закрепить обратно, развернуть / вернуть. */
export function GroupActions({ api, containerApi, group, panels }: IDockviewHeaderActionsProps) {
  const [loc, setLoc] = useState(api.location.type);
  const [max, setMax] = useState(() => api.isMaximized());
  useEffect(() => {
    const a = api.onDidLocationChange((e) => setLoc(e.location.type));
    const b = containerApi.onDidMaximizedGroupChange(() => setMax(api.isMaximized()));
    setLoc(api.location.type);
    return () => {
      a.dispose();
      b.dispose();
    };
  }, [api, containerApi]);

  const floating = loc === 'floating';
  const float = () => {
    const w = Math.min(720, Math.round(containerApi.width * 0.5));
    const h = Math.min(520, Math.round(containerApi.height * 0.6));
    containerApi.addFloatingGroup(group, {
      x: Math.max(16, Math.round((containerApi.width - w) / 2)),
      y: Math.max(16, Math.round((containerApi.height - h) / 3)),
      width: w,
      height: h,
    });
  };
  const dock = () => api.moveTo({ position: dockPositionFor(panels.map((p) => p.id)) });

  return (
    <div className="dk-actions">
      {loc === 'grid' && (
        <button
          type="button"
          className="dk-action"
          title={max ? 'Вернуть раскладку' : 'Развернуть на весь экран'}
          aria-label={max ? 'Вернуть раскладку' : 'Развернуть на весь экран'}
          onClick={() => (max ? api.exitMaximized() : api.maximize())}
        >
          {max ? <RestoreIcon /> : <MaxIcon />}
        </button>
      )}
      {(loc === 'grid' || floating) && !max && (
        <button
          type="button"
          className="dk-action"
          title={floating ? 'Закрепить в раскладке' : 'Открепить в плавающее окно'}
          aria-label={floating ? 'Закрепить в раскладке' : 'Открепить в плавающее окно'}
          onClick={floating ? dock : float}
        >
          {floating ? <DockIcon /> : <FloatIcon />}
        </button>
      )}
    </div>
  );
}

/** Все панели закрыты — подсказываем, как вернуть. */
export function DockWatermark(_: IWatermarkPanelProps) {
  const { openPanel } = useDock();
  return (
    <div className="dk-watermark">
      <h2>Все окна закрыты</h2>
      <p>Откройте нужные панели через меню «Окна» в шапке или верните раскладку по умолчанию.</p>
      <div className="dk-watermark__actions">
        <button type="button" className="btn btn--s btn--secondary btn--accent" onClick={() => openPanel('orders')}>
          Открыть «Наряды»
        </button>
        <button type="button" className="btn btn--s btn--secondary btn--accent" onClick={() => openPanel('map')}>
          Открыть «Карту»
        </button>
      </div>
    </div>
  );
}
