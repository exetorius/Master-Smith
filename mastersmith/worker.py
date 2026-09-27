"""The build worker: one job at a time (Blender is CPU-bound), pulled from the store's queue.
Runs as a thread inside the service, or standalone: python -m mastersmith worker"""
import json
import threading
import time
import traceback
import uuid

from . import pipeline
from .spec import Spec
from .store import Store
from .wallet import Wallet


def new_job_id():
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def run_one(store, wallet, row):
    job_id = row["id"]

    def log(line):
        store.append_log(job_id, line)

    try:
        if row["kind"] == "refinish":
            result = pipeline.refinish(row["source_job"], row["user"], wallet, row["spec"], log=log, job_id=job_id)
        elif row["kind"] == "rework":
            src = json.loads(row["source_job"] or "{}")      # {"seed": path, "ref": path|None, "mode": "refinish"}
            if (src.get("mode") or "refinish") != "refinish":
                # a retexture queued before the post-op repairs were removed (2026-09-26): a colour change is a new build
                store.finish(job_id, "failed", error="the %s mode no longer exists; build the asset again" % src["mode"])
                return
            result = pipeline.rework(src["seed"], Spec.from_dict(row["spec"]), row["user"], wallet, ref_view=src.get("ref"),
                                     log=log, job_id=job_id)
        else:
            result = pipeline.build(Spec.from_dict(row["spec"]), row["user"], wallet, log=log, job_id=job_id)
        store.finish(job_id, result["status"], result=result, error=result.get("error"))
    except Exception:  # noqa: BLE001 - the worker must survive any single job
        store.finish(job_id, "failed", error=traceback.format_exc()[-1500:])


class Worker(threading.Thread):
    def __init__(self, store=None, wallet=None, poll=2.0):
        super().__init__(daemon=True, name="mastersmith-worker")
        self.store = store or Store()
        self.wallet = wallet or Wallet()
        self.poll = poll
        self.stop = threading.Event()
        self.current = None

    def run(self):
        self.store.requeue_stale()
        while not self.stop.is_set():
            row = self.store.claim_next()
            if not row:
                self.stop.wait(self.poll)
                continue
            self.current = row["id"]
            run_one(self.store, self.wallet, row)
            self.current = None


if __name__ == "__main__":
    w = Worker()
    w.run()
