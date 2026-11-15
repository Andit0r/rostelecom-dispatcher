// Геолокация для мягкой проверки «Прибыл»: никогда не бросает и не висит дольше таймаута.

/** Расстояние по большому кругу (haversine), метры. */
export function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371008.8;
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

export interface Fix {
  lat: number;
  lon: number;
  accuracy: number;
}

/** Текущее место или null (нет API, отказ, ошибка, таймаут). */
export function currentFix(timeoutMs = 6000): Promise<Fix | null> {
  if (typeof navigator === 'undefined' || !('geolocation' in navigator)) return Promise.resolve(null);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (v: Fix | null) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(guard);
      resolve(v);
    };
    // Страховка: некоторые браузеры не вызывают ни один колбэк, пока висит системный запрос разрешения.
    const guard = window.setTimeout(() => finish(null), timeoutMs + 1000);
    try {
      navigator.geolocation.getCurrentPosition(
        (p) => finish({ lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy }),
        () => finish(null),
        { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 60_000 },
      );
    } catch {
      finish(null);
    }
  });
}

export function distanceText(m: number): string {
  if (m < 1000) return `${Math.round(m / 10) * 10} м`;
  return `${(m / 1000).toFixed(1).replace('.', ',')} км`;
}
