
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Path as FPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

"""
Feishu (Lark) Bitable backend for Cedar Sys

What this server does:
- Keeps app_secret on the server (never in browser)
- Exchanges app_id/app_secret for tenant_access_token (cached)
- Reads/writes Feishu Bitable records
- Exposes a small REST API for your existing front-end fetch() calls

You MUST set environment variables (or a .env file via your shell):
  FEISHU_APP_ID=...
  FEISHU_APP_SECRET=...
  FEISHU_POINT_APP_TOKEN=...   # the bitable "app_token" for your Point_Manage base
  FEISHU_GOODS_APP_TOKEN=...   # optional; if omitted, fallback to FEISHU_POINT_APP_TOKEN
  FEISHU_STUDENT_TABLE_ID=tbl6VArLeEuWaczs
  FEISHU_EVENT_TABLE_ID=tbl4rmvistECppMv
  FEISHU_GOODS_TABLE_ID=...

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
GOODS_APP_TOKEN = os.getenv("FEISHU_GOODS_APP_TOKEN", "").strip() or POINT_APP_TOKEN
STUDENT_TABLE_ID = os.getenv("FEISHU_STUDENT_TABLE_ID", "tbl6VArLeEuWaczs").strip()
EVENT_TABLE_ID = os.getenv("FEISHU_EVENT_TABLE_ID", "tbl4rmvistECppMv").strip()
GOODS_TABLE_ID = os.getenv("FEISHU_GOODS_TABLE_ID", "").strip()

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

ITEM_FIELDS = {
    "item_id": "item_id",
    "name": "name",
    "price": "price",
    "qty": "qty",
    "img_url": "img_url",
    "note": "note",
    "update_at": "update_at",
}

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
HTML_DIR = BASE_DIR / "html"

app = FastAPI(title="Cedar Sys - Feishu Backend", version="0.2.0")

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
    student_id: str | int | float
    delta: float | int | str
    type: str
    description: Optional[str] = ""
    operator: Optional[str] = ""

    @field_validator("student_id", "delta", mode="before")
    @classmethod
    def normalize_numeric_strings(cls, value: Any) -> Any:
        return _coerce_number_like(value)

class EventPatch(BaseModel):
    time: Optional[str] = None
    student_id: Optional[str | int | float] = None
    delta: Optional[float | int | str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    operator: Optional[str] = None

    @field_validator("student_id", "delta", mode="before")
    @classmethod
    def normalize_numeric_strings(cls, value: Any) -> Any:
        return _coerce_number_like(value)


class ItemCreate(BaseModel):
    item_id: str
    name: str
    price: int | str
    qty: int | str
    img_url: Optional[str] = ""
    note: Optional[str] = ""
    update_at: Optional[str] = ""

    @field_validator("price", "qty", mode="before")
    @classmethod
    def normalize_numeric_strings(cls, value: Any) -> Any:
        return _coerce_number_like(value)


class ItemPatch(BaseModel):
    name: Optional[str] = None
    price: Optional[int | str] = None
    qty: Optional[int | str] = None
    img_url: Optional[str] = None
    note: Optional[str] = None
    update_at: Optional[str] = None

    @field_validator("price", "qty", mode="before")
    @classmethod
    def normalize_numeric_strings(cls, value: Any) -> Any:
        return _coerce_number_like(value)


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
    if not GOODS_TABLE_ID:
        missing.append("FEISHU_GOODS_TABLE_ID")
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing env vars: {', '.join(missing)}. Set them before running.",
        )

def _extract_bitable_scalar(value: Any) -> Any:
    """
    Normalize Feishu Bitable field values into plain JSON scalars whenever possible.
    Common text cells come back as rich-text arrays like:
      [{"text": "G1", "type": "text"}]
    """
    if value is None:
        return None

    if isinstance(value, list):
        if not value:
            return ""

        if all(isinstance(item, dict) and "text" in item for item in value):
            return "".join(str(item.get("text", "")) for item in value).strip()

        if all(isinstance(item, dict) for item in value):
            for key in ("name", "text", "value", "id"):
                parts = [
                    str(item.get(key)).strip()
                    for item in value
                    if item.get(key) not in (None, "")
                ]
                if parts:
                    return ", ".join(parts)

        parts = [str(item).strip() for item in value if item not in (None, "")]
        return ", ".join(parts)

    if isinstance(value, dict):
        for key in ("text", "name", "value", "id"):
            if value.get(key) not in (None, ""):
                return value.get(key)
        return str(value)

    if isinstance(value, str):
        return value.strip()

    return value

def _coerce_number_like(value: Any) -> Any:
    """
    Backward compatibility for payloads coming from the old spreadsheet-based front-end:
    numeric columns were often posted as strings like "1001" or "-5".
    Feishu Number fields require actual numbers.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return text
        try:
            if any(ch in text for ch in (".", "e", "E")):
                return float(text)
            return int(text)
        except ValueError:
            return text

    return value

