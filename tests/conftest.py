from datetime import date

import pytest

from job_tracker import service
from job_tracker.storage import Store

TODAY = date(2026, 10, 14)  # a Wednesday; Monday of that week is 2026-10-12


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "Job Tracker", today=lambda: TODAY)


@pytest.fixture
def seed(store):
    """seed(**fields) -> new application id. Defaults: a Ready Backend Engineer at Acme."""

    def _seed(**fields):
        base = {"company": "Acme Pty Ltd", "role": "Backend Engineer", "status": "Ready"}
        return service.add_application(store, {**base, **fields})

    return _seed
