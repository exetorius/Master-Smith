"""Pictures: from a prompt, or from a prompt plus reference pictures (the edit case). Two providers behind one call:
fal ids (fal-ai/nano-banana-2, ...; with references the /edit endpoint) and local/ ids (FLUX.2 klein on this PC,
free: mastersmith/local.py). Every successful call is appended to `calls` with its cost."""
import os
import time

from PIL import Image

from . import config
from .fal import Fal, FalError, image_url
from .pricing import image_price

REFUSAL_WORDS = ("safety", "moderat", "policy", "block", "refus", "prohibited", "violat", "not allowed", "flagged", "could not generate")


class ImageError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ImageRefused(ImageError):
    """The provider's content checker declined the prompt or the reference picture."""


FLUX_SIZES = {"4:3": "landscape_4_3", "3:4": "portrait_4_3", "16:9": "landscape_16_9", "9:16": "portrait_16_9", "1:1": "square_hd"}


def fal_endpoint(model, has_references):
    """A fal picture id with references runs its /edit sibling."""
    if has_references and not model.endswith("/edit"):
        return model + "/edit"
    return model


def fal_payload(model, prompt, reference_urls=(), aspect_ratio="4:3", resolution="1K"):
    """Each fal picture family has its own request shape."""
    if "flux" in model:
        payload = {"prompt": prompt, "image_size": FLUX_SIZES.get(aspect_ratio, "landscape_4_3"), "num_images": 1, "output_format": "png"}
    else:   # nano-banana, nano-banana-2, nano-banana-pro
        payload = {"prompt": prompt, "aspect_ratio": aspect_ratio, "resolution": resolution, "num_images": 1, "output_format": "png"}
    if reference_urls:
        payload["image_urls"] = list(reference_urls)[:4]
    return payload


class Images:
    def __init__(self, key=None, log=print):
        self.log = log
        self.calls = []
        self.stage = ""                  # the pipeline names the stage; every call carries it for the cost breakdown
        self._fal = None                 # its own fal client, so its calls are counted here and nowhere else

    def fal(self):
        if self._fal is None:
            self._fal = Fal(log=lambda m: None)
        return self._fal

    def _generate_fal(self, prompt, path, model, refs, aspect_ratio, resolution):
        endpoint = fal_endpoint(model, bool(refs))
        image_price(endpoint)
        fal = self.fal()
        urls = [r if str(r).startswith(("http://", "https://")) else fal.upload(r) for r in refs[:4]]
        t0 = time.time()
        try:
            out = fal.run(endpoint, fal_payload(endpoint, prompt, urls, aspect_ratio, resolution or config.IMAGE_RESOLUTION), timeout=600)
        except FalError as exc:
            text = str(exc)
            if any(w in text.lower() for w in REFUSAL_WORDS + ("nsfw", "content")):
                raise ImageRefused("%s refused the request: %s" % (endpoint, text[:300]), exc.status)
            raise ImageError("fal %s: %s" % (endpoint, text[:300]), exc.status)
        url = image_url(out)
        if not url:
            raise ImageRefused("%s returned no picture (content checker?)" % endpoint, 422)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fal.download(url, path)
        im = Image.open(path)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.save(path)
        usd = fal.calls[-1]["usd"] if fal.calls else image_price(endpoint)
        secs = round(time.time() - t0, 1)
        self.calls.append({"model": endpoint, "seconds": secs, "usd": usd, "references": len(refs), "stage": self.stage})
        self.log("  image %s: %.0fs, $%.3f%s" % (
            endpoint, secs, usd, (" (%d reference%s)" % (len(refs), "" if len(refs) == 1 else "s")) if refs else ""))
        return path

    def _generate_local(self, prompt, path, model, refs, aspect_ratio):
        from . import local
        image_price(model)
        local_refs = [self.fal().uploads.get(r, r) if self._fal else r for r in refs[:4]]
        try:
            secs = local.picture(prompt, path, local_refs, aspect_ratio, log=self.log)
        except local.LocalError as exc:
            raise ImageError("local %s: %s" % (model, exc))
        self.calls.append({"model": model, "seconds": secs, "usd": 0.0, "references": len(refs), "stage": self.stage})
        self.log("  image %s: %.0fs, $0%s" % (
            model, secs, (" (%d reference%s)" % (len(refs), "" if len(refs) == 1 else "s")) if refs else ""))
        return path

    def generate(self, prompt, path, model=None, references=(), aspect_ratio="4:3", resolution=None, timeout=300):
        """One PNG at `path`. `references`: local paths or URLs the picture must follow (an edit)."""
        model = model or config.CONCEPT_MODEL
        if config.NO_SPEND and not model.startswith("local/") and not (config.PAID_PICTURES and model.startswith("fal-ai/")):
            model = config.LOCAL_PICTURE_MODEL           # FLUX.2 klein on this PC
        refs = [r for r in (references or []) if r]
        if model.startswith("fal-ai/"):
            return self._generate_fal(prompt, path, model, refs, aspect_ratio, resolution)
        if model.startswith("local/"):
            return self._generate_local(prompt, path, model, refs, aspect_ratio)
        raise ImageError("unknown picture model %s: use a fal-ai/ id or a local/ id" % model)

    def spent(self):
        return round(sum(c["usd"] for c in self.calls), 6)
