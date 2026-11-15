// Типы по data/api_spec.yaml (/api/v1) и вспомогательному /ui (backend/app/ui_api.py).

export type DateTime = string;
export type Technology = 'PON' | 'FTTB' | 'XDSL' | 'DOCSIS';
export type Profile = 'INSTALL' | 'LTP3';
export type WorkTypeCode =
  | 'INST_PON_MKD'
  | 'INST_FTTB_MKD'
  | 'INST_PRIVATE_VLS'
  | 'ADD_SERVICE'
  | 'SURVEY_PRIVATE'
  | 'REPAIR'
  | 'DEGRADATION'
  | 'DISMANTLE';
export type BuildingType = 'MKD' | 'PRIVATE';

export interface TimeWindow {
  start: DateTime;
  end: DateTime;
}
export interface Equipment {
  ONT: number;
  ROUTER: number;
  STB: number;
}

export interface Order {
  order_id: string;
  work_type_code: WorkTypeCode;
  technology: Technology;
  building_id: string;
  address: string;
  lat: number;
  lon: number;
  building_type: BuildingType;
  requires_height: boolean;
  access_node_id: string;
  registered_at: DateTime;
  slot: TimeWindow;
  latest_start: DateTime;
  deadline: DateTime;
  required_equipment: Equipment;
  is_repeat: boolean;
  previous_order_id?: string | null;
}

export type StopType = 'DEPOT_START' | 'VISIT' | 'BREAK' | 'RESTOCK' | 'DEPOT_END';
export type StopStatus = 'PLANNED' | 'IN_PROGRESS' | 'DONE';

export interface Stop {
  seq: number;
  type: StopType;
  location_id: string;
  order_id?: string | null;
  arrival: DateTime;
  start: DateTime;
  end: DateTime;
  travel_sec?: number;
  distance_m?: number;
  status?: StopStatus;
  equipment_after?: Equipment;
  explanation?: string;
}

export interface RouteTotals {
  visits?: number;
  travel_sec?: number;
  distance_m?: number;
  work_min?: number;
  utilization?: number;
}

export interface Route {
  staff_id: string;
  service_center_id: string;
  status: 'ACTIVE' | 'UNAVAILABLE';
  unavailable_from?: DateTime | null;
  unavailable_until?: DateTime | null;
  stops: Stop[];
  totals?: RouteTotals;
}

export type UnassignedReason =
  | 'NO_QUALIFIED_STAFF'
  | 'NO_SHIFT_TIME'
  | 'SLOT_UNREACHABLE'
  | 'NO_EQUIPMENT'
  | 'STAFF_UNAVAILABLE'
  | 'OTHER';

export interface UnassignedOrder {
  order_id: string;
  reason_code: UnassignedReason;
  reason_text: string;
  suggested_action: 'RESCHEDULE' | 'REASSIGN' | 'ESCALATE';
  proposed_slot?: TimeWindow | null;
  since?: DateTime | null;
}

export interface PgpGroup {
  group_id: string;
  access_node_id: string;
  order_ids: string[];
  detected_at: DateTime;
  action: 'TRANSFER_TO_STATION';
  comment?: string;
}

export interface CancelledOrder {
  order_id: string;
  cancelled_at: DateTime;
  event_id?: string | null;
}

export type AlertType = 'LATE_RISK' | 'PGP_DETECTED' | 'UNASSIGNED';
export type AlertAction = 'REASSIGN' | 'RESCHEDULE' | 'TRANSFER_TO_STATION' | 'NONE';

export interface Alert {
  alert_id: string;
  created_at: DateTime;
  type: AlertType;
  status: 'ACTIVE' | 'RESOLVED';
  resolved_at?: DateTime | null;
  order_id?: string | null;
  group_id?: string | null;
  staff_id?: string | null;
  forecast_start?: DateTime | null;
  lateness_min?: number | null;
  suggested_action: AlertAction;
  suggested_staff_id?: string | null;
  proposed_slot?: TimeWindow | null;
  applied?: boolean;
  message: string;
}

export interface PlanSummary {
  orders_total?: number;
  visits_planned?: number;
  unassigned?: number;
  pgp_orders?: number;
  cancelled?: number;
  travel_sec_total?: number;
  utilization_cv?: number;
}

