"""Test setup: always uses its own database (never MONGO_DB from the environment) and drops it first."""
import os

os.environ["MONGO_DB"] = "attendance_test"  # must be set before app.main is imported
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture(scope="session")
def client():
    main.client.drop_database("attendance_test")
    with TestClient(main.app) as c:  # runs startup -> creates indexes
        yield c
    main.client.drop_database("attendance_test")


@pytest.fixture(scope="session")
def db(client):
    return main.db
