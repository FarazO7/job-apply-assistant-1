from pymongo import MongoClient, ASCENDING

from .config import settings

client = MongoClient(settings.mongodb_uri)
db = client[settings.mongodb_db]

applications = db["applications"]
profiles = db["profiles"]  # singleton document, _id="me"


def ensure_indexes() -> None:
    # dedupe_key is unique so the same posting never gets queued or sent twice
    applications.create_index([("dedupe_key", ASCENDING)], unique=True)
    applications.create_index([("status", ASCENDING)])
    applications.create_index([("created_at", ASCENDING)])
