# Extend the published Demucs image so the heavy layers stay cached.
FROM xserrat/facebook-demucs:latest
USER root
RUN python3 -m pip install --no-cache-dir --upgrade minio redis requests jsonpickle
WORKDIR /app
COPY worker.py /app/worker.py
# The base image entrypoint launches demucs.separate; run the worker instead.
ENTRYPOINT []
CMD ["python3", "/app/worker.py"]