def _normalize_student_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    normalized = {
        "id": _extract_bitable_scalar(row.get("id")),
        "grade": _extract_bitable_scalar(row.get("grade")),
        "class": _extract_bitable_scalar(row.get("class")),
        "name": _extract_bitable_scalar(row.get("name")),
    }

    if all(normalized[key] in (None, "") for key in ("id", "grade", "class", "name")):
        return None

    return normalized

def _normalize_event_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    normalized = {
        "event_id": _extract_bitable_scalar(row.get("event_id")),
        "time": _extract_bitable_scalar(row.get("time")),
        "student_id": _extract_bitable_scalar(row.get("student_id")),
        "delta": _extract_bitable_scalar(row.get("delta")),
        "type": _extract_bitable_scalar(row.get("type")),
        "description": _extract_bitable_scalar(row.get("description")),
        "operator": _extract_bitable_scalar(row.get("operator")),
    }

    if all(
        normalized[key] in (None, "")
        for key in ("event_id", "time", "student_id", "delta", "type", "description", "operator")
    ):
        return None

    return normalized


def _normalize_item_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    normalized = {
        "item_id": _extract_bitable_scalar(row.get("item_id")),
        "name": _extract_bitable_scalar(row.get("name")),
        "price": _extract_bitable_scalar(row.get("price")),
        "qty": _extract_bitable_scalar(row.get("qty")),
        "img_url": _extract_bitable_scalar(row.get("img_url")),
        "note": _extract_bitable_scalar(row.get("note")),
        "update_at": _extract_bitable_scalar(row.get("update_at")),
    }

    if all(
        normalized[key] in (None, "")
        for key in ("item_id", "name", "price", "qty", "img_url", "note", "update_at")
    ):
        return None

    for key in ("price", "qty"):
        normalized[key] = _coerce_number_like(normalized[key])

    return normalized

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


async def bitable_delete_record(
    client: httpx.AsyncClient,
    app_token: str,
    table_id: str,
    record_id: str,
) -> Dict[str, Any]:
    token = await get_tenant_access_token(client)
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.delete(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Delete HTTP {r.status_code}: {r.text}")
    resp = r.json()
    if resp.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"Delete error: {resp}")
    return resp.get("data", {}) or {}

def _reverse_map(public_to_bitable: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in public_to_bitable.items()}


def _html_response(filename: str) -> FileResponse:
    file_path = (RUNTIME_DIR / "html" / filename) if getattr(sys, "frozen", False) else (HTML_DIR / filename)
    if not file_path.exists():
        raise HTTPException(status_code=500, detail=f"Missing HTML file: {filename}")
    return FileResponse(file_path)


async def find_record_id_by_field(
    client: httpx.AsyncClient,
    app_token: str,
    table_id: str,
    field_map: Dict[str, str],
    public_field: str,
    value: Any,
    page_size: int = 50,
) -> str:
    filter_obj = {
        "conjunction": "and",
        "conditions": [
            {
                "field_name": field_map[public_field],
                "operator": "is",
                "value": [value],
            }
        ],
    }
    matches = await bitable_search_all(
        client=client,
        app_token=app_token,
        table_id=table_id,
        field_map=field_map,
        filter_obj=filter_obj,
        page_size=page_size,
    )
    if not matches:
        raise HTTPException(status_code=404, detail=f"{public_field} not found: {value}")

    record_id = matches[0].get("_record_id")
    if not record_id:
        raise HTTPException(status_code=502, detail="Matched record has no record_id")
    return record_id

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/app/pms", include_in_schema=False)
async def app_pms():
    return _html_response("Point Manage Sys.html")


