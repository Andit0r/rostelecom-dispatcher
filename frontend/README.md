# Интерфейс диспетчера

1. Backend (Python 3.12): `cd backend && py -3.12 -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
2. Запуск API: `.venv\Scripts\uvicorn app.main:app --port 8080` (бюджет построения плана при «Создать день»/«Пересобрать» — `UI_PLAN_BUDGET_SEC`, по умолчанию 15 с).
3. Фронт: `cd frontend && npm install && npm run dev`, открыть http://localhost:5173 (запросы `/api` и `/ui` проксируются на :8080).
4. В интерфейсе, если дня нет, нажмите «Создать день» и вносите наряды вкладкой «Наряды» → «+ Новый наряд» (или загрузите день через API / `tools/sim/run_sim.py`).
5. Сборка и проверка типов: `npm run build`, `npx tsc --noEmit`. Токены темы — `src/theme/tokens.css`.
