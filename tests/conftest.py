import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main


@pytest.fixture
def real_mongo_client():
    uri = os.getenv("MONGO_TEST_URI")
    if not uri:
        pytest.skip("MONGO_TEST_URI is not configured; real MongoDB integration test")
    mongo_client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    version = tuple(int(part) for part in mongo_client.server_info()["version"].split(".")[:2])
    if version < (6, 0):
        mongo_client.close()
        pytest.fail("MongoDB 6.0+ is required for the aggregation integration tests")
    database_name = "hronetask_test_" + uuid.uuid4().hex
    main.client = mongo_client
    main.db = mongo_client[database_name]
    main.ensure_indexes()
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        mongo_client.drop_database(database_name)
        mongo_client.close()