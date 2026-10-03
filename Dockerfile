FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir httpx==0.28.1
COPY . .
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["python", "-m", "app.main"]
