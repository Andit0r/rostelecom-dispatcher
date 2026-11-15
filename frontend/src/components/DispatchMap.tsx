import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { Route, ServiceCenter, UiOrder } from '../types/api';
import { REASON, STATUS, hhmm, window2 } from '../format';
import { SegmentedControl } from './ui';
import {
  PROFILE_SHORT,
  jointMainOf,
  locationIndex,
  routeColorValues,
  routeVar,
  stopLatLon,
  type LatLon,
  type RouteMode,
  type Selection,
  type StaffInfo,
} from '../planModel';

const EKB: L.LatLngTuple = [56.84, 60.61];

const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string);

function scPopup(sc: ServiceCenter): string {
  return (
    `<div class="map-popup"><strong>${esc(sc.name)}</strong><br>${esc(sc.address)}<br>` +
    `Режим работы: ${esc(sc.opening_hours.open)}–${esc(sc.opening_hours.close)}<br>` +
    `Пополнение: ${sc.restock_duration_min} мин</div>`
  );
}

/** Категория маркера: риск опоздания выделяется отдельно от «в маршруте». */
type Cat = 'LATE' | 'ROUTED' | 'UNASSIGNED' | 'PGP' | 'CANCELLED';
const CATS: { key: Cat; label: string }[] = [
  { key: 'LATE', label: 'Риск опоздания' },
  { key: 'ROUTED', label: 'В маршруте' },
  { key: 'UNASSIGNED', label: 'Не распределён' },
  { key: 'PGP', label: 'ПГП' },
  { key: 'CANCELLED', label: 'Отменён' },
];
/** Порядок отрисовки на canvas: последние — сверху. */
const DRAW_ORDER: Cat[] = ['CANCELLED', 'UNASSIGNED', 'ROUTED', 'PGP', 'LATE'];

const catOf = (o: UiOrder): Cat => {
  if (o.status === 'ROUTED') return o.late_risk ? 'LATE' : 'ROUTED';
  if (o.status === 'PGP') return 'PGP';
  if (o.status === 'CANCELLED') return 'CANCELLED';
  return 'UNASSIGNED';
};

/** Canvas не понимает var(--…), поэтому значения токенов читаем из CSS. */
function palette() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n: string) => cs.getPropertyValue(n).trim();
  return {
    accent: v('--atmr-accent'),
    halo: v('--atmr-bg-surface1'),
    neutral: v('--atmr-status-neutral'),
    pgp: v('--atmr-map-pgp'),
    late: v('--atmr-map-late'),
    cancelled: v('--atmr-border-strong'),
    ink: v('--atmr-text-primary'),
  };
}
type Palette = ReturnType<typeof palette>;

function style(cat: Cat, p: Palette): L.CircleMarkerOptions {
  switch (cat) {
    case 'LATE':
      return { radius: 7, color: p.halo, weight: 2, fillColor: p.late, fillOpacity: 1 };
    case 'ROUTED':
      return { radius: 5, color: p.halo, weight: 1.5, fillColor: p.accent, fillOpacity: 1 };
    case 'UNASSIGNED':
      return { radius: 5, color: p.neutral, weight: 2, fillColor: p.halo, fillOpacity: 1 };
    case 'PGP':
      return { radius: 5.5, color: p.halo, weight: 1.5, fillColor: p.pgp, fillOpacity: 1 };
    case 'CANCELLED':
      return { radius: 4, color: p.cancelled, weight: 1, opacity: 0.7, fillColor: p.cancelled, fillOpacity: 0.35 };
  }
}