export interface Plan {
  date: string;
  plan_version: number;
  based_on_event_id?: string | null;
  as_of: DateTime;
  computation_ms?: number;
  routes: Route[];
  unassigned: UnassignedOrder[];
  pgp_groups: PgpGroup[];
  cancelled: CancelledOrder[];
  alerts: Alert[];
  summary?: PlanSummary;
}

export interface ErrorResponse {
  error: { code: string; message: string; details?: { field?: string | null; message: string }[] };
}

// ---------------------------------------------------------------- справочники (/ui/reference)

export interface ServiceCenter {
  service_center_id: string;
  name: string;
  address: string;
  lat: number;
  lon: number;
  opening_hours: { open: string; close: string };
  restock_duration_min: number;
}

export interface Staff {
  staff_id: string;
  display_name?: string;
  service_center_id: string;
  profiles: Profile[];
  technologies: Technology[];
  height_permit: boolean;
  shift: {
    start: string;
    end: string;
    break_duration_min: number;
    break_window: { from: string; to: string };
  };
  equipment_start: Equipment;
  equipment_capacity: Equipment;
  experience_years?: number;
}

export interface WorkType {
  code: WorkTypeCode;
  name: string;
  profiles: Profile[];
  duration_min: number;
  max_start_delay_min: number | null;
  deadline_kind: 'SLOT' | 'HOURS' | 'WORKING_DAYS';
  deadline_value: number | null;
  repeat_deadline_value: number | null;
  pgp_relevant: boolean;
}

export interface AccessNodeInfo {
  access_node_id: string;
  node_type: 'OLT' | 'SWITCH' | 'DSLAM' | 'CMTS';
  technology: Technology;
  name: string;
  address?: string;
  lat: number;
  lon: number;
  buildings_count: number;
}

export interface UiReference {
  service_centers: ServiceCenter[];
  staff: Staff[];
  work_types: WorkType[];
  access_nodes: AccessNodeInfo[];
}

// ---------------------------------------------------------------- /ui

export type UiOrderStatus = 'ROUTED' | 'UNASSIGNED' | 'PGP' | 'CANCELLED' | 'UNKNOWN';

export interface UiOrder extends Order {
  work_type_name: string;
  status: UiOrderStatus;
  staff_id: string | null;
  staff_name: string | null;
  planned_arrival: DateTime | null;
  planned_start: DateTime | null;
  planned_end: DateTime | null;
  stop_status: StopStatus | null;
  explanation: string | null;
  group_id: string | null;
  reason_code: UnassignedReason | null;
  reason_text: string | null;
  suggested_action: UnassignedOrder['suggested_action'] | null;
  cancelled_at: DateTime | null;
  late_risk: { alert_id: string; lateness_min: number | null; suggested_action: AlertAction } | null;
  /** Совместный наряд (JOINT_ORDERS=on): напарник с визитом ORD-…-B в то же время. */
  partner_staff_id?: string | null;
  partner_staff_name?: string | null;
}

export interface UiDayOrders {
  date: string;
  plan_version: number;
  as_of: DateTime;
  orders: UiOrder[];
}

/** Состояние дня в шапке (дата, время и версия плана). */
export interface DemoStatus {
  loaded: boolean;
  date?: string;
  as_of?: DateTime;
  plan_version?: number;
}

/** Результат POST /ui/day/create. */
export interface DayCreateResult {
  status: string;
  date: string;
  now: DateTime;
  plan_version: number;
  orders: number;
  computation_ms?: number;
  /** catalog — справочников не было, взяты значения по умолчанию из каталога; existing — уже загруженные. */
  reference_loaded_from: 'catalog' | 'existing';
}

/** Результат POST /ui/day/rebuild. */
export interface DayRebuildResult {
  status: string;
  date: string;
  now: DateTime;
  plan_version: number;
  orders: number;
  computation_ms?: number;
}

/** Итог события (строка в шапке): новый/отменённый наряд из панели «Данные». */
export interface DemoStepItem {
  event_id: string;
  type: string;
  timestamp: DateTime;
  accepted: boolean;
  plan_version?: number;
  processing_ms?: number;
  message?: string | null;
  changed_staff_ids?: string[];
  error?: ErrorResponse;
}

/* ---------------------------------------------------------------- /ui/scenario (сценарный анализ) */

