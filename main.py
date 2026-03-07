from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, os, asyncio, json
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="LabQCert API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_token_cache: dict = {}
TURKAK_BASE = "https://api.turkak.org.tr"

def get_db():
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL tanımlı değil")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                id      SERIAL PRIMARY KEY,
                key     TEXT UNIQUE NOT NULL,
                value   JSONB NOT NULL,
                updated TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit(); cur.close(); conn.close()
        print("DB init OK")
    except Exception as e:
        print("DB init error:", e)

@app.on_event("startup")
async def startup():
    init_db()

# ─── STATE API ───────────────────────────────
@app.get("/api/state")
def get_all_state():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_state")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {row["key"]: row["value"] for row in rows}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/state/{key}")
async def save_state_key(key: str, request: Request):
    try:
        data = await request.json()
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_state (key, value, updated) VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated=NOW()
        """, (key, json.dumps(data)))
        conn.commit(); cur.close(); conn.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/state/batch")
async def save_state_batch(request: Request):
    try:
        data = await request.json()
        conn = get_db(); cur = conn.cursor()
        for key, value in data.items():
            cur.execute("""
                INSERT INTO app_state (key, value, updated) VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated=NOW()
            """, (key, json.dumps(value)))
        conn.commit(); cur.close(); conn.close()
        return {"ok": True, "count": len(data)}
    except Exception as e:
        raise HTTPException(500, str(e))

# ─── TÜRKAK API ──────────────────────────────
class TokenRequest(BaseModel):
    username: str; password: str; apiUrl: Optional[str] = TURKAK_BASE

class FirmaModel(BaseModel):
    ad: str; adres: str=""; tel: str=""; mail: str=""

class CihazModel(BaseModel):
    ad: str; seriNo: str=""; marka: str=""; model: str=""

class KalibModel(BaseModel):
    tarih: str; yapan: str=""; yer: str=""

class NumaraAlRequest(BaseModel):
    token: str; apiUrl: Optional[str]=TURKAK_BASE
    firma: FirmaModel; cihaz: CihazModel; kalibrasyon: KalibModel
    fileId: Optional[str]=None

class RevizeRequest(BaseModel):
    token: str; tbdsId: str; revizeTarih: str; revizeNot: str=""; apiUrl: Optional[str]=TURKAK_BASE

async def turkak_get_token(username, password, api_url):
    cached = _token_cache.get(username)
    if cached and cached["expires"] > datetime.now():
        return cached["token"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{api_url}/SSO/signin", json={"Username": username, "Password": password})
        r.raise_for_status()
        data = r.json()
        token = data.get("Token") or data.get("token")
        if not token: raise HTTPException(400, "Token alınamadı")
        _token_cache[username] = {"token": token, "expires": datetime.now() + timedelta(hours=11, minutes=50)}
        return token

@app.post("/api/turkak/token")
async def get_token(req: TokenRequest):
    try:
        token = await turkak_get_token(req.username, req.password, req.apiUrl)
        expiry = _token_cache.get(req.username, {}).get("expires", datetime.now()).strftime("%d.%m.%Y %H:%M")
        return {"token": token, "expiry": expiry}
    except httpx.HTTPStatusError as e:
        raise HTTPException(401, f"Türkak login hatası: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/turkak/numara-al")
async def numara_al(req: NumaraAlRequest):
    try:
        api_url = req.apiUrl or TURKAK_BASE
        headers = {"Authorization": f"Bearer {req.token}"}
        file_id = req.fileId
        if not file_id:
            async with httpx.AsyncClient(timeout=15) as client:
                meta_r = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCustomerGetData/tr", headers=headers)
                meta = meta_r.json() if meta_r.status_code == 200 else {}
                files = meta.get("Files", [])
                if files: file_id = files[0]["ID"]
        if not file_id: raise HTTPException(400, "Türkak dosya ID bulunamadı")
        kal_tarih = req.kalibrasyon.tarih or datetime.now().strftime("%Y-%m-%d")
        payload = [{"FileID": file_id, "MachineOrDeviceType": f"{req.cihaz.marka} {req.cihaz.model} {req.cihaz.ad}".strip(), "DeviceSerialNumber": req.cihaz.seriNo, "PersonnelPerformingCalibration": req.kalibrasyon.yapan, "CalibrationLocation": req.kalibrasyon.yer, "CalibrationDate": f"{kal_tarih}T00:00:00", "FirstReleaseDateOfTheDocument": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/", json=payload, headers=headers)
            result = r.json()
        item1 = result.get("Item1", []); item2 = result.get("Item2", [])
        if not item1 and item2: raise HTTPException(400, item2[0].get("ErrorDescription","Sertifika kaydedilemedi"))
        if not item1: raise HTTPException(400, "Sertifika kaydedilemedi")
        cert_id = item1[0]["ID"]
        await asyncio.sleep(2)
        async with httpx.AsyncClient(timeout=15) as client:
            r2 = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}", headers=headers)
            cert_data = r2.json()
        return {"id": cert_id, "tbdsNo": cert_data.get("TBDSNumber",""), "sertNo": cert_data.get("CertificationBodyDocumentNumber",""), "state": cert_data.get("State","")}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/api/turkak/sertifika-durum/{cert_id}")
async def sertifika_durum(cert_id: str, authorization: str=Header(None), x_api_url: str=Header(None)):
    token = (authorization or "").replace("Bearer ","").strip()
    api_url = x_api_url or TURKAK_BASE
    if not token: raise HTTPException(401, "Token gerekli")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}", headers={"Authorization": f"Bearer {token}"})
            data = r.json()
        return {"id": data.get("ID",""), "tbdsNo": data.get("TBDSNumber",""), "sertNo": data.get("CertificationBodyDocumentNumber",""), "state": data.get("State","")}
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/api/turkak/revize")
async def revize(req: RevizeRequest):
    api_url = req.apiUrl or TURKAK_BASE
    headers = {"Authorization": f"Bearer {req.token}"}
    payload = [{"ID": req.tbdsId, "RevisionDate": req.revizeTarih, "RevisionNote": req.revizeNot}]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/", json=payload, headers=headers)
            result = r.json()
        item1 = result.get("Item1",[]); item2 = result.get("Item2",[])
        if item2 and not item1: raise HTTPException(400, item2[0].get("ErrorDescription","Revize hatası"))
        return {"ok": True, "id": item1[0]["ID"] if item1 else req.tbdsId}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

# ── Static
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root(): return FileResponse("static/index.html")

@app.get("/{path:path}")
def catch_all(path: str): return FileResponse("static/index.html")
