/*
 * Временные компоненты с API, повторяющим @atomaro/ui-kit (пакет из закрытого реестра).
 * Пропсы взяты из документации Storybook Atomaro (MCP):
 *  Button: size s|m|l|xl, variant primary|secondary|outline|ghost, colorScheme neutral|accent,
 *          label, iconPrefix, iconSuffix, disabled, href
 *  Badge:  label, colorScheme error|info|warning|success|neutral|status-01..06, size s|2xs,
 *          variant primary|secondary|outline|ghost, dot
 *  TabsGroup/TabsItem: value, onChange, size s|m, border; TabsItem — label, index
 *  SegmentedControl/Segment: value, onChange, size s|m|l, variant primary|secondary; Segment — label, index
 *  Switch: checked, onChange(value), label, size s|xs, labelPosition right|left, disabled
 * Замена на настоящие компоненты — заменой импорта; лишних пропсов здесь нет.
 */
import type { ReactNode } from 'react';

type Cls = (string | false | null | undefined)[];
const cx = (...c: Cls) => c.filter(Boolean).join(' ');

export interface ButtonProps {
  label?: ReactNode;
  size?: 's' | 'm' | 'l' | 'xl';
  variant?: 'primary' | 'secondary' | 'outline' | 'ghost';
  colorScheme?: 'neutral' | 'accent';
  iconPrefix?: ReactNode;
  iconSuffix?: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  title?: string;
}

export function Button({
  label,
  size = 'm',
  variant = 'primary',
  colorScheme = 'accent',
  iconPrefix,
  iconSuffix,
  disabled,
  onClick,
  title,
}: ButtonProps) {
  return (
    <button
      type="button"
      className={cx('btn', `btn--${size}`, `btn--${variant}`, `btn--${colorScheme}`)}
      disabled={disabled}
      onClick={onClick}
      title={title}
    >
      {iconPrefix}
      {label}
      {iconSuffix}
    </button>
  );
}

export type BadgeScheme =
  | 'error'
  | 'info'
  | 'warning'
  | 'success'
  | 'neutral'
  | 'status-01'
  | 'status-02'
  | 'status-03'
  | 'status-04'
  | 'status-05'
  | 'status-06';

export interface BadgeProps {
  label?: ReactNode;
  colorScheme?: BadgeScheme;
  size?: 's' | '2xs';
  variant?: 'primary' | 'secondary' | 'outline' | 'ghost';
  dot?: boolean;
}

export function Badge({ label, colorScheme = 'info', size = 's', variant = 'primary', dot }: BadgeProps) {
  return (
    <span className={cx('badge', `badge--${colorScheme}`, `badge--${size}`, `badge--${variant}`)}>
      {dot && <span className="badge__dot" />}
      {label}
    </span>
  );
}

export interface TabsItemProps {
  index: string;
  label: ReactNode;
}

export function TabsGroup({
  value,
  onChange,
  items,
  size = 'm',
  border = true,
}: {
  value: string;
  onChange?: (index: string) => void;
  items: TabsItemProps[];
  size?: 's' | 'm';
  border?: boolean;
}) {
  return (
    <div className={cx('tabs', `tabs--${size}`, border && 'tabs--border')} role="tablist">
      {items.map((it) => (
        <button
          key={it.index}
          type="button"
          role="tab"
          aria-selected={it.index === value}
          className={cx('tabs__item', it.index === value && 'tabs__item--active')}
          onClick={() => onChange?.(it.index)}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  size = 's',
  labelPosition = 'right',
  disabled = false,
}: {
  checked?: boolean;
  onChange?: (value: boolean) => void;
  label?: ReactNode;
  size?: 's' | 'xs';
  labelPosition?: 'right' | 'left';
  disabled?: boolean;
}) {
  return (
    <label className={cx('switch', `switch--${size}`, `switch--label-${labelPosition}`, disabled && 'switch--disabled')}>
      <input
        type="checkbox"
        role="switch"
        className="switch__input"
        checked={!!checked}
        disabled={disabled}
        onChange={(e) => onChange?.(e.target.checked)}
      />
      <span className="switch__track" aria-hidden />
      {label != null && <span className="switch__label">{label}</span>}
    </label>
  );
}

export function SegmentedControl({
  value,
  onChange,
  items,
  size = 'm',
  variant = 'primary',
  disabledAll,
}: {
  value: string;
  onChange?: (index: string) => void;
  items: { index: string; label: ReactNode }[];
  size?: 's' | 'm' | 'l';
  variant?: 'primary' | 'secondary';
  disabledAll?: boolean;
}) {
  return (
    <div className={cx('seg', `seg--${size}`, `seg--${variant}`)} role="radiogroup">
      {items.map((it) => (
        <button
          key={it.index}
          type="button"
          role="radio"
          aria-checked={it.index === value}
          disabled={disabledAll}
          className={cx('seg__item', it.index === value && 'seg__item--active')}
          onClick={() => onChange?.(it.index)}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}
