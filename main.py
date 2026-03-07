from fastapi import FastAPI, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, os, asyncio
from datetime import datetime, timedelta

app = FastAPI(title="LabQCert API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Token cache (in-memory, 12h)
_token_cache: dict = {}

TURKAK_BASE = "https://api.turkak.org.tr"

# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────
class TokenRequest(BaseModel):
    username: str
    password: str
    apiUrl: Optional[str] = TURKAK_BASE

class FirmaModel(BaseModel):
    ad: str
    adres: str = ""
    tel: str = ""
    mail: str = ""

class CihazModel(BaseModel):
    ad: str
    seriNo: str = ""
    marka: str = ""
    model: str = ""

class KalibModel(BaseModel):
    tarih: str
    yapan: str = ""
    yer: str = ""

class NumaraAlRequest(BaseModel):
    token: str
    apiUrl: Optional[str] = TURKAK_BASE
    firma: FirmaModel
    cihaz: CihazModel
    kalibrasyon: KalibModel
    fileId: Optional[str] = None

class RevizeRequest(BaseModel):
    token: str
    tbdsId: str
    revizeTarih: str
    revizeNot: str = ""
    apiUrl: Optional[str] = TURKAK_BASE

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
async def turkak_get_token(username: str, password: str, api_url: str) -> str:
    cache_key = username
    cached = _token_cache.get(cache_key)
    if cached and cached["expires"] > datetime.now():
        return cached["token"]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{api_url}/SSO/signin",
            json={"Username": username, "Password": password}
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("Token") or data.get("token")
        if not token:
            raise HTTPException(400, "Token alınamadı: " + str(data))

        _token_cache[cache_key] = {
            "token": token,
            "expires": datetime.now() + timedelta(hours=11, minutes=50)
        }
        return token

async def get_or_create_customer(token: str, api_url: str, firma: FirmaModel, file_id: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        # Son müşterileri çek, firma adıyla eşleştir
        r = await client.get(
            f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetData/",
            headers=headers
        )
        if r.status_code == 200:
            musteriler = r.json()
            for m in musteriler:
                if firma.ad.lower() in (m.get("Name","")).lower():
                    return m["ID"]

        # Yoksa yeni müşteri oluştur - önce ülke/şehir verisi al
        meta_r = await client.get(
            f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCustomerGetData/tr",
            headers=headers
        )
        meta = meta_r.json() if meta_r.status_code == 200 else {}
        countries = meta.get("Countries", [])
        cities    = meta.get("Cities", [])

        tr = next((c for c in countries if "türkiye" in c.get("Name","").lower() or c.get("Name","")=="Turkey"), None)
        country_id = tr["ID"] if tr else (countries[0]["ID"] if countries else None)
        city_id = cities[0]["ID"] if cities else None

        payload = [{
            "CountryID": country_id,
            "CityID": city_id,
            "FileID": file_id,
            "Title": firma.ad,
            "Address": firma.adres,
            "Phone": firma.tel,
            "EMail": firma.mail,
            "DVAccountType": 2  # Kurumsal Türkiye
        }]
        save_r = await client.post(
            f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCustomerSaveData/",
            json=payload, headers=headers
        )
        result = save_r.json()
        item1 = result.get("Item1", [])
        if item1:
            return item1[0]["ID"]
        raise HTTPException(400, "Müşteri oluşturulamadı: " + str(result))

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────
@app.post("/api/turkak/token")
async def get_token(req: TokenRequest):
    try:
        token = await turkak_get_token(req.username, req.password, req.apiUrl)
        cache = _token_cache.get(req.username, {})
        expiry_str = cache.get("expires", datetime.now()).strftime("%d.%m.%Y %H:%M")
        return {"token": token, "expiry": expiry_str}
    except httpx.HTTPStatusError as e:
        raise HTTPException(401, f"Türkak login hatası: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/turkak/numara-al")
async def numara_al(req: NumaraAlRequest):
    try:
        api_url = req.apiUrl or TURKAK_BASE
        token   = req.token
        headers = {"Authorization": f"Bearer {token}"}

        # FileID yoksa GetData'dan ilk dosyayı al
        file_id = req.fileId
        if not file_id:
            async with httpx.AsyncClient(timeout=15) as client:
                meta_r = await client.get(
                    f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCustomerGetData/tr",
                    headers=headers
                )
                meta = meta_r.json() if meta_r.status_code == 200 else {}
                files = meta.get("Files", [])
                if files:
                    file_id = files[0]["ID"]

        if not file_id:
            raise HTTPException(400, "Türkak dosya ID bulunamadı. Ayarlar > Türkak > FileID girin.")

        # Müşteri ID al/oluştur
        customer_id = await get_or_create_customer(token, api_url, req.firma, file_id)

        # Sertifika kaydet
        kal_tarih = req.kalibrasyon.tarih or datetime.now().strftime("%Y-%m-%d")
        yayim_tarih = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        payload = [{
            "CustomerID": customer_id,
            "FirstReleaseDateOfTheDocument": yayim_tarih,
            "MachineOrDeviceType": f"{req.cihaz.marka} {req.cihaz.model} {req.cihaz.ad}".strip(),
            "DeviceSerialNumber": req.cihaz.seriNo,
            "PersonnelPerformingCalibration": req.kalibrasyon.yapan,
            "CalibrationLocation": req.kalibrasyon.yer,
            "CalibrationDate": f"{kal_tarih}T00:00:00"
        }]

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                json=payload, headers=headers
            )
            result = r.json()

        item1 = result.get("Item1", [])
        item2 = result.get("Item2", [])

        if not item1 and item2:
            raise HTTPException(400, "Sertifika kaydedilemedi: " + item2[0].get("ErrorDescription",""))

        if not item1:
            raise HTTPException(400, "Sertifika kaydedilemedi (zorunlu alan hatası)")

        cert_id = item1[0]["ID"]

        # Kısa bekleme — TBDSNumber atanması için
        await asyncio.sleep(2)

        async with httpx.AsyncClient(timeout=15) as client:
            r2 = await client.get(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                headers=headers
            )
            cert_data = r2.json()

        return {
            "id": cert_id,
            "tbdsNo": cert_data.get("TBDSNumber",""),
            "sertNo": cert_data.get("CertificationBodyDocumentNumber",""),
            "state": cert_data.get("State","")
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/turkak/sertifika-durum/{cert_id}")
async def sertifika_durum(cert_id: str, authorization: str = Header(None), x_api_url: str = Header(None)):
    token   = (authorization or "").replace("Bearer ","").strip()
    api_url = x_api_url or TURKAK_BASE
    if not token:
        raise HTTPException(401, "Token gerekli")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            data = r.json()
        return {
            "id": data.get("ID",""),
            "tbdsNo": data.get("TBDSNumber",""),
            "sertNo": data.get("CertificationBodyDocumentNumber",""),
            "state": data.get("State","")
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/turkak/revize")
async def revize(req: RevizeRequest):
    api_url = req.apiUrl or TURKAK_BASE
    headers = {"Authorization": f"Bearer {req.token}"}
    payload = [{
        "ID": req.tbdsId,
        "RevisionDate": req.revizeTarih,
        "RevisionNote": req.revizeNot
    }]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                json=payload, headers=headers
            )
            result = r.json()
        item1 = result.get("Item1",[])
        item2 = result.get("Item2",[])
        if item2 and not item1:
            raise HTTPException(400, item2[0].get("ErrorDescription","Revize hatası"))
        return {"ok": True, "id": item1[0]["ID"] if item1 else req.tbdsId}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/{path:path}")
def catch_all(path: str):
    return FileResponse("static/index.html")
