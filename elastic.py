"""
Elasticsearch integration for real estate listings.
Handles index creation, ingestion, and search.

Connection resolution order:
1. ES_URL / ES_USER / ES_PASSWORD or ES_API_KEY env vars
2. `Elasticsearch endpoint` and `Elasticsearch token` entries in realtor/data/data.txt
3. http://localhost:9200 (no auth)
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

_DATA_FILE = Path(__file__).parent / "data" / "data.txt"

INDEX = "realestate_listings"

MAPPING = {
    "mappings": {
        "properties": {
            "raw":        {"type": "text",    "analyzer": "standard"},
            "rooms":      {"type": "integer"},
            "price":      {"type": "integer"},
            "location":   {"type": "text",    "fields": {"keyword": {"type": "keyword"}}},
            "phone":      {"type": "keyword"},
            "source_file":{"type": "keyword"},
            "ingested_at":{"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}


def _credentials_from_file() -> dict:
    if not _DATA_FILE.exists():
        return {}
    text = _DATA_FILE.read_text(encoding="utf-8")
    out = {}
    m = re.search(r"Elasticsearch endpoint\s+(\S+)", text)
    if m:
        out["url"] = m.group(1)
    m = re.search(r"Elasticsearch token\s+(\S+)", text)
    if m:
        out["api_key"] = m.group(1)
    return out


def get_client() -> Elasticsearch:
    url     = os.getenv("ES_URL", "")
    user    = os.getenv("ES_USER", "")
    pw      = os.getenv("ES_PASSWORD", "")
    api_key = os.getenv("ES_API_KEY", "")

    if not url or not (user or api_key):
        creds = _credentials_from_file()
        url = url or creds.get("url", "")
        api_key = api_key or creds.get("api_key", "")

    if not url:
        url = "http://localhost:9200"

    if user and pw:
        return Elasticsearch(url, basic_auth=(user, pw))
    if api_key:
        return Elasticsearch(url, api_key=api_key)
    return Elasticsearch(url)


def ensure_index(es: Elasticsearch):
    if not es.indices.exists(index=INDEX):
        es.indices.create(index=INDEX, body=MAPPING)


def ingest_listings(listings, source_file: str = "") -> tuple[int, int]:
    """Bulk-index listings. Returns (success_count, error_count)."""
    es = get_client()
    ensure_index(es)

    def actions():
        for listing in listings:
            doc_id = hashlib.sha1(listing.raw.encode()).hexdigest()
            yield {
                "_op_type": "index",
                "_index": INDEX,
                "_id": doc_id,
                "_source": {
                    "raw":         listing.raw,
                    "rooms":       listing.rooms,
                    "price":       listing.price,
                    "location":    listing.location,
                    "phone":       listing.phone,
                    "source_file": source_file,
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                },
            }

    success, errors = bulk(es, actions(), raise_on_error=False, stats_only=False)
    error_count = len(errors) if isinstance(errors, list) else errors
    return success, error_count


def search_listings(
    keywords: list[str] | None = None,
    rooms: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    location: str | None = None,
    size: int = 200,
) -> list[dict]:
    es = get_client()
    if not es.indices.exists(index=INDEX):
        return []

    must = []
    filter_ = []

    if keywords:
        must.append({"match": {"raw": " ".join(keywords)}})

    if rooms is not None:
        filter_.append({"term": {"rooms": rooms}})

    if location:
        must.append({"match": {"location": location}})

    price_range = {}
    if min_price:
        price_range["gte"] = min_price
    if max_price:
        price_range["lte"] = max_price
    if price_range:
        filter_.append({"range": {"price": price_range}})

    query = {"bool": {"must": must, "filter": filter_}} if (must or filter_) else {"match_all": {}}

    resp = es.search(index=INDEX, query=query, size=size, sort=[{"ingested_at": "desc"}])
    return [hit["_source"] for hit in resp["hits"]["hits"]]


def count_total() -> int:
    es = get_client()
    if not es.indices.exists(index=INDEX):
        return 0
    return es.count(index=INDEX)["count"]


def count_before(dt: str) -> int:
    """Count documents ingested before the given ISO date string."""
    es = get_client()
    if not es.indices.exists(index=INDEX):
        return 0
    return es.count(index=INDEX, query={"range": {"ingested_at": {"lt": dt}}})["count"]


def delete_before(dt: str) -> int:
    """Delete all documents ingested before the given ISO date string. Returns deleted count."""
    es = get_client()
    if not es.indices.exists(index=INDEX):
        return 0
    resp = es.delete_by_query(
        index=INDEX,
        query={"range": {"ingested_at": {"lt": dt}}},
        refresh=True,
    )
    return resp["deleted"]


def get_stats() -> dict:
    """Returns aggregated statistics for the admin dashboard."""
    es = get_client()
    if not es.indices.exists(index=INDEX):
        return {}
    resp = es.search(
        index=INDEX,
        size=0,
        aggs={
            "valid_prices": {
                "filter": {"range": {"price": {"gte": 200_000, "lte": 50_000_000}}},
                "aggs": {
                    "avg_price": {"avg": {"field": "price"}},
                    "min_price": {"min": {"field": "price"}},
                    "max_price": {"max": {"field": "price"}},
                },
            },
            "rooms_dist":    {"terms": {"field": "rooms",            "size": 20}},
            "top_locations": {"terms": {"field": "location.keyword", "size": 10}},
            "by_source":     {"terms": {"field": "source_file",      "size": 30}},
            "by_day":        {"date_histogram": {"field": "ingested_at", "calendar_interval": "day", "format": "yyyy-MM-dd"}},
        },
    )
    aggs = resp["aggregations"]
    vp = aggs["valid_prices"]
    return {
        "avg_price":     vp["avg_price"]["value"],
        "min_price":     vp["min_price"]["value"],
        "max_price":     vp["max_price"]["value"],
        "rooms_dist":    {b["key"]: b["doc_count"] for b in aggs["rooms_dist"]["buckets"]},
        "top_locations": {b["key"]: b["doc_count"] for b in aggs["top_locations"]["buckets"] if b["key"]},
        "by_source":     {b["key"] or "(ללא שם)": b["doc_count"] for b in aggs["by_source"]["buckets"]},
        "by_day":        {b["key_as_string"]: b["doc_count"] for b in aggs["by_day"]["buckets"]},
    }


def ping() -> bool:
    try:
        return get_client().ping()
    except Exception:
        return False
