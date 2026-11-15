import { useEffect, useRef, useState } from 'react';
import { PANELS, type PanelId } from '../dock/layout';

interface Props {
  open: ReadonlySet<PanelId>;
  onToggle: (id: PanelId, show: boolean) => void;
  onReset: () => void;
}

/** Меню «Окна»: чекбоксы показа панелей и сброс раскладки. */
export function WindowsMenu({ open, onToggle, onReset }: Props) {
  const [shown, setShown] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!shown) return;
    const onDown = (e: PointerEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setShown(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShown(false);
    };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [shown]);

  return (
    <div className="winmenu" ref={boxRef}>
      <button
        type="button"
        className="btn btn--s btn--ghost btn--neutral winmenu__trigger"
        aria-haspopup="true"
        aria-expanded={shown}
        aria-label="Окна"
        title="Окна: показать или скрыть панели"
        onClick={() => setShown((v) => !v)}
      >
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <rect x="2" y="2.5" width="12" height="11" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M2 6h12M7.5 6v7.5" stroke="currentColor" strokeWidth="1.4" />
        </svg>
        <span className="winmenu__label">Окна</span>
      </button>
      {shown && (
        <div className="winmenu__pop" role="group" aria-label="Панели экрана">
          <p className="winmenu__head">Показывать панели</p>
          {PANELS.map((p) => (
            <label key={p.id} className="winmenu__item">
              <input type="checkbox" checked={open.has(p.id)} onChange={(e) => onToggle(p.id, e.target.checked)} />
              <span className="winmenu__box" aria-hidden />
              {p.title}
            </label>
          ))}
          <div className="winmenu__sep" />
          <button
            type="button"
            className="winmenu__reset"
            onClick={() => {
              onReset();
              setShown(false);
            }}
          >
            Сбросить раскладку
          </button>
          <p className="winmenu__hint">
            Вкладки перетаскиваются к краям и в другие группы, границы тянутся мышью. Shift при перетаскивании вкладки — плавающее окно.
          </p>
        </div>
      )}
    </div>
  );
}