export type StaffCutStrategy =
  | 'random'
  | 'least_loaded'
  | 'by_profile:INSTALL'
  | 'by_profile:LTP3'
  | `by_service_center:${string}`;

export interface ScenarioMetrics {
  orders_total: number;
  visits: number;
  unassigned: number;
  unassigned_by_reason: Partial<Record<UnassignedReason, number>>;
  pgp_orders: number;
  in_slot_share: number;
  travel_h: number;
  staff_used: number;
  util_mean: number;
  util_cv: number;
  restocks: number;
}

export interface ScenarioAffectedOrder {
  order_id: string;
  base_staff_id: string;
  base_staff_removed: boolean;
  work_type_code: WorkTypeCode | null;
  is_repeat: boolean;
  slot: { start: DateTime; end: DateTime } | null;
  address: string | null;
  reason_code: UnassignedReason | 'PGP' | null;
  reason_text: string | null;
}

export interface StaffCutResult {
  date: string;
  strategy: string;
  remove_count: number;
  seed: number;
  budget_sec: number;
  base_source: 'current_plan_v1' | 'rebuilt_initial';
  removed: { staff_id: string; display_name: string | null; profiles: string[]; service_center_id: string; base_visits: number }[];
  base: ScenarioMetrics;
  scenario: ScenarioMetrics;
  delta: Record<string, number>;
  affected_orders: ScenarioAffectedOrder[];
  newly_routed: string[];
  reassigned: number;
  time_sec: number;
}

export interface EquipmentStat {
  p50: number;
  p90: number;
  max: number;
  current_start_avg: number;
  capacity: number;
}

export interface EquipmentRecommendation {
  profile: 'INSTALL' | 'LTP3';
  staff_on_shift: number;
  staff_with_visits: number;
  ONT: EquipmentStat;
  ROUTER: EquipmentStat;
  STB: EquipmentStat;
  staff_exceeding_current_start: number;
}

export interface EquipmentResult {
  date: string;
  mode: 'no_restock';
  budget_sec: number;
  base_source: 'current_plan_v1' | 'rebuilt_initial';
  current: ScenarioMetrics;
  no_restock: ScenarioMetrics;
  delta: Record<string, number>;
  visits_gained: number;
  travel_saved_h: number;
  recommended: EquipmentRecommendation[];
  time_sec: number;
}

/** Метрики плана в сценарии «пробки по часам»: travel_h — по базовой матрице,
 *  travel_traffic_h и опоздания — при исполнении плана с коэффициентами по часам. */
export interface TrafficMetrics extends ScenarioMetrics {
  travel_traffic_h: number;
  late_visits: number;
  late_min: number;
  on_time_visits: number;
  in_slot_share_traffic: number;
  violations: number;
  time_sec: number;
}

export interface TrafficResult {
  date: string;
  budget_sec: number;
  traffic_env: boolean;
  factors: { hour: number; factor: number }[];
  off: TrafficMetrics;
  on: TrafficMetrics;
  delta: Record<string, number>;
  time_sec: number;
}

// ---------------------------------------------------------------- /ui/employee (мобильный экран сотрудника)

export type MarkKind = 'CALLED' | 'ARRIVED' | 'DONE' | 'TRANSFER';
export type MobileAction = MarkKind | 'UNAVAILABLE';
export type StopState = 'PLANNED' | 'IN_PROGRESS' | 'DONE';

export interface EmployeeStaffItem {
  staff_id: string;
  display_name: string | null;
  profiles: Profile[];
  service_center_id: string;
  service_center_name: string | null;
  shift: { start: string; end: string };
  route_status: 'ACTIVE' | 'UNAVAILABLE';
  visits: number;
  visits_done: number;
}

export interface EmployeeStaffList {
  date: string;
  plan_version: number;
  as_of: DateTime;
  staff: EmployeeStaffItem[];
}

export interface EmployeeOrder {
  order_id: string;
  work_type_code: WorkTypeCode;
  work_type_name: string;
  technology: Technology;
  address: string;
  lat: number;
  lon: number;
  building_type: BuildingType;
  requires_height: boolean;
  slot: TimeWindow;
  latest_start: DateTime;
  required_equipment: Equipment;
  is_repeat: boolean;
}

