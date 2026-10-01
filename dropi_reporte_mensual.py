#!/usr/bin/env python3
"""
Descarga el reporte mensual de pedidos de Dropi
("Órdenes con Productos — un producto por fila") usando los endpoints
oficiales de la plataforma, sin entrar al navegador.

Endpoints usados (descubiertos del frontend app.dropi.co v3.4.0):
  - POST https://api-v2.dropi.co/bff/auth/core/login   (login + 2FA)
  - GET  https://reports.dropi.co/api/orders/exportexcel (crea el reporte)
  - GET  https://api.dropi.co/api/reports/index          (estado del reporte)
  - GET  https://reports.dropi.co/{file_path}{file_name} (descarga del xlsx)

Uso:
  python3 dropi_reporte_mensual.py                # mes actual
  python3 dropi_reporte_mensual.py --mes 2026-08  # mes específico
  python3 dropi_reporte_mensual.py --desde 2026-08-01 --hasta 2026-08-31

La primera vez pide usuario, contraseña y código 2FA; el token queda
guardado en .dropi_sesion.json para las siguientes corridas mientras
no expire (si expira, pide 2FA de nuevo).
"""

import argparse
import base64
import getpass
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BFF_LOGIN = "https://api-v2.dropi.co/bff/auth/core/login"
API = "https://api.dropi.co/api"
REPORTS_API = "https://reports.dropi.co/api"
REPORTS_DOWNLOAD = "https://reports.dropi.co"
CLOUDFRONT = "https://d1l4mzebo786pw.cloudfront.net"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CARPETA = Path(__file__).resolve().parent
ARCHIVO_SESION = CARPETA / ".dropi_sesion.json"
CARPETA_REPORTES = CARPETA / "reportes"


# ------------------------------------------------------------------ http --
def http(method, url, params=None, token=None, host=None, json_body=None,
         timeout=60):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    # api.dropi.co rechaza (403 Access denied) peticiones sin estas
    # cabeceras típicas de navegador; deben ir completas.
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Origin": "https://app.dropi.co",
        "Referer": "https://app.dropi.co/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = None
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["X-Host"] = host
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            if "json" in ctype:
                return r.status, json.loads(raw.decode())
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


# ----------------------------------------------------------------- login --
def exp_del_token(token):
    """Devuelve la fecha de expiración (epoch) de un JWT, o None."""
    try:
        parte = token.split(".")[1]
        parte += "=" * (-len(parte) % 4)
        datos = json.loads(base64.urlsafe_b64decode(parte))
        return datos.get("exp")
    except Exception:
        return None


def login(email, password, otp=None):
    """Login contra el BFF.

    Devuelve (sesion, necesita_2fa):
      - necesita_2fa=True  -> credenciales correctas pero falta el código.
      - sesion             -> dict con token/user_id/pais/exp.
    Lanza RuntimeError si hay un error real.
    """
    base = {"email": email, "password": password,
            "white_brand_id": 1, "with_cdc": False}
    status, resp = http("POST", BFF_LOGIN, json_body={**base, "otp": otp})
    if status != 200 or not isinstance(resp, dict):
        raise RuntimeError(f"Login falló (HTTP {status}): {str(resp)[:300]}")
    razon = resp.get("status_reason")
    if razon == "2fa":
        return None, True
    datos = resp.get("data") or {}
    if not datos.get("token"):
        raise RuntimeError("Login sin token. Respuesta: "
                           f"{json.dumps(resp)[:400]}")
    objetos = datos.get("objects") or {}
    paises = datos.get("countries") or []
    pais = (paises[0].get("code") or "co").lower() if paises else "co"
    ses = {
        "token": datos["token"],
        "user_id": objetos.get("id"),
        "pais": pais,
        "exp": exp_del_token(datos["token"]),
    }
    return ses, False


def cargar_sesion():
    if not ARCHIVO_SESION.exists():
        return None
    try:
        ses = json.loads(ARCHIVO_SESION.read_text())
        exp = ses.get("exp")
        if exp and time.time() > exp - 300:
            return None
        # verificar que el token siga vivo
        st, _ = http("GET", f"{API}/reports/index",
                     params={"startData": 0, "pageSize": 1, "page": 1,
                             "search": "", "status": "ALL"},
                     token=ses["token"], host=ses.get("pais"))
        return ses if st == 200 else None
    except Exception:
        return None


def obtener_sesion():
    ses = cargar_sesion()
    if ses:
        print("Sesión guardada válida (no hace falta 2FA).")
        return ses
    email = input("Usuario (correo Dropi): ").strip()
    password = getpass.getpass("Contraseña: ")
    ses, necesita = login(email, password)
    if necesita:
        codigo = input("Código de tu app de autenticación (6 dígitos): ").strip()
        ses, necesita = login(email, password, codigo)
        if necesita:
            raise SystemExit("El código de dos factores no fue aceptado; "
                             "inténtalo de nuevo con un código nuevo.")
    ARCHIVO_SESION.write_text(json.dumps(ses))
    return ses


