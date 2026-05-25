FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV WEB_MAX_FETCH=15
# Hosts like Render/Railway inject $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded at runtime.
CMD uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000}
