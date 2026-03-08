
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Path as FPath
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

"""
Feishu (Lark) Bitable backend for Cedar Sys - Point Manage System

What this server does:
- Keeps app_secret on the server (never in browser)
- Exchanges app_id/app_secret for tenant_access_token (cached)
- Reads/writes Feishu Bitable records
- Exposes a small REST API for your existing front-end fetch() calls

You MUST set environment variables (or a .env file via your shell):
  FEISHU_APP_ID=...
  FEISHU_APP_SECRET=...
  FEISHU_POINT_APP_TOKEN=...   # the bitable "app_token" for your Point_Manage base
  FEISHU_STUDENT_TABLE_ID=tbl6VArLeEuWaczs
  FEISHU_EVENT_TABLE_ID=tbl4rmvistECppMv

How to find app_token quickly:
  Open the Base in browser, the URL looks like:
    https://.../base/<APP_TOKEN>?table=...
  Copy the string right after /base/ and before ?  => that is app_token.
"""
from dotenv import load_dotenv
load_dotenv()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
POINT_APP_TOKEN = os.getenv("FEISHU_POINT_APP_TOKEN", "").strip()
STUDENT_TABLE_ID = os.getenv("FEISHU_STUDENT_TABLE_ID", "tbl6VArLeEuWaczs").strip()
EVENT_TABLE_ID = os.getenv("FEISHU_EVENT_TABLE_ID", "tbl4rmvistECppMv").strip()

# Field mapping: keys are the "public" names your front-end expects.
# values are the EXACT field names in Feishu Bitable.
STUDENT_FIELDS = {
    "id": "id",
    "grade": "grade",
    "class": "class",
    "name": "name",
}

EVENT_FIELDS = {
    "event_id": "event_id",
    "time": "time",               # store ISO string or timestamp string
    "student_id": "student_id",
    "delta": "delta",             # number
    "type": "type",
    "description": "description",
    "operator": "operator",
}

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

app = FastAPI(title="Cedar Sys - Feishu Point Backend", version="0.1.0")

# CORS for local development (adjust if you deploy)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class EventCreate(BaseModel):
    event_id: str
    time: str
    student_id: str
    delta: float
    type: str
    description: Optional[str] = ""
    operator: Optional[str] = ""

class EventPatch(BaseModel):
    time: Optional[str] = None
    student_id: Optional[str] = None
    delta: Optional[float] = None
    type: Optional[str] = None
    description: Optional[str] = None
    operator: Optional[str] = None


class TokenCache:
    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.expire_at: float = 0.0

    def valid(self) -> bool:
        return bool(self.token) and time.time() < (self.expire_at - 60)  # 60s safety

TOKEN_CACHE = TokenCache()

def _check_env() -> None:
    missing = []
    if not FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")
    if not FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not POINT_APP_TOKEN:
        missing.append("FEISHU_POINT_APP_TOKEN")
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing env vars: {', '.join(missing)}. Set them before running.",
        )

async def get_tenant_access_token(client: httpx.AsyncClient) -> str:
    _check_env()
    if TOKEN_CACHE.valid():
        return TOKEN_CACHE.token  # type: ignore

    url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}

    r = await client.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Token HTTP {r.status_code}: {r.text}")

    data = r.json()
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"Token error: {data}")

    token = data["tenant_access_token"]
    expire = int(data.get("expire", 3600))
    TOKEN_CACHE.token = token
    TOKEN_CACHE.expire_at = time.time() + expire
    return token

