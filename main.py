from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
from pydantic import BaseModel
from typing import Optional
import httpx, os, asyncio, json
from datetime import datetime, timedelta
import asyncpg

# ── API router (tüm /api rotaları burada)
api = APIRouter(prefix="/api")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_token_cache: dict = {}
TURKAK_BASE  = "https://api.turkak.org.tr"
_pool = None

def clean_db_url(url: str) -> str:
    return url.split("?")[0] if "?" in url else url

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            clean_db_url(DATABASE_URL), ssl="require", min_size=1, max_size=5)
    return _pool

async def init_db():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS app_state (
                    key     TEXT PRIMARY KEY,
                    value   JSONB NOT NULL,
                    updated TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        print("DB init OK")
    except Exception as e:
        print("DB init error:", e)

# ── STATE ─────────────────────────────────────
@api.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}

@api.get("/state")
async def get_all_state():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_state")
        return {r["key"]: json.loads(r["value"]) for r in rows}
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/state-batch")
async def save_state_batch(request: Request):
    try:
        data = await request.json()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for key, value in data.items():
                    await conn.execute("""
                        INSERT INTO app_state (key, value, updated)
                        VALUES ($1, $2::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE
                        SET value=EXCLUDED.value, updated=NOW()
                    """, key, json.dumps(value))
        return {"ok": True, "count": len(data)}
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/state/{key}")
async def save_state_key(key: str, request: Request):
    try:
        data = await request.json()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO app_state (key, value, updated)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value, updated=NOW()
            """, key, json.dumps(data))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── TÜRKAK ────────────────────────────────────
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
    token: str; tbdsId: str; revizeTarih: str
    revizeNot: str=""; apiUrl: Optional[str]=TURKAK_BASE

async def turkak_get_token(username, password, api_url):
    cached = _token_cache.get(username)
    if cached and cached["expires"] > datetime.now():
        return cached["token"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{api_url}/SSO/signin",
                              json={"Username": username, "Password": password})
        r.raise_for_status()
        data = r.json()
        token = data.get("Token") or data.get("token")
        if not token: raise HTTPException(400, "Token alinamadi")
        _token_cache[username] = {"token": token,
            "expires": datetime.now() + timedelta(hours=11, minutes=50)}
        return token

@api.post("/turkak/token")
async def get_token(req: TokenRequest):
    try:
        token = await turkak_get_token(req.username, req.password, req.apiUrl)
        expiry = _token_cache.get(req.username,{}).get("expires",datetime.now()).strftime("%d.%m.%Y %H:%M")
        return {"token": token, "expiry": expiry}
    except httpx.HTTPStatusError as e:
        raise HTTPException(401, f"Turkak login hatasi: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/turkak/numara-al")
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
        if not file_id: raise HTTPException(400, "Turkak dosya ID bulunamadi")
        kal_tarih = req.kalibrasyon.tarih or datetime.now().strftime("%Y-%m-%d")
        payload = [{"FileID": file_id,
            "MachineOrDeviceType": f"{req.cihaz.marka} {req.cihaz.model} {req.cihaz.ad}".strip(),
            "DeviceSerialNumber": req.cihaz.seriNo,
            "PersonnelPerformingCalibration": req.kalibrasyon.yapan,
            "CalibrationLocation": req.kalibrasyon.yer,
            "CalibrationDate": f"{kal_tarih}T00:00:00",
            "FirstReleaseDateOfTheDocument": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                                  json=payload, headers=headers)
            result = r.json()
        item1=result.get("Item1",[]); item2=result.get("Item2",[])
        if not item1 and item2: raise HTTPException(400, item2[0].get("ErrorDescription","Kayit hatasi"))
        if not item1: raise HTTPException(400, "Sertifika kaydedilemedi")
        cert_id = item1[0]["ID"]
        await asyncio.sleep(2)
        async with httpx.AsyncClient(timeout=15) as client:
            r2 = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                                  headers=headers)
            cert_data = r2.json()
        return {"id": cert_id, "tbdsNo": cert_data.get("TBDSNumber",""),
                "sertNo": cert_data.get("CertificationBodyDocumentNumber",""),
                "state": cert_data.get("State","")}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@api.get("/turkak/sertifika-durum/{cert_id}")
async def sertifika_durum(cert_id: str, authorization: str=Header(None), x_api_url: str=Header(None)):
    token = (authorization or "").replace("Bearer ","").strip()
    api_url = x_api_url or TURKAK_BASE
    if not token: raise HTTPException(401, "Token gerekli")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                headers={"Authorization": f"Bearer {token}"})
            data = r.json()
        return {"id": data.get("ID",""), "tbdsNo": data.get("TBDSNumber",""),
                "sertNo": data.get("CertificationBodyDocumentNumber",""),
                "state": data.get("State","")}
    except Exception as e: raise HTTPException(500, str(e))

@api.post("/turkak/revize")
async def revize(req: RevizeRequest):
    api_url = req.apiUrl or TURKAK_BASE
    headers = {"Authorization": f"Bearer {req.token}"}
    payload = [{"ID": req.tbdsId, "RevisionDate": req.revizeTarih, "RevisionNote": req.revizeNot}]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                                  json=payload, headers=headers)
            result = r.json()
        item1=result.get("Item1",[]); item2=result.get("Item2",[])
        if item2 and not item1: raise HTTPException(400, item2[0].get("ErrorDescription","Revize hatasi"))
        return {"ok": True, "id": item1[0]["ID"] if item1 else req.tbdsId}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))


# ── SERTİFİKA PDF ────────────────────────────
import base64, datetime as dt, io
from fastapi.responses import Response as FastAPIResponse

try:
    from xhtml2pdf import pisa
    PDF_OK = True
except ImportError:
    PDF_OK = False

def _fmt_tarih(v):
    if not v or v == "-": return "-"
    try:
        parts = str(v).split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
    except Exception:
        pass
    return str(v)

def _html_to_pdf(html_str: str) -> bytes:
    buf = io.BytesIO()
    pisa.CreatePDF(html_str.encode("utf-8"), dest=buf, encoding="utf-8")
    return buf.getvalue()

def _build_sertifika_html(s, mc, firma, fis, kal, det, ay, fv):
    lab_ad=ay.get("labAdi","Laboratuvar"); lab_adres=ay.get("labAdres","")
    lab_tel=ay.get("labTel",""); lab_mail=ay.get("labMail",""); akred_no=ay.get("akredNo","")
    lab_logo=ay.get("labLogo",""); turkak_logo=ay.get("turkakLogo",""); muhur_logo=ay.get("muhurLogo","")
    sert_no=fv.get("sertNo") or s.get("no") or "—"
    yayim_tarih=fv.get("yayimTarih") or det.get("yayimTarih") or ""
    sicaklik=fv.get("sicaklik") or det.get("sicaklik") or ""
    nem_val=fv.get("nem") or det.get("nem") or ""
    aciklama=fv.get("aciklama") or det.get("aciklama") or ""
    gorus=fv.get("gorus") or det.get("gorus") or ""
    prosedur_str=fv.get("prosedur",""); ref_str=fv.get("refCihazlar","")
    yapan=fv.get("yapan",""); onaylayan=fv.get("onaylayan","")
    ek_sayfa=s.get("ekPdfSayfa",0) or 0; toplam=(ek_sayfa+2) if ek_sayfa else "?"
    logo_h=f'<img src="{lab_logo}" style="max-height:70px;max-width:130px">' if lab_logo else ""
    turkak_h=f'<img src="{turkak_logo}" style="max-height:70px;max-width:130px">' if turkak_logo else ""
    muhur_h=f'<img src="{muhur_logo}" style="width:60px;height:60px">' if muhur_logo else '<div style="width:60px;height:60px;border:1pt solid #bbb;border-radius:50%"></div>'
    pros_rows=""
    for p in [x.strip() for x in prosedur_str.split(",") if x.strip()]:
        parts=p.split(" - ")
        pros_rows+=f'<tr><td style="padding:3px 6px;border:1px solid #ccc">{parts[0]}</td><td style="padding:3px 6px;border:1px solid #ccc">{" - ".join(parts[1:]) if len(parts)>1 else ""}</td></tr>'
    if not pros_rows: pros_rows='<tr><td colspan="2" style="padding:3px 6px;border:1px solid #ccc;color:#888">—</td></tr>'
    ref_rows=""
    for r in [x for x in ref_str.split("\n") if x.strip()]:
        p=[x.strip() for x in r.split("|")]
        ad=p[0] if len(p)>0 else "-"; mm=p[1] if len(p)>1 else "-"
        sn=(p[2] if len(p)>2 else "").replace("S/N:","").strip() or "-"
        srt=(p[3] if len(p)>3 else "").replace("Sert:","").strip() or "-"
        ref_rows+=f'<tr><td style="padding:3px 6px;border:1px solid #ccc">{ad}</td><td style="padding:3px 6px;border:1px solid #ccc">{mm}</td><td style="padding:3px 6px;border:1px solid #ccc">{sn}</td><td style="padding:3px 6px;border:1px solid #ccc">-</td><td style="padding:3px 6px;border:1px solid #ccc">{srt}</td><td style="padding:3px 6px;border:1px solid #ccc">{akred_no or "-"}</td></tr>'
    if not ref_rows: ref_rows='<tr><td colspan="6" style="padding:3px 6px;border:1px solid #ccc;color:#888">—</td></tr>'
    mc_ad=mc.get("ad","-"); mc_marka=mc.get("marka","-"); mc_model=mc.get("model","-")
    mc_seri=mc.get("seriNo","-"); mc_env=mc.get("envNo","-")
    mc_aralik=mc.get("olcumAraligi","-"); mc_yer=mc.get("kalYer","") or lab_adres or "-"
    firma_ad=firma.get("ad","-"); firma_adres=firma.get("adres","") or firma.get("il","-")
    fis_no=fis.get("fisNo","") or f"#{s.get('fisId','')}"
    kal_tarih=_fmt_tarih(kal.get("tarih","")); kal_sonraki=_fmt_tarih(kal.get("sonrakiTarih",""))
    fis_tarih=_fmt_tarih(fis.get("tarih",""))

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@page {{size: A4; margin: 12mm 14mm 10mm 14mm;}}
body {{font-family: Arial, sans-serif; font-size: 9pt; color: #111;}}
.page {{page-break-after: always;}}
.page-last {{page-break-after: avoid;}}
.hdr-top {{width:100%; margin-bottom:5px;}}
.hdr-top td {{vertical-align:middle; padding:0;}}
.hdr-center-name {{text-align:center; font-size:13pt; font-weight:700; margin-bottom:2px;}}
.hdr-center-addr {{text-align:center; font-size:8pt; color:#333; margin-bottom:4px;}}
.title-bar {{border-top:2pt solid #000; border-bottom:2pt solid #000; text-align:center; padding:3px 0; margin-bottom:5px;}}
.title-tr {{font-size:12pt; font-weight:700;}}
.title-en {{font-size:8pt; font-style:italic; color:#444;}}
.sert-row {{width:100%; margin-bottom:6px; font-size:8pt; font-weight:700;}}
.info-tbl {{width:100%; border-collapse:collapse; font-size:8pt; margin-bottom:6px; border:1pt solid #000;}}
.info-tbl td {{padding:3px 7px; vertical-align:top; line-height:1.35;}}
.info-tbl .lbl {{width:34%; color:#444;}}
.info-tbl .sep {{width:8px;}}
.info-tbl .val {{font-weight:600;}}
.lbl-small {{display:block; font-size:6.5pt; color:#777; font-style:italic;}}
.akred-text {{font-size:7.5pt; line-height:1.45; color:#222; margin-bottom:7px;}}
.sig-tbl {{width:100%; margin-top:auto; border-top:1pt solid #bbb; padding-top:8px; font-size:7.5pt; text-align:center;}}
.sig-tbl td {{text-align:center; vertical-align:top; padding:0 4px;}}
.sig-lbl {{font-size:7pt; color:#555; font-style:italic; margin-bottom:3px;}}
.sig-name {{font-weight:700; font-size:8pt; margin-top:3px;}}
.footer {{border-top:1pt solid #bbb; margin-top:5px; padding-top:3px; font-size:6.5pt; color:#555; width:100%;}}
.footer td {{font-size:6.5pt; color:#555; padding:0;}}
.pg-num {{text-align:center; font-size:6.5pt; color:#888; margin-top:2px;}}
.p2hdr {{width:100%; border-bottom:2pt solid #000; padding-bottom:5px; margin-bottom:7px;}}
.p2hdr td {{vertical-align:top; padding:0;}}
.kabul-bar {{width:100%; background:#f3f3f3; border:1pt solid #ccc; padding:3px 8px; font-size:7.5pt; margin-bottom:6px;}}
.sec-hdr {{background:#000; color:#fff; font-size:7.5pt; font-weight:700; padding:2px 8px; margin:0;}}
.sec-body {{border:1pt solid #000; border-top:none; padding:4px 8px; font-size:8pt; line-height:1.4;}}
.sec-italic {{font-size:7pt; color:#555; font-style:italic; margin-top:2px;}}
.dtbl {{width:100%; border-collapse:collapse; font-size:7.5pt;}}
.dtbl th {{background:#e0e0e0; padding:3px 6px; border:1px solid #bbb; font-weight:700; text-align:left;}}
.dtbl td {{padding:3px 6px; border:1px solid #bbb;}}
</style></head><body>

<div class="page">
<table class="hdr-top"><tr>
  <td style="width:130px">{logo_h}</td>
  <td style="text-align:center; font-size:13pt; font-weight:700;">{lab_ad}</td>
  <td style="width:130px; text-align:right">{turkak_h}</td>
</tr></table>
<div class="hdr-center-addr">{lab_adres}</div>
<div class="title-bar"><div class="title-tr">KALİBRASYON SERTİFİKASI</div><div class="title-en">Calibration Certificate</div></div>
<table class="sert-row"><tr><td>{sert_no}</td><td style="text-align:right">{akred_no}</td></tr></table>
<table class="info-tbl">
  <tr><td class="lbl">Cihazın Sahibi<span class="lbl-small">Customer</span></td><td class="sep">:</td><td class="val">{firma_ad}</td></tr>
  <tr><td class="lbl">Adres<span class="lbl-small">Address</span></td><td class="sep">:</td><td class="val">{firma_adres}</td></tr>
  <tr><td class="lbl">İstek Numarası<span class="lbl-small">Order No</span></td><td class="sep">:</td><td class="val">{fis_no}</td></tr>
  <tr><td class="lbl">Makine / Cihaz<span class="lbl-small">Instrument / Device</span></td><td class="sep">:</td><td class="val">{mc_ad}</td></tr>
  <tr><td class="lbl">İmalatçı<span class="lbl-small">Manufacturer</span></td><td class="sep">:</td><td class="val">{mc_marka}</td></tr>
  <tr><td class="lbl">Tip<span class="lbl-small">Type</span></td><td class="sep">:</td><td class="val">{mc_model}</td></tr>
  <tr><td class="lbl">Seri Numarası<span class="lbl-small">Serial Number</span></td><td class="sep">:</td><td class="val">{mc_seri}</td></tr>
  <tr><td class="lbl">Kalibrasyon Tarihi<span class="lbl-small">Date of Calibration</span></td><td class="sep">:</td><td class="val">{kal_tarih}</td></tr>
  <tr><td class="lbl">Sayfa Sayısı<span class="lbl-small">Number of Pages</span></td><td class="sep">:</td><td class="val">{toplam}</td></tr>
  <tr><td class="lbl">Demirbaş Numarası<span class="lbl-small">Device ID Number</span></td><td class="sep">:</td><td class="val">{mc_env}</td></tr>
</table>
<div class="akred-text">
  <p>Bu kalibrasyon sertifikası, Uluslararası Birimler Sisteminde (SI) tanımlanmış birimlerin ulusal ölçüm standartlarına izlenebilirliğini belgelemektedir.</p>
  <p><i>This calibration documents the traceability to national standards, which realize the unit of measurement according to the International System of Units (SI).</i></p>
  <p>{lab_ad}, Türk Akreditasyon Kurumu (TÜRKAK) tarafından TS EN ISO/IEC 17025:2017 standardına göre akredite edilmiştir. Akreditasyon dosya numarası: <b>{akred_no}</b></p>
</div>
<table class="sig-tbl"><tr>
  <td><div class="sig-lbl">Mühür / <i>Seal</i></div>{muhur_h}</td>
  <td><div class="sig-lbl">Yayımlandığı Tarih / <i>Date</i></div><div class="sig-name">{_fmt_tarih(yayim_tarih)}</div></td>
  <td><div class="sig-lbl">Kalibrasyonu Yapan / <i>Calibrated by</i></div><div style="font-size:6.5pt;color:#999;font-style:italic;margin:4px 0 1px">e-imzalıdır</div><div class="sig-name">{yapan or "-"}</div><div style="font-size:7.5pt;color:#333">{kal_tarih}</div></td>
  <td><div class="sig-lbl">Onaylayan / Tarih / <i>Approval / Date</i></div><div style="font-size:6.5pt;color:#999;font-style:italic;margin:4px 0 1px">e-imzalıdır</div><div class="sig-name">{onaylayan or "-"}</div><div style="font-size:7.5pt;color:#333">{_fmt_tarih(yayim_tarih)}</div></td>
</tr></table>
<table class="footer"><tr><td>{lab_ad} | Tel: {lab_tel} | {lab_adres}</td><td style="text-align:right">e-mail: {lab_mail}</td></tr></table>
<div class="pg-num">Sayfa 1 / {toplam}</div>
</div>

<div class="page-last">
<table class="p2hdr"><tr><td style="font-size:11pt;font-weight:700">{lab_ad}</td><td style="text-align:right;font-size:8pt;font-weight:700">{akred_no}<br>{sert_no}</td></tr></table>
<table class="kabul-bar"><tr><td><b>Kalibrasyona Kabul Tarihi :</b> {fis_tarih}</td><td style="text-align:right"><b>Gelecek Kalibrasyon Tarihi :</b> {kal_sonraki}</td></tr></table>

<div style="margin-bottom:5px"><p class="sec-hdr">1. KALİBRASYON YAPILAN CİHAZ BİLGİLERİ / CALIBRATED DEVICE INFORMATION</p>
<div style="border:1pt solid #000;border-top:none"><table class="dtbl">
  <tr><td style="width:22%;color:#444">Makine/Cihaz<br><small>Instrument/Device</small></td><td style="width:28%;font-weight:600">{mc_ad}</td><td style="width:22%;color:#444">Seri No<br><small>Serial Number</small></td><td style="font-weight:600">{mc_seri}</td></tr>
  <tr><td style="color:#444">İmalatçı<br><small>Manufacturer</small></td><td style="font-weight:600">{mc_marka}</td><td style="color:#444">Ölçüm Aralığı<br><small>Measurement Range</small></td><td style="font-weight:600">{mc_aralik}</td></tr>
  <tr><td style="color:#444">Tip<br><small>Type</small></td><td style="font-weight:600">{mc_model}</td><td style="color:#444">Kalibrasyon Yeri<br><small>Location</small></td><td style="font-weight:600">{mc_yer}</td></tr>
</table></div></div>

<div style="margin-bottom:5px"><p class="sec-hdr">2. ORTAM ŞARTLARI / ENVIRONMENT CONDITIONS</p><div class="sec-body">Sıcaklık : ({sicaklik or "-"}) °C &nbsp;&nbsp; Nem : ({nem_val or "-"}) %</div></div>
<div style="margin-bottom:5px"><p class="sec-hdr">3. ÖLÇÜM ŞARTLARI / MEASUREMENT CONDITIONS</p><div class="sec-body">Cihaz, kalibrasyon öncesi laboratuvarda bekletilerek ortam şartlarına uyum sağladıktan sonra ölçümler gerçekleştirilmiştir.<div class="sec-italic">Measurements have been carried out after the instruments were maintained in the suitable area.</div></div></div>
<div style="margin-bottom:5px"><p class="sec-hdr">4. ÖLÇÜM BELİRSİZLİĞİ / MEASUREMENT UNCERTAINTY</p><div class="sec-body">Beyan edilen genişletilmiş ölçüm belirsizliği k=2 kapsam faktörü ile yaklaşık %95 güvenilirlik seviyesini sağlamaktadır.<div class="sec-italic">The reported expanded uncertainty is stated as the standard uncertainty multiplied by k=2, corresponding to approximately 95% coverage probability.</div></div></div>

<div style="margin-bottom:5px"><p class="sec-hdr">5. KALİBRASYON PROSEDÜRLERİ / CALIBRATION PROCEDURES</p>
<div style="border:1pt solid #000;border-top:none"><table class="dtbl"><thead><tr><th>Prosedür No</th><th>Prosedür Adı</th></tr></thead><tbody>{pros_rows}</tbody></table></div></div>

<div style="margin-bottom:5px"><p class="sec-hdr">6. KALIBRASYONDA KULLANILAN REFERANSLAR / REFERENCES</p>
<div style="border:1pt solid #000;border-top:none"><table class="dtbl"><thead><tr><th>Demirbaş No</th><th>Cihaz/Marka/Model</th><th>Seri No</th><th>Gel. Kal. Tarihi</th><th>Sertifika No</th><th>İzlenebilir</th></tr></thead><tbody>{ref_rows}</tbody></table></div></div>

<div style="margin-bottom:5px"><p class="sec-hdr">7. AÇIKLAMALAR / REMARKS</p><div class="sec-body" style="min-height:20px">{aciklama or "Kalibrasyon sonuçları, kalibrasyon tarihinden itibaren geçerlidir."}<div style="margin-top:3px;font-weight:700">Bu Kalibrasyon Sertifikası, TS EN ISO/IEC 17025 standardında belirtilen yükümlülükler çerçevesinde tanzim edilmiştir.</div></div></div>
<div style="margin-bottom:5px"><p class="sec-hdr">8. UYGUNLUK DEĞERLENDİRME / CONFORMITY ASSESSMENT</p><div class="sec-body">Ölçüm sonuçları raporda belirtilmiş olup, değerlendirme kullanıcıya bırakılmıştır.</div></div>
<div style="margin-bottom:5px"><p class="sec-hdr">9. GÖRÜŞ VE YORUMLAR / OPINION AND COMMENTS</p><div class="sec-body" style="min-height:18px">{gorus or "—"}</div></div>

<div style="font-size:6.5pt;color:#444;text-align:center;border-top:1pt solid #bbb;padding-top:3px;margin-top:5px">Bu sertifika dijital imzalıdır. Laboratuvarın yazılı izni alınmadan kopyalanamaz.</div>
<table class="footer"><tr><td>{lab_ad} | Tel: {lab_tel} | {lab_adres}</td><td style="text-align:right">e-mail: {lab_mail}</td></tr></table>
<div class="pg-num">Sayfa 2 / {toplam}</div>
</div>

</body></html>"""

