import os
import sys
import json
import time
import shutil
import tempfile
import platform
import subprocess

import redis
import requests
from minio import Minio

redisHost = os.getenv("REDIS_HOST") or "localhost"
redisPort = int(os.getenv("REDIS_PORT") or 6379)
minioHost = os.getenv("MINIO_HOST") or "localhost:9000"
minioUser = os.getenv("MINIO_ACCESS_KEY") or "rootuser"
minioPassword = os.getenv("MINIO_SECRET_KEY") or "rootpass123"
# Set only inside the cluster so sample callbacks aimed at localhost reach REST.
callbackHost = os.getenv("CALLBACK_HOST")

QUEUE_BUCKET = "queue"
OUTPUT_BUCKET = "output"
WORKER_KEY = "toWorker"
LOG_KEY = "logging"
STEMS = ("bass", "drums", "other", "vocals")

infoKey = "{}.worker.info".format(platform.node())
debugKey = "{}.worker.debug".format(platform.node())

redisClient = redis.StrictRedis(host=redisHost, port=redisPort, db=0)
minioClient = Minio(
    minioHost,
    access_key=minioUser,
    secret_key=minioPassword,
    secure=False,
)


def log_debug(message, key=debugKey):
    print("DEBUG:", message, file=sys.stdout, flush=True)
    try:
        redisClient.lpush(LOG_KEY, f"{key}:{message}")
    except Exception as exc:
        print(f"log_debug failed: {exc}", file=sys.stderr, flush=True)


def log_info(message, key=infoKey):
    print("INFO:", message, file=sys.stdout, flush=True)
    try:
        redisClient.lpush(LOG_KEY, f"{key}:{message}")
    except Exception as exc:
        print(f"log_info failed: {exc}", file=sys.stderr, flush=True)


def ensure_buckets():
    for bucket in (QUEUE_BUCKET, OUTPUT_BUCKET):
        if not minioClient.bucket_exists(bucket):
            minioClient.make_bucket(bucket)
            log_info(f"Created bucket {bucket}")


def find_stems(output_dir):
    found = {}
    for root, _dirs, files in os.walk(output_dir):
        for stem in STEMS:
            filename = f"{stem}.mp3"
            if filename in files and stem not in found:
                found[stem] = os.path.join(root, filename)
    return found


def send_callback(callback):
    if not isinstance(callback, dict):
        log_debug("No callback requested")
        return
    url = callback.get("url")
    data = callback.get("data", {})
    if not url:
        log_debug("Callback missing url")
        return
    if callbackHost:
        url = url.replace("localhost:5000", callbackHost).replace("127.0.0.1:5000", callbackHost)
    try:
        requests.post(url, json=data, timeout=5)
        log_info(f"Sent callback to {url}")
    except Exception as exc:
        # The lab allows the webhook to fail.
        log_info(f"Callback failed: {exc}")


def separate_song(input_path, output_dir, model):
    cmd = [sys.executable, "-m", "demucs.separate", "--mp3", "-o", output_dir]
    if model:
        cmd.extend(["-n", model])
    cmd.append(input_path)
    log_info(f"Running demucs: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.stdout:
        log_debug(result.stdout[-2000:])
    if result.returncode != 0:
        log_info(f"Demucs failed ({result.returncode}): {result.stderr[-2000:]}")
        return False
    return True


def process_task(task):
    songhash = task.get("songhash")
    if not songhash:
        log_info(f"Task missing songhash: {task}")
        return
    callback = task.get("callback")
    model = task.get("model")
    work_dir = tempfile.mkdtemp(prefix="demucs-")
    input_path = os.path.join(work_dir, f"{songhash}.mp3")
    output_dir = os.path.join(work_dir, "output")
    log_info(f"Processing song {songhash}")
    try:
        ensure_buckets()
        minioClient.fget_object(QUEUE_BUCKET, f"{songhash}.mp3", input_path)
        log_info(f"Downloaded {songhash}.mp3 from {QUEUE_BUCKET}")
        if not separate_song(input_path, output_dir, model):
            return
        stems = find_stems(output_dir)
        if len(stems) != len(STEMS):
            log_info(f"Expected tracks {STEMS}, found {sorted(stems)}")
            return
        for stem, path in stems.items():
            object_name = f"{songhash}-{stem}.mp3"
            minioClient.fput_object(
                OUTPUT_BUCKET,
                object_name,
                path,
                content_type="audio/mpeg",
            )
            log_info(f"Uploaded {object_name} to {OUTPUT_BUCKET}")
        send_callback(callback)
        log_info(f"Completed processing {songhash}")
    except Exception as exc:
        log_info(f"Error processing {songhash}: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    log_info("Worker started, waiting for jobs on toWorker")
    while True:
        try:
            work = redisClient.blpop(WORKER_KEY, timeout=0)
            if not work:
                continue
            payload = work[1].decode("utf-8")
            log_debug(f"Received task {payload}")
            process_task(json.loads(payload))
        except Exception as exc:
            log_info(f"Worker loop error: {exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
