import os
import sys
import io
import json
import base64
import hashlib
import platform

import redis
from flask import Flask, request, Response, send_file
from minio import Minio

app = Flask(__name__)

redisHost = os.getenv("REDIS_HOST") or "localhost"
redisPort = int(os.getenv("REDIS_PORT") or 6379)
minioHost = os.getenv("MINIO_HOST") or "localhost:9000"
minioUser = os.getenv("MINIO_ACCESS_KEY") or "rootuser"
minioPassword = os.getenv("MINIO_SECRET_KEY") or "rootpass123"

QUEUE_BUCKET = "queue"
OUTPUT_BUCKET = "output"
WORKER_KEY = "toWorker"
LOG_KEY = "logging"

infoKey = "{}.rest.info".format(platform.node())
debugKey = "{}.rest.debug".format(platform.node())

redisClient = redis.StrictRedis(host=redisHost, port=redisPort, db=0)
minioClient = Minio(
    minioHost,
    access_key=minioUser,
    secret_key=minioPassword,
    secure=False,
)


def log_debug(message, key=debugKey):
    print("DEBUG:", message, file=sys.stdout)
    try:
        redisClient.lpush(LOG_KEY, f"{key}:{message}")
    except Exception as exc:
        print(f"log_debug failed: {exc}", file=sys.stderr)


def log_info(message, key=infoKey):
    print("INFO:", message, file=sys.stdout)
    try:
        redisClient.lpush(LOG_KEY, f"{key}:{message}")
    except Exception as exc:
        print(f"log_info failed: {exc}", file=sys.stderr)
    sys.stdout.flush()


def ensure_buckets():
    for bucket in (QUEUE_BUCKET, OUTPUT_BUCKET):
        if not minioClient.bucket_exists(bucket):
            minioClient.make_bucket(bucket)
            log_info(f"Created bucket {bucket}")


def stem_name(track):
    # Accept either "vocals" or "vocals.mp3".
    if track.endswith(".mp3"):
        return track[:-4]
    return track


@app.route("/", methods=["GET"])
def hello():
    return "<h1> Music Separation Server</h1><p> Use a valid endpoint </p>"


@app.route("/apiv1/separate", methods=["POST"])
def separate():
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        return Response(
            json.dumps({"error": "Invalid JSON"}),
            status=400,
            mimetype="application/json",
        )
    if not isinstance(body, dict):
        return Response(
            json.dumps({"error": "Invalid JSON"}),
            status=400,
            mimetype="application/json",
        )

    mp3_b64 = body.get("mp3")
    if not mp3_b64:
        return Response(
            json.dumps({"error": "Missing mp3 data"}),
            status=400,
            mimetype="application/json",
        )
    try:
        mp3_bytes = base64.b64decode(mp3_b64)
    except Exception:
        return Response(
            json.dumps({"error": "Invalid base64 mp3"}),
            status=400,
            mimetype="application/json",
        )

    songhash = hashlib.sha256(mp3_bytes).hexdigest()
    task = {
        "songhash": songhash,
        "callback": body.get("callback"),
        "model": body.get("model"),
    }
    try:
        ensure_buckets()
        minioClient.put_object(
            QUEUE_BUCKET,
            f"{songhash}.mp3",
            io.BytesIO(mp3_bytes),
            length=len(mp3_bytes),
            content_type="audio/mpeg",
        )
        redisClient.lpush(WORKER_KEY, json.dumps(task))
        log_info(f"Enqueued song {songhash}")
        payload = {"hash": songhash, "reason": "Song enqueued for separation"}
        return Response(json.dumps(payload), status=200, mimetype="application/json")
    except Exception as exc:
        log_info(f"Failed to enqueue song: {exc}")
        return Response(
            json.dumps({"error": str(exc)}),
            status=500,
            mimetype="application/json",
        )


@app.route("/apiv1/queue", methods=["GET"])
def get_queue():
    try:
        raw_items = redisClient.lrange(WORKER_KEY, 0, -1)
        hashes = []
        for raw in raw_items:
            text = raw.decode("utf-8")
            try:
                task = json.loads(text)
                hashes.append(task.get("songhash", text))
            except Exception:
                hashes.append(text)
        return Response(
            json.dumps({"queue": hashes}),
            status=200,
            mimetype="application/json",
        )
    except Exception as exc:
        return Response(
            json.dumps({"error": str(exc)}),
            status=500,
            mimetype="application/json",
        )


@app.route("/apiv1/track/<songhash>/<track>", methods=["GET"])
def get_track(songhash, track):
    object_name = f"{songhash}-{stem_name(track)}.mp3"
    try:
        obj = minioClient.get_object(OUTPUT_BUCKET, object_name)
        data = obj.read()
        obj.close()
        obj.release_conn()
        return send_file(
            io.BytesIO(data),
            mimetype="audio/mpeg",
            download_name=object_name,
        )
    except Exception:
        log_debug(f"Track not found: {object_name}")
        return Response(
            json.dumps({"error": "Track not found"}),
            status=404,
            mimetype="application/json",
        )


@app.route("/apiv1/remove/<songhash>/<track>", methods=["GET", "DELETE"])
def remove_track(songhash, track):
    object_name = f"{songhash}-{stem_name(track)}.mp3"
    try:
        minioClient.remove_object(OUTPUT_BUCKET, object_name)
        log_info(f"Removed {object_name}")
        return Response(
            json.dumps({"removed": object_name}),
            status=200,
            mimetype="application/json",
        )
    except Exception as exc:
        log_info(f"Failed to remove {object_name}: {exc}")
        return Response(
            json.dumps({"error": str(exc)}),
            status=404,
            mimetype="application/json",
        )


if __name__ == "__main__":
    try:
        ensure_buckets()
    except Exception as exc:
        print(f"Bucket init skipped: {exc}", file=sys.stderr)
    app.run(host="0.0.0.0", port=5000, threaded=True)
