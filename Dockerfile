FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY config/report-schema.md config/preferences.md config/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "tradingagent"]
CMD ["--stage", "all"]
