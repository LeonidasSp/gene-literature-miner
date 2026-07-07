FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend
EXPOSE 7860
# Honor the platform-injected $PORT (Render/Fly/Cloud Run set this). Default 7860 so
# it also runs on Hugging Face Spaces (which serves containers on 7860) with no config.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