export interface EmployeeStop {
  seq: number;
  type: StopType;
  location_id: string;
  location: { address: string; lat: number; lon: number; name?: string } | null;
  order_id: string | null;
  order: EmployeeOrder | null;
  arrival: DateTime;
  start: DateTime;
  end: DateTime;
  travel_sec: number | null;
  distance_m: number | null;
  plan_status: StopStatus | null;
  state: StopState;
  equipment_after: Equipment | null;
  explanation: string | null;
  marks: Record<MarkKind, DateTime | null> | null;
  next_mark: MarkKind | null;
  allowed_actions: MobileAction[];
  call_window: TimeWindow | null;
}

export interface EmployeeMark {
  order_id: string;
  kind: MarkKind;
  at: DateTime;
  recorded_at: DateTime;
  staff_id: string;
}

export interface EmployeeRoute {
  date: string;
  plan_version: number;
  as_of: DateTime;
  staff: {
    staff_id: string;
    display_name: string | null;
    profiles: Profile[];
    technologies: Technology[];
    service_center: { service_center_id: string; name: string; address: string; lat: number; lon: number } | null;
    shift: Staff['shift'];
  };
  on_shift: boolean;
  route_status: 'ACTIVE' | 'UNAVAILABLE' | null;
  unavailable_from: DateTime | null;
  unavailable_until: DateTime | null;
  allowed_actions: MobileAction[];
  progress: { visits_total: number; visits_done: number };
  next: {
    seq: number;
    order_id: string;
    call_window: TimeWindow | null;
    call_hint: string | null;
    next_mark: MarkKind | null;
  } | null;
  stops: EmployeeStop[];
  marks: EmployeeMark[];
}

export interface EmployeeMarkEvent {
  event_id: string;
  type: string;
  accepted: boolean;
  duplicate: boolean;
  message: string | null;
}

export interface EmployeeMarkResult {
  accepted: boolean;
  duplicate: boolean;
  kind: MobileAction;
  order_id: string | null;
  staff_id: string;
  at: DateTime;
  recorded_at: DateTime;
  event_sent: boolean;
  event: EmployeeMarkEvent | null;
  event_note: string | null;
  plan_version: number;
  route_changed: boolean;
  added_order_ids: string[];
  removed_order_ids: string[];
  changed_staff_ids: string[];
  marks: Record<MarkKind, DateTime | null>;
  next_mark: MarkKind | null;
  allowed_actions: MobileAction[];
}

// ---------------------------------------------------------------- справочник сотрудников (/ui/directory/staff)

export interface StaffProfile {
  staff_id: string;
  /** «Фамилия И. О.» */
  display_name: string;
  full_name: string;
  /** null — ФИО ещё не введено (запись создана из справочника): display_name = «Сотрудник ST-xxx». */
  first_name: string | null;
  last_name: string | null;
  /** true — ФИО не введено. */
  name_empty?: boolean;
  middle_name: string | null;
  phone: string | null;
  position: string | null;
  personnel_number: string | null;
  note: string | null;
  profile: Profile | null;
  profiles: Profile[];
  service_center_id: string | null;
  /** false — сотрудника нет в текущем справочнике (PUT reference): запись сохранена, в план не попадает. */
  in_reference?: boolean;
  /** То же, что in_reference (прежнее имя поля). */
  in_planner: boolean;
  technologies?: string[];
  /** Рюкзак АО из справочника (как пришёл в PUT reference); null — сотрудника нет в справочнике. */
  equipment_start_data: Equipment | null;
  equipment_capacity_data: Equipment | null;
  /** Эффективный рюкзак (с переопределением диспетчера), им грузится демо-день. */
  equipment_start: Equipment | null;
  equipment_capacity: Equipment | null;
  equipment_override: boolean;
}

/** Подсказка: p90 расхода АО за смену из последнего сценария «Рюкзак АО». */
export interface RecommendedBackpack {
  date: string;
  percentile: number;
  by_profile: Partial<Record<Profile, Partial<Equipment>>>;
}

export interface StaffDirectoryList {
  staff: StaffProfile[];
  count: number;
  /** false — справочники не загружены (нет PUT reference): список пуст. */
  reference_loaded?: boolean;
  not_in_reference?: number;
  persisted: boolean;
  recommended_backpack: RecommendedBackpack | null;
}

export type EquipmentScope = 'ALL' | Profile;

export interface EquipmentBulkInput {
  start?: Partial<Equipment>;
  capacity?: Partial<Equipment>;
  profile?: EquipmentScope;
}

