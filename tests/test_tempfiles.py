"""Tests for TempJobDir and cleanup_stale_dirs."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.media.tempfiles import TempJobDir, cleanup_stale_dirs


async def test_temp_job_dir_created_and_removed_on_success(tmp_path: Path) -> None:
    job_dir_path = None
    async with TempJobDir(str(tmp_path), "job-123") as job_dir:
        job_dir_path = job_dir
        assert job_dir.exists()
        (job_dir / "file.txt").write_text("hello")

    assert job_dir_path is not None
    assert not job_dir_path.exists()


async def test_temp_job_dir_removed_even_on_exception(tmp_path: Path) -> None:
    job_dir_path = None
    with pytest.raises(ValueError, match="boom"):
        async with TempJobDir(str(tmp_path), "job-456") as job_dir:
            job_dir_path = job_dir
            assert job_dir.exists()
            raise ValueError("boom")

    assert job_dir_path is not None
    assert not job_dir_path.exists()


async def test_temp_job_dir_path_is_scoped_under_workdir_tmp(tmp_path: Path) -> None:
    async with TempJobDir(str(tmp_path), "job-789") as job_dir:
        assert job_dir == tmp_path / "tmp" / "job-789"


def test_cleanup_stale_dirs_removes_old_and_keeps_recent(tmp_path: Path) -> None:
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()

    old_dir = tmp_root / "old-job"
    old_dir.mkdir()
    (old_dir / "leftover.bin").write_bytes(b"x")

    recent_dir = tmp_root / "recent-job"
    recent_dir.mkdir()

    # Backdate the old dir's mtime by 2 hours; leave the recent one as-is.
    two_hours_ago = time.time() - 7200
    os.utime(old_dir, (two_hours_ago, two_hours_ago))

    removed = cleanup_stale_dirs(str(tmp_path), max_age_minutes=60)

    assert removed == 1
    assert not old_dir.exists()
    assert recent_dir.exists()


def test_cleanup_stale_dirs_no_tmp_root_returns_zero(tmp_path: Path) -> None:
    assert cleanup_stale_dirs(str(tmp_path / "does_not_exist"), max_age_minutes=60) == 0


def test_cleanup_stale_dirs_empty_tmp_root_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()
    assert cleanup_stale_dirs(str(tmp_path), max_age_minutes=60) == 0
