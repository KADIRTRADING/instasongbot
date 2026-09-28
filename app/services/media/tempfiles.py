"""Per-job temp directory management + a backstop sweep for anything a crashed
job left behind. See ARCHITECTURE.md §12.

Every job gets its own `WORKDIR/tmp/<job_id>/` via `TempJobDir`, always removed
in a `finally` block regardless of success or failure. `cleanup_stale_dirs` is
run periodically (an arq cron job, see workers/tasks.py) as a backstop for the
case where a worker process was killed mid-job and its `finally` never ran.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.logging_conf import get_logger

logger = get_logger(__name__)


class TempJobDir:
    """Async context manager: creates WORKDIR/tmp/<job_id>/ on entry, always
    removes it (recursively) on exit.

    Usage:
        async with TempJobDir(settings.WORKDIR, job_id) as job_dir:
            ... download/convert into job_dir ...
        # job_dir and everything in it is gone here, even if an exception
        # was raised inside the `async with` block.
    """

    def __init__(self, workdir: str, job_id: str) -> None:
        self.path = Path(workdir) / "tmp" / job_id

    async def __aenter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        shutil.rmtree(self.path, ignore_errors=True)
        return False  # never swallow exceptions raised inside the block


def cleanup_stale_dirs(workdir: str, max_age_minutes: int) -> int:
    """Remove any tmp/<job_id> directory older than `max_age_minutes`.
    Synchronous by design — called from an arq cron job via `run_in_executor`
    equivalent (arq already runs cron functions as coroutines, but the file
    walk itself is cheap enough to do inline; see workers/tasks.py).
    Returns the number of directories removed.
    """
    tmp_root = Path(workdir) / "tmp"
    if not tmp_root.exists():
        return 0

    cutoff = time.time() - (max_age_minutes * 60)
    removed = 0
    for entry in tmp_root.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                logger.info("cleanup_stale_dir", path=str(entry))
        except OSError as exc:
            logger.warning("cleanup_stale_dir_failed", path=str(entry), error=str(exc))
    return removed