async def bitable_search_all(
    client: httpx.AsyncClient,
    app_token: str,
    table_id: str,
    field_map: Dict[str, str],
    filter_obj: Optional[Dict[str, Any]] = None,
    page_size: int = 500,
) -> List[Dict[str, Any]]:
    """
    Returns a list of "fields" dicts (already mapped to your public names),
    plus internal record_id as "_record_id".
    """
    token = await get_tenant_access_token(client)
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {token}"}

    results: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        body: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            body["page_token"] = page_token
        if filter_obj:
            body["filter"] = filter_obj

        r = await client.post(url, headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Search HTTP {r.status_code}: {r.text}")

        resp = r.json()
        if resp.get("code") != 0:
            raise HTTPException(status_code=502, detail=f"Search error: {resp}")

        data = resp.get("data", {}) or {}
        items = data.get("items", []) or []
        for it in items:
            fields = it.get("fields", {}) or {}
            mapped: Dict[str, Any] = {"_record_id": it.get("record_id")}
            for public_key, bitable_field in field_map.items():
                mapped[public_key] = fields.get(bitable_field)
            results.append(mapped)

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    return results

async def bitable_create_record(
    client: httpx.AsyncClient,
    app_token: str,
    table_id: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    token = await get_tenant_access_token(client)
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(url, headers=headers, json={"fields": fields}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Create HTTP {r.status_code}: {r.text}")
    resp = r.json()
    if resp.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"Create error: {resp}")
    return resp.get("data", {}) or {}

async def bitable_update_record(
    client: httpx.AsyncClient,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    token = await get_tenant_access_token(client)
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.put(url, headers=headers, json={"fields": fields}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Update HTTP {r.status_code}: {r.text}")
    resp = r.json()
    if resp.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"Update error: {resp}")
    return resp.get("data", {}) or {}

def _reverse_map(public_to_bitable: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in public_to_bitable.items()}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/api/points/students")
async def api_students():
    async with httpx.AsyncClient() as client:
        rows = await bitable_search_all(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=STUDENT_TABLE_ID,
            field_map=STUDENT_FIELDS,
        )
    # Strip internal record_id before returning to front-end
    return [{k: v for k, v in r.items() if k != "_record_id"} for r in rows]

@app.get("/api/points/events")
async def api_events():
    async with httpx.AsyncClient() as client:
        rows = await bitable_search_all(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            field_map=EVENT_FIELDS,
        )
    return [{k: v for k, v in r.items() if k != "_record_id"} for r in rows]

@app.post("/api/points/events")
async def api_create_event(evt: EventCreate):
    # Map public keys -> bitable field names
    bitable_fields = {}
    for public_key, bitable_field in EVENT_FIELDS.items():
        bitable_fields[bitable_field] = getattr(evt, public_key)

    async with httpx.AsyncClient() as client:
        data = await bitable_create_record(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            fields=bitable_fields,
        )
    return {"ok": True, "record_id": (data.get("record") or {}).get("record_id")}

@app.patch("/api/points/events/{event_id}")
async def api_update_event(
    event_id: str = FPath(..., description="Your business event_id field"),
    patch: EventPatch = None,
):
    # Step 1: search by event_id to find record_id
    async with httpx.AsyncClient() as client:
        filter_obj = {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": EVENT_FIELDS["event_id"],
                    "operator": "is",
                    "value": [event_id],
                }
            ],
        }
        matches = await bitable_search_all(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            field_map=EVENT_FIELDS,
            filter_obj=filter_obj,
            page_size=50,
        )

        if not matches:
            raise HTTPException(status_code=404, detail=f"event_id not found: {event_id}")
        record_id = matches[0].get("_record_id")
        if not record_id:
            raise HTTPException(status_code=502, detail="Matched record has no record_id")

        # Step 2: build update fields (only provided keys)
        update_fields: Dict[str, Any] = {}
        for public_key, bitable_field in EVENT_FIELDS.items():
            if public_key == "event_id":
                continue
            val = getattr(patch, public_key) if patch else None
            if val is not None:
                update_fields[bitable_field] = val

        if not update_fields:
            return {"ok": True, "updated": False}

        await bitable_update_record(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            record_id=record_id,
            fields=update_fields,
        )

    return {"ok": True, "updated": True, "record_id": record_id}
