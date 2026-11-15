import { useEffect, useRef, type RefObject } from 'react';
import type { Selection } from '../planModel';

/** Прокручивает только сам контейнер так, чтобы элемент оказался по центру, если он вне видимой области.
 *  Не scrollIntoView: тот двигает и всех предков (.app, вкладки dockview), а у скрытой во вкладке
 *  панели — сдвигает всю страницу (шапка уезжает вверх). */
function scrollWithin(box: HTMLElement, node: HTMLElement) {
  if (!box.clientHeight) return;
  const b = box.getBoundingClientRect();
  const n = node.getBoundingClientRect();
  if (n.top >= b.top && n.bottom <= b.bottom) return;
  const target = box.scrollTop + (n.top - b.top) - (box.clientHeight - n.height) / 2;
  box.scrollTop = Math.max(0, Math.min(target, box.scrollHeight - box.clientHeight));
}

/** Прокручивает контейнер к элементу [data-order=id], когда наряд выбран в другом месте экрана.
 *  Если элемента пока нет (фильтр сбрасывается), пробует после следующих рендеров. */
export function useScrollToSelected(box: RefObject<HTMLElement>, sel: Selection | null, own: Selection['source']) {
  const pending = useRef<string | null>(null);
  useEffect(() => {
    if (sel && sel.source !== own) pending.current = sel.id;
  }, [sel?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const id = pending.current;
    const el = box.current;
    if (!id || !el) return;
    const node = el.querySelector<HTMLElement>(`[data-order="${CSS.escape(id)}"]`);
    if (node) {
      scrollWithin(el, node);
      pending.current = null;
    }
  });
}
