FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir --upgrade redis jsonpickle requests flask minio
COPY rest-server.py .
EXPOSE 5000
CMD ["python", "rest-server.py"]
