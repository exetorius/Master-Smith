"""Minimal fal.ai queue client: submit, poll, fetch, upload to fal's CDN, download results.
Every successful call is appended to `calls` with its table price so the pipeline can settle the bill."""
import json
import os
import shutil
import tempfile
import time

import requests

from . import config
from .pricing import price

QUEUE = "https://queue.fal.run"
UPLOAD_INIT = "https://rest.alpha.fal.ai/storage/upload/initiate"


def _is_picture_model(model):
    """A fal picture endpoint (Nano Banana and friends, their /edit), the one thing MASTERSMITH_PAID_PICTURES lets
    through no-spend."""
    from .pricing import IMAGE_PRICES
    base = model[:-5] if model.endswith("/edit") else model
    return model.startswith("fal-ai/") and (model in IMAGE_PRICES or base in IMAGE_PRICES)


class FalError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Fal:
    def __init__(self, key=None, log=print):
        self.key = key or os.environ.get("FAL_KEY", "")
        if not self.key:
            raise FalError("FAL_KEY is not set (put it in .env)")
        self.log = log
        self.http = requests.Session()
        self.calls = []
        self.stage = ""
        self.uploads = {}                    # CDN url -> the local file it came from (the local models read files)

    def _retry(self, fn, attempts=4):
        """A dropped connection mid-poll is not a vendor failure: try again a few times before giving up."""
        for i in range(attempts):
            try:
                return fn()
            except (requests.ConnectionError, requests.Timeout) as exc:
                if i == attempts - 1:
                    raise FalError("fal network error: %s" % str(exc)[:200])
                time.sleep(2 * (i + 1))

    def _h(self):
        return {"Authorization": "Key " + self.key, "Content-Type": "application/json"}

    def run(self, model, payload, timeout=1500, poll=3.0):
        usd = price(model, payload)          # refuses unpriced endpoints before any money moves
        if model.startswith("local/"):
            return self._run_local(model, payload, usd)
        if config.NO_SPEND and not (config.PAID_PICTURES and _is_picture_model(model)):
            raise FalError("MASTERSMITH_NO_SPEND=1: the paid call %s was refused (nothing was spent)" % model, 402)
        t0 = time.time()
        r = self._retry(lambda: self.http.post("%s/%s" % (QUEUE, model), headers=self._h(),
                                               data=json.dumps(payload), timeout=60))
        if r.status_code != 200:
            raise FalError("fal submit %s: HTTP %d %s" % (model, r.status_code, r.text[:300]), r.status_code)
        sub = r.json()
        status_url, response_url = sub.get("status_url"), sub.get("response_url")
        if not status_url or not response_url:
            raise FalError("fal submit %s: malformed response" % model)
        while True:
            if time.time() - t0 > timeout:
                raise FalError("fal %s timed out after %ds" % (model, timeout))
            time.sleep(poll)
            s = self._retry(lambda: self.http.get(status_url, headers=self._h(), timeout=30))
            if s.status_code not in (200, 202):
                raise FalError("fal status %s: HTTP %d" % (model, s.status_code), s.status_code)
            st = s.json().get("status")
            if st == "COMPLETED":
                break
            if st not in ("IN_QUEUE", "IN_PROGRESS"):
                raise FalError("fal %s: status %s" % (model, st))
        res = self._retry(lambda: self.http.get(response_url, headers=self._h(), timeout=60))
        if res.status_code != 200:
            raise FalError("fal result %s: HTTP %d %s" % (model, res.status_code, res.text[:300]), res.status_code)
        out = res.json()
        if isinstance(out, dict) and out.get("detail"):
            raise FalError("fal %s rejected the request: %s" % (model, str(out["detail"])[:300]), 422)
        secs = round(time.time() - t0, 1)
        self.calls.append({"model": model, "seconds": secs, "usd": usd, "stage": self.stage})
        self.log("  fal %s: %.0fs, $%.3f" % (model, secs, usd))
        return out

    def _run_local(self, model, payload, usd):
        """The free tier (mastersmith/local.py): the same call shape, run on this PC, answered like fal answers."""
        from . import local
        if model != "local/trellis2":
            raise FalError("no local runner for %s" % model)
        url = payload.get("image_url") or (payload.get("image_urls") or [None])[0]
        image = self.uploads.get(url) or local.file_path(url)
        work = tempfile.mkdtemp(prefix="ms_local_")
        if not image:
            image = self.download(url, os.path.join(work, "input.png"))
        glb = os.path.join(work, "seed.glb")
        try:
            secs = local.trellis(image, glb, log=self.log)
        except local.LocalError as exc:
            raise FalError("local %s: %s" % (model, exc))
        self.calls.append({"model": model, "seconds": secs, "usd": usd, "stage": self.stage})
        self.log("  local %s: %.0fs, $0" % (model, secs))
        return local.seed_output(glb)

    def upload(self, path, mime=None):
        if config.NO_SPEND and not config.PAID_PICTURES:   # the local models read files: no fal storage needed
            url = "file:///" + os.path.abspath(path).replace("\\", "/")
            self.uploads[url] = os.path.abspath(path)
            return url
        low = str(path).lower()
        mime = mime or ("image/png" if low.endswith(".png") else "image/jpeg" if low.endswith((".jpg", ".jpeg"))
                        else "image/webp" if low.endswith(".webp") else "model/gltf-binary")
        # a dropped TLS handshake here (SSLEOFError, the Havoc re-finish of 2026-09-24) is a ConnectionError: retried
        init = self._retry(lambda: self.http.post(UPLOAD_INIT + "?storage_type=fal-cdn-v3", headers=self._h(),
                                                  data=json.dumps({"content_type": mime, "file_name": os.path.basename(path)}), timeout=120))
        if init.status_code != 200:
            raise FalError("fal upload initiate: HTTP %d %s" % (init.status_code, init.text[:200]))
        body = init.json()
        with open(path, "rb") as f:
            data = f.read()
        put = self._retry(lambda: self.http.put(body["upload_url"], data=data, headers={"Content-Type": mime}, timeout=300))
        if put.status_code not in (200, 201, 204):
            raise FalError("fal upload PUT: HTTP %d" % put.status_code)
        self.uploads[body["file_url"]] = os.path.abspath(path)
        return body["file_url"]

    def download(self, url, path, timeout=600):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if url.startswith("file://"):                  # a local model's result: moved out of its scratch folder
            from .local import file_path
            src = file_path(url)
            scratch = os.path.dirname(src)
            if os.path.basename(scratch).startswith("ms_local_"):
                shutil.move(src, path)
                shutil.rmtree(scratch, ignore_errors=True)
            else:
                shutil.copyfile(src, path)
            return path
        def go():
            with self.http.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
        self._retry(go)
        return path

    def spent(self):
        return round(sum(c["usd"] for c in self.calls), 4)


def first_url(obj, suffixes):
    """Depth-first search of a fal response for a file url ending in one of `suffixes`."""
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str) and u.split("?")[0].lower().endswith(tuple(suffixes)):
            return u
        for v in obj.values():
            r = first_url(v, suffixes)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = first_url(v, suffixes)
            if r:
                return r
    return None


def image_url(out):
    imgs = (out or {}).get("images") or ([out["image"]] if (out or {}).get("image") else [])
    return imgs[0]["url"] if imgs else first_url(out, (".png", ".jpg", ".jpeg", ".webp"))
