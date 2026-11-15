/*
 * «Создать день»: небольшая форма с датой (по умолчанию сегодня). Используется в пустом состоянии
 * и во вкладке «Режимы». Создаёт пустой день и пустой план (POST /ui/day/create); наряды вносятся
 * формой «Новый наряд» во вкладке «Наряды».
 */
import { useState } from 'react';
import { Button } from './ui';

/** Сегодняшняя дата в YYYY-MM-DD по часам браузера. */
export function todayIso(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

interface Props {
  /** Создаёт день на дату; true — успешно. */
  onCreate: (date: string) => Promise<boolean>;
  busy?: boolean;
  onCancel?: () => void;
  onDone?: () => void;
}

export function CreateDayForm({ onCreate, busy, onCancel, onDone }: Props) {
  const [date, setDate] = useState(todayIso);
  const [fail, setFail] = useState<string | null>(null);
  const valid = /^\d{4}-\d{2}-\d{2}$/.test(date);

  const submit = () => {
    if (!valid || busy) return;
    setFail(null);
    void onCreate(date).then((ok) => {
      if (ok) onDone?.();
      else setFail('Не удалось создать день — причина показана вверху страницы.');
    });
  };

  return (
    <form
      className="create-day"
      aria-label="Создать день"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label className="create-day__field">
        <span>Дата дня</span>
        <input className="input num" type="date" value={date} onChange={(e) => setDate(e.target.value)} required />
      </label>
      <div className="create-day__btns">
        <Button size="s" label={busy ? 'Создаём…' : 'Создать'} disabled={!valid || !!busy} onClick={submit} />
        {onCancel && <Button size="s" variant="ghost" colorScheme="neutral" label="Отмена" onClick={onCancel} />}
      </div>
      {fail && (
        <p className="dform__error" role="alert">
          {fail}
        </p>
      )}
    </form>
  );
}
