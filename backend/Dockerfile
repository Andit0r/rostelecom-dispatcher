# Сборка из корня репозитория: docker build -f backend/Dockerfile .
# (контекст — корень, так что .dockerignore лежит в корне)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLANNER=pyvrp \
    PLANNER_DAY_BUDGET_SEC=60

WORKDIR /srv

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app

EXPOSE 8080

# Таймаут keep-alive не меньше 330 с: расчёт плана может занимать до 300 с.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "330"]