@api.get("/sertifika-pdf/{sert_id}")
async def sertifika_pdf_indir(sert_id: int):
    if not PDF_OK:
        raise HTTPException(503, "xhtml2pdf kurulu degil")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_state")
        state = {r["key"]: json.loads(r["value"]) for r in rows}
    except Exception as e:
        raise HTTPException(500, str(e))
    sertler = state.get("sertifikalar", [])
    s = next((x for x in sertler if x.get("id") == sert_id), None)
    if not s:
        raise HTTPException(404, f"Sertifika bulunamadi: {sert_id}")
    mc    = next((x for x in state.get("musteriCihazlari",[]) if x.get("id")==s.get("cihazId")), {})
    firma = next((x for x in state.get("firmalar",[])         if x.get("id")==s.get("firmaId")), {})
    fis   = next((x for x in state.get("cihazKabuller",[])    if x.get("id")==s.get("fisId")),   {})
    kal   = next((x for x in state.get("kalibrasyon",[])       if x.get("id")==s.get("kalId")),   {})
    det   = s.get("detay", {}); ay = state.get("labAyarlari", {})
    personeller = state.get("personeller", [])
    def pers_ad(pid):
        p = next((x for x in personeller if x.get("id")==pid), None)
        return f"{p.get('ad','')} {p.get('soyad','')}".strip() if p else ""
    methodlar=state.get("methodlar",[]); prosedurler=state.get("prosedurler",[]); refler=state.get("refCihazlar",[])
    method=next((x for x in methodlar if x.get("kod")==mc.get("methodKod","")), None)
    prosedur_str=""; ref_str=""
    if method:
        pros=[p for p in prosedurler if p.get("id") in method.get("prosedurIds",[])]
        prosedur_str=", ".join([f"{p.get('no','')} - {p.get('ad','')}" for p in pros])
        refs=[r for r in refler if r.get("id") in method.get("refCihazIds",[])]
        ref_str="\n".join([f"{r.get('no','-')} | {r.get('ad','-')} {r.get('marka','')} {r.get('model','')} | S/N:{r.get('seriNo','-')} | Sert:{r.get('sertNo','-')}" for r in refs])
    fv={"sertNo":s.get("no") or det.get("sertNo",""),"yayimTarih":det.get("yayimTarih",""),"sicaklik":det.get("sicaklik",""),"nem":det.get("nem",""),"aciklama":det.get("aciklama",""),"gorus":det.get("gorus",""),"prosedur":prosedur_str,"refCihazlar":ref_str,"yapan":pers_ad(kal.get("yapanId")),"onaylayan":pers_ad(kal.get("onaylayanId"))}
    html_str=_build_sertifika_html(s,mc,firma,fis,kal,det,ay,fv)
    pdf_bytes=_html_to_pdf(html_str)
    no_safe=(s.get("no") or f"SERT-{sert_id}").replace("/","-").replace(" ","_")
    filename=f"{sert_id}+{no_safe}.pdf"
    return FastAPIResponse(content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

class ImzaliPdfRequest(BaseModel):
    sert_id: int; dosya_adi: str; pdf_data: str; token: str

INTERNAL_TOKEN = "labqcert_internal_2026"

@api.post("/imzali-pdf-yukle")
async def imzali_pdf_yukle(req: ImzaliPdfRequest):
    if req.token != INTERNAL_TOKEN:
        raise HTTPException(401, "Gecersiz token")
    try:
        base64.b64decode(req.pdf_data)
    except Exception:
        raise HTTPException(400, "Gecersiz base64")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_state")
        state = {r["key"]: json.loads(r["value"]) for r in rows}
    except Exception as e:
        raise HTTPException(500, str(e))
    sertler=state.get("sertifikalar",[])
    s=next((x for x in sertler if x.get("id")==req.sert_id), None)
    if not s:
        raise HTTPException(404, f"Sertifika bulunamadi: {req.sert_id}")
    s["imzaliPdfData"]=req.pdf_data; s["imzaliPdfAdi"]=req.dosya_adi
    if s.get("durum")=="hazirlaniyor":
        s["durum"]="imzalandi"; s["imzalamaTarih"]=dt.date.today().isoformat(); yeni_durum="imzalandi"
    elif s.get("durum")=="imzalandi":
        s["durum"]="onaylandi"; s["onaylamaTarih"]=dt.date.today().isoformat(); yeni_durum="onaylandi"
    else:
        yeni_durum=s.get("durum")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO app_state (key, value, updated) VALUES ('sertifikalar',$1::jsonb,NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated=NOW()""", json.dumps(sertler))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "sert_id": req.sert_id, "yeni_durum": yeni_durum}

# ── APP ───────────────────────────────────────
app = FastAPI(title="LabQCert")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Önce API router'ı ekle
app.include_router(api)

# Sonra startup
@app.on_event("startup")
async def startup():
    if DATABASE_URL:
        await init_db()
    else:
        print("WARNING: DATABASE_URL yok")

# En sona static + catch_all
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/{path:path}")
def catch_all(path: str):
    return FileResponse("static/index.html")
