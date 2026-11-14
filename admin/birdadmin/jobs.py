"""Upload jobs: receive a file, then process and publish it in the background.

One worker thread, so one clip is encoded at a time -- the droplet is small,
and two simultaneous transcodes would just make both slow. Jobs live in
memory: a container restart forgets them (and startup clears the work
directory), which costs at most one re-upload.

States:  receiving -> queued -> processing -> done
         (any of them can end in: error)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import media
from .storage import Storage

log = logging.getLogger(__name__)

_KEEP_FINISHED_SECONDS = 6 * 60 * 60


@dataclass
class Job:
    id: str
    filename: str
    meta: media.ClipMeta
    state: str = "receiving"
    step: str = "Waiting for the upload"
    received: int = 0
    size: int | None = None
    error: str | None = None
    slug: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "filename": self.filename, "title": self.meta.title,
            "state": self.state, "step": self.step, "received": self.received,
            "size": self.size, "error": self.error, "slug": self.slug,
            "created": self.created, "finished": self.finished,
        }


class JobQueue:
    def __init__(self, storage: Storage, workdir: Path, preset: str) -> None:
        self.storage = storage
        self.workdir = workdir
        self.preset = preset
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue[str] = queue.Queue()
        workdir.mkdir(parents=True, exist_ok=True)
        for leftover in workdir.iterdir():     # from a previous run
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
        threading.Thread(target=self._worker, name="clip-worker", daemon=True).start()

    # -- bookkeeping -------------------------------------------------------

    def create(self, filename: str, meta: media.ClipMeta, size: int | None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename[:200], meta=meta, size=size)
        with self._lock:
            self._prune()
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune()
            return [j.view() for j in sorted(self._jobs.values(), key=lambda j: -j.created)]

    def dismiss(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.state in ("done", "error"):
                del self._jobs[job_id]
                return True
            return False

    def _prune(self) -> None:
        cutoff = time.time() - _KEEP_FINISHED_SECONDS
        for jid in [j.id for j in self._jobs.values() if j.finished and j.finished < cutoff]:
            del self._jobs[jid]

    def upload_path(self, job: Job) -> Path:
        return self.workdir / f"{job.id}.upload"

    def fail(self, job: Job, message: str) -> None:
        job.state, job.error, job.step, job.finished = "error", message, "Failed", time.time()
        self.upload_path(job).unlink(missing_ok=True)

    def enqueue(self, job: Job) -> None:
        job.state, job.step = "queued", "Waiting for its turn"
        self._q.put(job.id)

    # -- the worker --------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job = self.get(self._q.get())
            if job is None:
                continue
            job.state = "processing"
            src = self.upload_path(job)

            def progress(msg: str, job: Job = job) -> None:
                job.step = msg

            try:
                entry = media.publish_clip(self.storage, src, job.meta,
                                           self.workdir / job.id, preset=self.preset,
                                           progress=progress)
                job.slug = entry["slug"]
                job.state, job.step, job.finished = "done", "Published", time.time()
                log.info("published %s from %s", job.slug, job.filename)
            except media.MediaError as err:
                self.fail(job, str(err))
            except Exception as err:  # noqa: BLE001 -- report, keep the worker alive
                log.exception("processing %s failed", job.filename)
                self.fail(job, f"Unexpected error: {err}")
            finally:
                src.unlink(missing_ok=True)
                workdir = self.workdir / job.id
                if workdir.is_dir():
                    for f in workdir.iterdir():
                        f.unlink(missing_ok=True)
                    workdir.rmdir()


def as_dict(job: Job) -> dict[str, Any]:
    return asdict(job)
