FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TZ=Asia/Bahrain PORT=8000 RADAR_DAILY_AT=06:30
EXPOSE 8000
VOLUME ["/app/data", "/app/docs"]
# Hosts the dashboard and refreshes it every day at RADAR_DAILY_AT
CMD ["python", "radar.py", "serve"]