export interface EquipmentBulkResult {
  updated?: number;
  reset?: number;
  profile: EquipmentScope;
  applies_from_next_replan: boolean;
  message: string;
  persisted: boolean;
}

export interface StaffProfileInput {
  /** null — оставить ФИО пустым (запись из справочника показывается как «Сотрудник ST-xxx»). */
  first_name: string | null;
  last_name: string | null;
  middle_name?: string | null;
  phone?: string | null;
  position?: string | null;
  personnel_number?: string | null;
  note?: string | null;
  profile?: Profile;
  service_center_id?: string;
  equipment_start?: Partial<Equipment>;
  equipment_capacity?: Partial<Equipment>;
}

export interface StaffProfileSaved extends StaffProfile {
  persisted: boolean;
  applies_from_next_replan?: boolean;
  message?: string;
  /** Только в ответе создания при AUTH_ENABLED=on: логин и стартовый пароль (показываются один раз). */
  login?: string;
  initial_password?: string;
}

// ---------------------------------------------------------------- авторизация (/ui/auth, /ui/directory/accounts)

/** Режим планировщика, переключаемый на лету (GET/PUT /ui/settings). */
export type RuntimeSettingKey = 'TRAFFIC_FACTORS' | 'JOINT_ORDERS' | 'PLANNER_DAY_BUDGET_SEC' | 'UI_PLAN_BUDGET_SEC';
export type RuntimeSettingValue = string | number;

export interface RuntimeSetting {
  key: RuntimeSettingKey | string;
  label: string;
  description: string;
  type: 'enum' | 'number';
  /** enum: 'on' | 'off'; number: секунды. */
  value: RuntimeSettingValue;
  default: RuntimeSettingValue;
  /** override — изменено из интерфейса, env — переменная окружения, default — по умолчанию. */
  source: 'default' | 'env' | 'override';
  risk: string | null;
  applies: string;
  options?: string[];
  min?: number;
  max?: number;
}

export interface RuntimeSettingsList {
  settings: RuntimeSetting[];
}

export interface RuntimeSettingsResult extends RuntimeSettingsList {
  changed: string[];
  /** true — построенный план устарел (сменились пробки или совместные наряды): нужна пересборка дня. */
  plan_outdated: boolean;
}

export type Role = 'DISPATCHER' | 'EMPLOYEE';

export interface AuthMe {
  login: string;
  role: Role;
  staff_id: string | null;
  display_name: string | null;
  must_change_password: boolean;
  /** false — AUTH_ENABLED=off: вход не нужен, кнопки «Выйти» нет. */
  auth_enabled: boolean;
}

export interface AuthSession {
  token: string;
  role: Role;
  staff_id: string | null;
  display_name: string | null;
  must_change_password: boolean;
  login: string;
  expires_in: number;
}

export interface DemoHint {
  enabled: boolean;
  dispatcher_login?: string;
  employee_sample_logins?: string[];
}

export interface Account {
  login: string;
  role: Role;
  staff_id: string | null;
  display_name: string | null;
  disabled: boolean;
  must_change_password: boolean;
  created_at?: string | null;
}

export interface AccountList {
  accounts: Account[];
  count: number;
}

export interface AccountReset extends Account {
  temporary_password: string;
  message: string;
}

/** GET /ui/plan/status — дешёвое состояние плана для автообновления (активный день = изменённый последним). */
/** POST /ui/day/reset — «Очистить день». */
export interface DayResetResult {
  status: 'reset';
  days: string[];
  with_reference: boolean;
  reference_loaded: boolean;
  /** Оставшиеся дни (при очистке одного дня, `?date=`). */
  remaining_days?: string[];
  active_date?: string | null;
}

export interface PlanStatus {
  loaded: boolean;
  date: string | null;
  plan_version: number | null;
  as_of: DateTime | null;
  orders_total: number | null;
  /** Поколение дня: меняется при каждом PUT days (тот же день загружен заново). */
  load_id: string | null;
}

export interface EmployeeStatus {
  loaded: boolean;
  date: string | null;
  plan_version: number | null;
  as_of: DateTime | null;
}

/* ---------------------------------------------------------------- /ui/orders (наряды из панели «Данные») */