function orderPopup(o: UiOrder, cat: Cat): string {
  const rows: string[] = [
    `<strong>${esc(o.order_id)}</strong> · ${esc(STATUS[o.status].label)}`,
    esc(o.work_type_name),
    esc(o.address),
    `Слот: ${window2(o.slot)}`,
  ];
  if (o.status === 'ROUTED') {
    rows.push(`Исполнитель: ${esc(o.staff_name ?? o.staff_id ?? '—')}`);
    if (o.partner_staff_id) rows.push(`Совместный наряд, напарник: ${esc(o.partner_staff_name ?? o.partner_staff_id)}`);
    rows.push(`План: прибытие ${hhmm(o.planned_arrival)}, работа ${hhmm(o.planned_start)}–${hhmm(o.planned_end)}`);
  }
  if (cat === 'LATE' && o.late_risk) {
    const m = o.late_risk.lateness_min;
    rows.push(`<span class="map-popup__warn">Риск опоздания${m != null ? `: +${Math.round(m)} мин` : ''}</span>`);
  }
  if (o.status === 'UNASSIGNED' || o.status === 'UNKNOWN') {
    const r = o.reason_text ?? (o.reason_code ? REASON[o.reason_code] : null);
    if (r) rows.push(`Причина: ${esc(r)}`);
  }
  if (o.status === 'PGP' && o.group_id) rows.push(`Группа ПГП: ${esc(o.group_id)}`);
  if (o.status === 'CANCELLED' && o.cancelled_at) rows.push(`Отменён в ${hhmm(o.cancelled_at)}`);
  return `<div class="map-popup">${rows.join('<br>')}</div>`;
}

/* Иконки остановок маршрута (inline SVG, цвет — currentColor). */
const ICON_RESTOCK =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 14 4.5v7L8 14.5 2 11.5v-7z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M2.4 4.7 8 7.5l5.6-2.8M8 7.5v6.6" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';
const ICON_BREAK =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 6h9v3.5a4 4 0 0 1-4 4h-1a4 4 0 0 1-4-4z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M11.5 7.2h.8a1.8 1.8 0 0 1 0 3.6h-1M5.5 1.8v2M8.5 1.8v2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