# --------------------------------------------------------------- reporte --
def crear_reporte(ses, desde, hasta):
    params = [
        ("orderBy", "id"), ("orderDirection", "desc"),
        ("result_number", 1000), ("start", 0),
        ("textToSearch", ""), ("status", "null"),
        ("supplier_id", "false"), ("user_id", ses["user_id"]),
        ("from", desde), ("until", hasta),
        ("token", ses["token"]),
        ("haveIncidenceProcesamiento", "false"),
        ("warranty", "false"), ("filter_date_by", ""), ("invoiced", "null"),
        ("exportAs", "productsbyRow"),
    ]
    st, resp = http("GET", f"{REPORTS_API}/orders/exportexcel",
                    params=params, token=ses["token"], host=ses.get("pais"))
    if st != 200 or not (isinstance(resp, dict) and resp.get("isSuccess")):
        raise SystemExit(f"No se pudo crear el reporte (HTTP {st}): "
                         f"{str(resp)[:400]}")
    print(f"Reporte solicitado: {resp.get('message', 'en cola')}")


def listar_reportes(ses):
    st, resp = http("GET", f"{API}/reports/index",
                    params={"startData": 0, "pageSize": 30, "page": 1,
                            "search": "", "status": "ALL"},
                    token=ses["token"], host=ses.get("pais"))
    if st != 200 or not isinstance(resp, dict):
        raise SystemExit(f"No se pudo listar reportes (HTTP {st}): "
                         f"{str(resp)[:300]}")
    return resp.get("objects") or []


def esperar_reporte(ses, ids_previos, espera_max=300):
    print("Esperando a que Dropi genere el archivo…")
    inicio = time.time()
    while time.time() - inicio < espera_max:
        time.sleep(10)
        for rep in listar_reportes(ses):
            if rep.get("id") in ids_previos:
                continue
            if "productsByRow" not in (rep.get("report_name") or ""):
                continue
            if rep.get("error"):
                raise SystemExit(f"El reporte falló: {rep.get('error')}")
            if rep.get("ready") and (rep.get("ready_records") or 0) > 0:
                return rep
            if rep.get("ready") and (rep.get("ready_records") or 0) == 0:
                raise SystemExit("El reporte quedó listo pero sin filas: "
                                 "revisa el rango de fechas.")
    raise SystemExit("El reporte no estuvo listo en 5 minutos; entra a "
                     "Reportes → Descargas en la web para verlo.")


def descargar(ses, rep):
    # storage_type "s3" se sirve por CloudFront; el resto por reports.dropi.co
    base = CLOUDFRONT if rep.get("storage_type") == "s3" else REPORTS_DOWNLOAD
    url = f"{base}/{rep.get('file_path','')}{rep.get('file_name','')}"
    st, contenido = http("GET", url, token=ses["token"])
    if st != 200 or not isinstance(contenido, bytes):
        raise SystemExit(f"Descarga falló (HTTP {st}): {str(contenido)[:300]}")
    CARPETA_REPORTES.mkdir(exist_ok=True)
    destino = CARPETA_REPORTES / (rep.get("display_name") or rep["file_name"])
    destino.write_bytes(contenido)
    return destino


# ------------------------------------------------------------------ main --
def rango_mes(anio, mes):
    import calendar
    ultimo = calendar.monthrange(anio, mes)[1]
    return f"{anio:04d}-{mes:02d}-01", f"{anio:04d}-{mes:02d}-{ultimo:02d}"


def main():
    ap = argparse.ArgumentParser(
        description="Descarga el reporte mensual de pedidos de Dropi "
                    "(un producto por fila).")
    ap.add_argument("--mes", metavar="AAAA-MM",
                    help="mes del reporte (por defecto: mes actual)")
    ap.add_argument("--desde", help="fecha inicial AAAA-MM-DD")
    ap.add_argument("--hasta", help="fecha final AAAA-MM-DD")
    args = ap.parse_args()

    if args.desde and args.hasta:
        desde, hasta = args.desde, args.hasta
    else:
        hoy = date.today()
        anio, mes = (hoy.year, hoy.month)
        if args.mes:
            anio, mes = map(int, args.mes.split("-"))
        desde, hasta = rango_mes(anio, mes)

    print(f"Reporte de pedidos del {desde} al {hasta} (un producto por fila)")
    ses = obtener_sesion()
    ids_previos = {r.get("id") for r in listar_reportes(ses)}
    crear_reporte(ses, desde, hasta)
    rep = esperar_reporte(ses, ids_previos)
    destino = descargar(ses, rep)
    print(f"\n✔ Listo: {destino}")
    print(f"  Filas procesadas: {rep.get('ready_records')}")


if __name__ == "__main__":
    main()