export interface OrderCatalogWorkType {
  code: WorkTypeCode;
  name: string;
  profiles: Profile[];
  duration_min: number;
  pgp_relevant: boolean;
  /** Есть отдельный срок для повторного обращения (REPAIR). */
  repeat_allowed: boolean;
}

export interface OrderCatalogBuilding {
  building_id: string;
  address: string;
  lat: number;
  lon: number;
  building_type: BuildingType;
  overhead_line: boolean;
  technologies: Technology[];
  /** Типы работ, которые выполняются по этому адресу. */
  work_types: WorkTypeCode[];
}

export interface OrderCatalogStaff {
  staff_id: string;
  display_name: string | null;
  profiles: Profile[];
  technologies: Technology[];
  height_permit: boolean;
  on_shift: boolean;
}

export interface OrderCatalog {
  work_types: OrderCatalogWorkType[];
  buildings: OrderCatalogBuilding[];
  buildings_total: number;
  buildings_count: number;
  staff: OrderCatalogStaff[];
}

export interface CreateOrderInput {
  building_id: string;
  work_type: WorkTypeCode;
  /** null — ближайшее стандартное окно; иначе ЧЧ:ММ. */
  slot?: { start: string; end: string } | null;
  is_repeat?: boolean;
  preferred_staff_id?: string | null;
}

export interface CreateOrderResult {
  accepted: boolean;
  order_id: string;
  order: Order;
  work_type_name: string;
  outcome: 'ASSIGNED' | 'UNASSIGNED' | 'PGP';
  assignment: { staff_id: string; staff_name: string | null; eta: DateTime; planned_start: DateTime; planned_end: DateTime } | null;
  unassigned: UnassignedOrder | null;
  pgp: { group_id: string; access_node_id: string; comment: string | null; order_ids: string[]; message: string } | null;
  preferred: { staff_id: string; staff_name: string; honored: boolean; message: string } | null;
  plan_version: number;
  as_of: DateTime;
  warnings: string[];
  event: DemoStepItem;
}

export interface CancelOrderResult {
  accepted: boolean;
  cancelled: boolean;
  order_id: string;
  summary: string;
  freed: { staff_id: string; staff_name: string | null } | null;
  plan_version: number;
  as_of: DateTime;
  changed_staff_ids: string[];
  message: string | null;
  warnings: string[];
  event: DemoStepItem;
}

// ---------------------------------------------------------------- журнал истории (/ui/history, без БД — 503 JOURNAL_UNAVAILABLE)

export interface HistoryVersion {
  version: number;
  as_of: DateTime | null;
  based_on_event_id: string | null;
  computation_ms: number | null;
  visits_planned: number | null;
  unassigned: number | null;
  pgp_orders: number | null;
  cancelled: number | null;
  late_risk_active: number | null;
  travel_sec_total: number | null;
  utilization_cv: number | null;
}

export interface HistoryVersions {
  date: string;
  versions: HistoryVersion[];
}

export interface ForecastRow {
  work_type_code: string;
  work_type_name: string;
  normative_min: number;
  mean_min: number;
  median_min: number;
  p80_min: number;
  n: number;
  deviation_pct: number;
  p80_deviation_pct: number;
  building_split: boolean;
  source: string;
}

export interface ForecastModel {
  /** false — истории нет (или справочник не загружен): прогноз недоступен, причина в reason. */
  available: boolean;
  reason?: string;
  rows: ForecastRow[];
  n_total: number;
  experience?: { used: boolean; slope_per_year: number; correlation: number; t: number; t_critical: number };
  quality?: {
    available: boolean;
    n_train?: number;
    n_test?: number;
    test_days?: string[];
    mae_normative_min?: number;
    mae_forecast_min?: number;
    improvement_pct?: number;
    p80_coverage?: number;
    normative_coverage?: number;
  };
  note?: string;
}

export interface ForecastRiskOrder {
  order_id: string;
  staff_id: string;
  work_type_code: string;
  planned_start: DateTime;
  latest_start: DateTime;
  forecast_start: DateTime;
  accumulated_delay_min: number;
  lateness_min: number;
  reason: string;
}

export interface ForecastRisk {
  available: boolean;
  reason?: string;
  date: string;
  plan_version?: number;
  visits_checked?: number;
  extra_p80_total_min?: number;
  at_risk: number;
  orders: ForecastRiskOrder[];
  note?: string;
}