/** Угол сегмента на экране (Меркатор конформный — угол не зависит от зума). */
function bearingDeg(a: LatLon, b: LatLon): number {
  const pa = L.CRS.EPSG3857.latLngToPoint(L.latLng(a), 0);
  const pb = L.CRS.EPSG3857.latLngToPoint(L.latLng(b), 0);
  return (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI;
}

/**
 * Состояние карты, которое переживает перемонтирование панели (закрыли/открыли окно,
 * сброс раскладки). Живёт в App, карта читает его при создании и обновляет сама.
 */
export interface MapMemory {
  center: L.LatLngTuple | null;
  zoom: number | null;
  /** Для какого дня и справочника уже сделан fitBounds (сравнение по ссылке). */
  fittedDay: string | undefined;
  fittedSc: ServiceCenter[] | null;
  /** Последние обработанные выборы: на перемонтировании старый выбор не центрирует карту повторно. */
  orderNonce: number;
  staffNonce: number;
  hidden: string[];
}
export const createMapMemory = (): MapMemory => ({
  center: null,
  zoom: null,
  fittedDay: undefined,
  fittedSc: null,
  orderNonce: 0,
  staffNonce: 0,
  hidden: [],
});

interface Props {
  memory: MapMemory;
  serviceCenters: ServiceCenter[];
  orders: UiOrder[];
  routes: Route[];
  staff: Map<string, StaffInfo>;
  /** Дата дня: смена даты или справочника = первая загрузка дня → fitBounds. */
  day?: string;
  staffSel: Selection | null;
  orderSel: Selection | null;
  routeMode: RouteMode;
  onRouteMode: (m: RouteMode) => void;
  onSelectStaff: (id: string | null, fit: boolean) => void;
  onSelectOrder: (id: string) => void;
}

export function DispatchMap({
  memory,
  serviceCenters,
  orders,
  routes,
  staff,
  day,
  staffSel,
  orderSel,
  routeMode,
  onRouteMode,
  onSelectStaff,
  onSelectOrder,
}: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const legendRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const scLayerRef = useRef<L.LayerGroup | null>(null);
  const orderLayerRef = useRef<L.LayerGroup | null>(null);
  const routeLayerRef = useRef<L.LayerGroup | null>(null);
  const selLayerRef = useRef<L.LayerGroup | null>(null);
  const rendererRef = useRef<L.Canvas | null>(null);
  const markersRef = useRef(new Map<string, L.CircleMarker>());
  const openIdRef = useRef<string | null>(null);
  const ordersRef = useRef(orders);
  ordersRef.current = orders;
  const cbRef = useRef({ onSelectStaff, onSelectOrder });
  cbRef.current = { onSelectStaff, onSelectOrder };
  const [hidden, setHidden] = useState<Set<Cat>>(() => new Set(memory.hidden as Cat[]));
  const memRef = useRef(memory);
  memRef.current = memory;
  /** Действие с видом карты, отложенное до момента, когда у контейнера появится размер
   *  (панель только что открыли или она во вкладке, которая ещё не показана). */
  const pendingViewRef = useRef<((map: L.Map) => void) | null>(null);
  const withView = (fn: (map: L.Map) => void) => {
    const map = mapRef.current;
    const box = boxRef.current;
    if (!map || !box) return;
    if (box.clientWidth > 0 && box.clientHeight > 0) {
      map.invalidateSize();
      pendingViewRef.current = null;
      fn(map);
    } else {
      pendingViewRef.current = fn;
    }
  };

  const selectedStaff = staffSel?.id ?? null;
  const ordersById = useMemo(() => new Map(orders.map((o) => [o.order_id, o])), [orders]);

  /** Точки маршрутов (СЦ → визиты → СЦ) с остановками для иконок. */
  const routeGeo = useMemo(() => {
    const loc = locationIndex(orders, serviceCenters);
    return routes.map((r) => {
      const pts: LatLon[] = [];
      const marks: { kind: 'RESTOCK' | 'BREAK'; at: LatLon; text: string }[] = [];
      const visits: { at: LatLon; n: number; id: string }[] = [];
      let n = 0;
      for (const s of r.stops) {
        const at = stopLatLon(s, r.service_center_id, loc, ordersById) ?? pts[pts.length - 1];
        if (!at) continue;
        const last = pts[pts.length - 1];
        if (!last || last[0] !== at[0] || last[1] !== at[1]) pts.push(at);
        if (s.type === 'VISIT' && s.order_id) visits.push({ at, n: ++n, id: jointMainOf(s.order_id) ?? s.order_id });
        if (s.type === 'RESTOCK') marks.push({ kind: 'RESTOCK', at, text: `Пополнение в СЦ ${hhmm(s.start)}–${hhmm(s.end)}` });
        if (s.type === 'BREAK') marks.push({ kind: 'BREAK', at, text: `Перерыв ${hhmm(s.start)}–${hhmm(s.end)}` });
      }
      return { route: r, pts, marks, visits };
    });
  }, [routes, orders, serviceCenters, ordersById]);

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const mem = memRef.current;
    const map = L.map(box).setView(mem.center ?? EKB, mem.zoom ?? 11);
    map.attributionControl.setPrefix(false);
    map.on('moveend zoomend', () => {
      if (!box.clientWidth || !box.clientHeight) return;
      const c = map.getCenter();
      memRef.current.center = [c.lat, c.lng];
      memRef.current.zoom = map.getZoom();
    });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    mapRef.current = map;
    rendererRef.current = L.canvas({ padding: 0.5 });
    // Маршруты и наряды в одном canvas: линии уводим назад bringToBack, точки остаются сверху.
    routeLayerRef.current = L.layerGroup().addTo(map);
    orderLayerRef.current = L.layerGroup().addTo(map);
    selLayerRef.current = L.layerGroup().addTo(map);
    scLayerRef.current = L.layerGroup().addTo(map);
    // Панель меняет размер при перетаскивании границ, докинге и откреплении — подстраиваем карту.
    const ro = new ResizeObserver(() => {
      if (!box.clientWidth || !box.clientHeight) return;
      map.invalidateSize();
      const pending = pendingViewRef.current;
      if (pending) {
        pendingViewRef.current = null;
        pending(map);
      }
    });
    ro.observe(box);
    return () => {
      ro.disconnect();
      pendingViewRef.current = null;
      map.remove();
      mapRef.current = null;
      scLayerRef.current = null;
      orderLayerRef.current = null;
      routeLayerRef.current = null;
      selLayerRef.current = null;
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    const layer = scLayerRef.current;
    if (!layer) return;
    layer.clearLayers();
    for (const sc of serviceCenters) {
      const icon = L.divIcon({
        className: 'map-sc',
        html: '<span class="map-sc__dot"></span>',
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([sc.lat, sc.lon], { icon, title: sc.name })
        .bindTooltip(sc.name, { direction: 'top', offset: [0, -10] })
        .bindPopup(scPopup(sc))
        .addTo(layer);
    }
  }, [serviceCenters]);

  // Наряды: перерисовка слоя на каждую версию плана, вид карты не трогаем.
  useEffect(() => {
    const layer = orderLayerRef.current;
    const renderer = rendererRef.current;
    if (!layer || !renderer) return;
    const reopen = openIdRef.current;
    layer.clearLayers();
    markersRef.current.clear();
    const p = palette();
    const byCat = new Map<Cat, UiOrder[]>();
    for (const o of orders) {
      const c = catOf(o);
      if (hidden.has(c)) continue;
      (byCat.get(c) ?? byCat.set(c, []).get(c)!).push(o);
    }
    for (const cat of DRAW_ORDER) {
      for (const o of byCat.get(cat) ?? []) {
        const ll: L.LatLngTuple = [o.lat, o.lon];
        if (cat === 'LATE') {
          L.circleMarker(ll, { renderer, radius: 11, color: p.late, weight: 2, opacity: 0.55, fill: false, interactive: false }).addTo(layer);
        }
        const m = L.circleMarker(ll, { renderer, ...style(cat, p) })
          .bindPopup(() => orderPopup(o, cat), { maxWidth: 280 })
          .on('click', () => cbRef.current.onSelectOrder(o.order_id))
          .on('popupopen', () => (openIdRef.current = o.order_id))
          .on('popupclose', () => {
            if (openIdRef.current === o.order_id) openIdRef.current = null;
          })
          .addTo(layer);
        markersRef.current.set(o.order_id, m);
        if (o.order_id === reopen) m.openPopup();
      }
    }
  }, [orders, hidden]);

  // Маршруты: линия на сотрудника, выбранный — поверх, с нумерацией визитов и стрелками.
  useEffect(() => {
    const layer = routeLayerRef.current;
    const renderer = rendererRef.current;
    if (!layer || !renderer) return;
    layer.clearLayers();
    if (routeMode === 'none') return;
    const colors = routeColorValues();
    const p = palette();
    const visible = routeGeo.filter((g) => routeMode === 'all' || g.route.staff_id === selectedStaff);
    const isFocus = (id: string) => !selectedStaff || id === selectedStaff;
    // bringToBack: первым уводим назад выбранный, затем остальные — они окажутся под ним.
    const ordered = [...visible].sort((a, b) => Number(isFocus(b.route.staff_id)) - Number(isFocus(a.route.staff_id)));
    for (const g of ordered) {
      if (g.pts.length < 2) continue;
      const id = g.route.staff_id;
      const info = staff.get(id);
      const color = colors[(info?.color ?? 0) % colors.length] || p.accent;
      const solo = id === selectedStaff;
      const focus = isFocus(id);
      const off = g.route.status === 'UNAVAILABLE';
      const line = L.polyline(g.pts, {
        renderer,
        color,
        weight: solo ? 4 : focus ? 2.5 : 2,
        opacity: solo ? 0.95 : focus ? 0.7 : 0.14,
        dashArray: off ? '6 6' : undefined,
        lineJoin: 'round',
      })
        .bindTooltip(
          `${esc(info?.name ?? id)}${info?.profiles.length ? ` · ${info.profiles.map((x) => PROFILE_SHORT[x]).join(', ')}` : ''}` +
            `<br>Визитов: ${g.visits.length}${off ? ' · недоступен' : ''}`,
          { sticky: true },
        )
        .on('click', () => cbRef.current.onSelectStaff(id, false))
        .addTo(layer);
      line.bringToBack();
      if (solo) {
        L.polyline(g.pts, { renderer, color: p.halo, weight: 8, opacity: 0.9, interactive: false }).addTo(layer).bringToBack();
        for (let i = 1; i < g.pts.length; i++) {
          const a = g.pts[i - 1];
          const b = g.pts[i];
          const mid: LatLon = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
          L.marker(mid, {
            interactive: false,
            keyboard: false,
            icon: L.divIcon({
              className: 'map-arrow',
              html: `<span style="transform:rotate(${bearingDeg(a, b).toFixed(1)}deg);color:${color}"></span>`,
              iconSize: [14, 14],
              iconAnchor: [7, 7],
            }),
          }).addTo(layer);
        }
        for (const v of g.visits) {
          L.marker(v.at, {
            interactive: false,
            keyboard: false,
            zIndexOffset: 200,
            icon: L.divIcon({
              className: 'map-num',
              html: `<span style="background:${color}">${v.n}</span>`,
              iconSize: [18, 18],
              iconAnchor: [-3, 21],
            }),
          }).addTo(layer);
        }
      }
      if (focus) {
        for (const mk of g.marks) {
          L.marker(mk.at, {
            keyboard: false,
            zIndexOffset: 300,
            icon: L.divIcon({
              className: `map-stop map-stop--${mk.kind.toLowerCase()}`,
              html: `<span style="color:${color}">${mk.kind === 'RESTOCK' ? ICON_RESTOCK : ICON_BREAK}</span>`,
              iconSize: [20, 20],
              iconAnchor: mk.kind === 'RESTOCK' ? [24, 22] : [22, -2],
            }),
          })
            .bindTooltip(`${esc(info?.name ?? id)}<br>${mk.text}`, { direction: 'top' })
            .on('click', () => cbRef.current.onSelectStaff(id, false))
            .addTo(layer);
        }
      }
    }
  }, [routeGeo, routeMode, selectedStaff, staff]);

  // Выбранный наряд: кольцо; при выборе из таблицы/Ганта — центрируем и открываем попап.
  useEffect(() => {
    const map = mapRef.current;
    const layer = selLayerRef.current;
    const renderer = rendererRef.current;
    if (!map || !layer || !renderer) return;
    layer.clearLayers();
    const o = orderSel ? ordersById.get(orderSel.id) : undefined;
    if (!o) return;
    const p = palette();
    L.circleMarker([o.lat, o.lon], { renderer, radius: 14, color: p.ink, weight: 2.5, fill: false, interactive: false })
      .addTo(layer)
      .bringToFront();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderSel?.id, ordersById]);

  // fitBounds только при первой загрузке дня (новая дата или новый справочник после «Загрузить день»).
  // Память в App помнит, для чего вписывали: перемонтирование панели масштаб не сбрасывает.
  // Эффект объявлен раньше эффектов выбора, чтобы при открытии панели выбор применился последним.
  useEffect(() => {
    const mem = memRef.current;
    if (mem.fittedDay === day && mem.fittedSc === serviceCenters) return;
    const pts: L.LatLngTuple[] = [
      ...ordersRef.current.map((o) => [o.lat, o.lon] as L.LatLngTuple),
      ...serviceCenters.map((sc) => [sc.lat, sc.lon] as L.LatLngTuple),
    ];
    if (!pts.length) return;
    mem.fittedDay = day;
    mem.fittedSc = serviceCenters;
    withView((map) => {
      const legendH = legendRef.current?.offsetHeight ?? 0;
      map.fitBounds(L.latLngBounds(pts), {
        paddingTopLeft: [32, 32],
        paddingBottomRight: [32, legendH + 24],
        maxZoom: 13,
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day, serviceCenters]);

  useEffect(() => {
    const mem = memRef.current;
    if (!orderSel || orderSel.nonce === mem.orderNonce) return;
    mem.orderNonce = orderSel.nonce;
    if (orderSel.source === 'map') return;
    const o = ordersRef.current.find((x) => x.order_id === orderSel.id);
    if (!o) return;
    withView((map) => {
      map.setView([o.lat, o.lon], Math.max(map.getZoom(), 14), { animate: false });
      const m = markersRef.current.get(o.order_id);
      if (m) m.openPopup();
      else L.popup({ maxWidth: 280 }).setLatLng([o.lat, o.lon]).setContent(orderPopup(o, catOf(o))).openOn(map);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderSel?.nonce]);

  // Вписываем маршрут, когда сотрудника выбрали в списке над картой или в Ганте.
  useEffect(() => {
    const mem = memRef.current;
    if (!staffSel || staffSel.nonce === mem.staffNonce) return;
    mem.staffNonce = staffSel.nonce;
    if (!staffSel.fit) return;
    const g = routeGeo.find((x) => x.route.staff_id === staffSel.id);
    if (!g || !g.pts.length) return;
    withView((map) => {
      const legendH = legendRef.current?.offsetHeight ?? 0;
      map.fitBounds(L.latLngBounds(g.pts), { paddingTopLeft: [40, 40], paddingBottomRight: [40, legendH + 32], maxZoom: 14 });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [staffSel?.nonce]);

  const counts = useMemo(() => {
    const c: Record<Cat, number> = { LATE: 0, ROUTED: 0, UNASSIGNED: 0, PGP: 0, CANCELLED: 0 };
    for (const o of orders) c[catOf(o)]++;
    return c;
  }, [orders]);

  /** Выпадающий список: сотрудники с маршрутами, сгруппированные по СЦ. */
  const groups = useMemo(() => {
    const scName = new Map(serviceCenters.map((s) => [s.service_center_id, s.name]));
    const by = new Map<string, { info: StaffInfo; visits: number; off: boolean }[]>();
    for (const r of routes) {
      const info = staff.get(r.staff_id) ?? { id: r.staff_id, name: r.staff_id, scId: r.service_center_id, profiles: [], color: 0 };
      const key = r.service_center_id;
      const visits = r.stops.filter((s) => s.type === 'VISIT').length;
      (by.get(key) ?? by.set(key, []).get(key)!).push({ info, visits, off: r.status === 'UNAVAILABLE' });
    }
    return [...by.entries()]
      .map(([sc, list]) => ({ sc, name: scName.get(sc) ?? sc, list: list.sort((a, b) => a.info.name.localeCompare(b.info.name, 'ru')) }))
      .sort((a, b) => a.name.localeCompare(b.name, 'ru'));
  }, [routes, staff, serviceCenters]);

  const toggle = (c: Cat) =>
    setHidden((h) => {
      const n = new Set(h);
      if (!n.delete(c)) n.add(c);
      memRef.current.hidden = [...n];
      return n;
    });

  const selInfo = selectedStaff ? staff.get(selectedStaff) : undefined;

  return (
    <>
      <div className="map-tools">
        <label className="map-tools__staff">
          <span
            className="map-tools__swatch"
            style={selInfo ? { background: routeVar(selInfo.color) } : undefined}
            aria-hidden
          />
          <select
            className="select"
            value={selectedStaff ?? ''}
            onChange={(e) => onSelectStaff(e.target.value || null, true)}
            aria-label="Сотрудник, маршрут которого выделить"
            disabled={!routes.length}
          >
            <option value="">Все сотрудники</option>
            {groups.map((g) => (
              <optgroup key={g.sc} label={g.name}>
                {g.list.map(({ info, visits, off }) => (
                  <option key={info.id} value={info.id}>
                    {info.name} — {visits} виз.{off ? ', недоступен' : ''}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <span className="map-tools__label">Маршруты</span>
        <SegmentedControl
          size="s"
          variant="secondary"
          value={routeMode}
          onChange={(v) => onRouteMode(v as RouteMode)}
          disabledAll={!routes.length}
          items={[
            { index: 'all', label: 'Все' },
            { index: 'selected', label: 'Выбранный' },
            { index: 'none', label: 'Скрыть' },
          ]}
        />
      </div>
      <div className="dispatch-map-wrap">
        <div ref={boxRef} className="dispatch-map" />
        {routeMode === 'selected' && !selectedStaff && routes.length > 0 && (
          <p className="map-hint">Выберите сотрудника в списке, на линии маршрута или в диаграмме Ганта</p>
        )}
        {orders.length > 0 && (
          <div ref={legendRef} className="map-legend" role="group" aria-label="Наряды на карте">
            {CATS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                className="map-legend__item"
                aria-pressed={!hidden.has(key)}
                title={hidden.has(key) ? 'Показать на карте' : 'Скрыть с карты'}
                onClick={() => toggle(key)}
              >
                <span className={`map-legend__dot map-legend__dot--${key.toLowerCase()}`} />
                {label}
                <span className="map-legend__count">{counts[key]}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