@app.get("/app/gms", include_in_schema=False)
async def app_gms():
    return _html_response("Good Manage Sys_LINKED_v2.html")

@app.get("/api/points/students")
async def api_students():
    async with httpx.AsyncClient() as client:
        rows = await bitable_search_all(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=STUDENT_TABLE_ID,
            field_map=STUDENT_FIELDS,
        )
    normalized_rows = []
    for row in rows:
        public_row = {k: v for k, v in row.items() if k != "_record_id"}
        normalized = _normalize_student_row(public_row)
        if normalized is not None:
            normalized_rows.append(normalized)
    return normalized_rows

@app.get("/api/points/events")
async def api_events():
    async with httpx.AsyncClient() as client:
        rows = await bitable_search_all(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            field_map=EVENT_FIELDS,
        )
    normalized_rows = []
    for row in rows:
        public_row = {k: v for k, v in row.items() if k != "_record_id"}
        normalized = _normalize_event_row(public_row)
        if normalized is not None:
            normalized_rows.append(normalized)
    return normalized_rows

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
        record_id = await find_record_id_by_field(
            client=client,
            app_token=POINT_APP_TOKEN,
            table_id=EVENT_TABLE_ID,
            field_map=EVENT_FIELDS,
            public_field="event_id",
            value=event_id,
        )

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


@app.get("/api/gms/items")
async def api_gms_items():
    async with httpx.AsyncClient() as client:
        rows = await bitable_search_all(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            field_map=ITEM_FIELDS,
        )
    normalized_rows = []
    for row in rows:
        public_row = {k: v for k, v in row.items() if k != "_record_id"}
        normalized = _normalize_item_row(public_row)
        if normalized is not None:
            normalized_rows.append(normalized)
    return normalized_rows


@app.post("/api/gms/items")
async def api_create_gms_item(item: ItemCreate):
    bitable_fields = {}
    for public_key, bitable_field in ITEM_FIELDS.items():
        bitable_fields[bitable_field] = getattr(item, public_key)

    async with httpx.AsyncClient() as client:
        data = await bitable_create_record(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            fields=bitable_fields,
        )
    return {"ok": True, "record_id": (data.get("record") or {}).get("record_id")}


@app.patch("/api/gms/items/{item_id}")
async def api_update_gms_item(
    item_id: str = FPath(..., description="Your business item_id field"),
    patch: ItemPatch = None,
):
    async with httpx.AsyncClient() as client:
        record_id = await find_record_id_by_field(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            field_map=ITEM_FIELDS,
            public_field="item_id",
            value=item_id,
        )

        patch_qty = patch.qty if patch else None
        if isinstance(patch_qty, (int, float)) and patch_qty <= 0:
            await bitable_delete_record(
                client=client,
                app_token=GOODS_APP_TOKEN,
                table_id=GOODS_TABLE_ID,
                record_id=record_id,
            )
            return {"ok": True, "updated": False, "deleted": True, "record_id": record_id}

        update_fields: Dict[str, Any] = {}
        for public_key, bitable_field in ITEM_FIELDS.items():
            if public_key == "item_id":
                continue
            val = getattr(patch, public_key) if patch else None
            if val is not None:
                update_fields[bitable_field] = val

        if not update_fields:
            return {"ok": True, "updated": False}

        await bitable_update_record(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            record_id=record_id,
            fields=update_fields,
        )

    return {"ok": True, "updated": True, "record_id": record_id}


@app.delete("/api/gms/items/{item_id}")
async def api_delete_gms_item(
    item_id: str = FPath(..., description="Your business item_id field"),
):
    async with httpx.AsyncClient() as client:
        record_id = await find_record_id_by_field(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            field_map=ITEM_FIELDS,
            public_field="item_id",
            value=item_id,
        )
        await bitable_delete_record(
            client=client,
            app_token=GOODS_APP_TOKEN,
            table_id=GOODS_TABLE_ID,
            record_id=record_id,
        )

    return {"ok": True, "deleted": True, "record_id": record_id}
