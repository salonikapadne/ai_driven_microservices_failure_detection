FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY seed/app.py /app/seed/app.py
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh
ENV PORT=3010
EXPOSE 3010
ENTRYPOINT ["/app/entrypoint.sh"]
