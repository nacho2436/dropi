#!/usr/bin/env python3
"""
Servidor web local para descargar reportes de Dropi con una página amigable.

Abre http://127.0.0.1:8765 en tu navegador (se abre solo al arrancar).
Desde ahí puedes poner el rango de fechas y, si la sesión expiró,
ingresar usuario, contraseña y el código de dos factores.

Uso:
  python3 servidor_dropi.py            # abre el navegador solo
  python3 servidor_dropi.py --puerto 9000
  python3 servidor_dropi.py --sin-abrir
  python3 servidor_dropi.py --lan      # accesible desde otros equipos de la red

Por defecto solo escucha en 127.0.0.1 (tu equipo). Con --lan escucha en
0.0.0.0: cualquier equipo de tu red local puede abrir la página con
http://<IP-de-este-equipo>:<puerto>.
"""

import argparse
import json
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import dropi_reporte_mensual as dropi

CARPETA = Path(__file__).resolve().parent
INDEX = CARPETA / "index.html"
CLOUDFRONT_IMG = "https://d39ru7awumhhs2.cloudfront.net/"
DB = CARPETA / "productos.sqlite"
CARPETA_IMAGENES = CARPETA / "productos_images"
ARCHIVO_CONFIG_IA = CARPETA / ".dropi_config.json"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
# Los planes GLM Coding (Lite/Pro/Max) usan este endpoint propio:
ZAI_URL_CODING = "https://api.z.ai/api/coding/paas/v4/chat/completions"
MODELO_DEFECTO = "glm-4.6"
NIVELES_PENSAMIENTO = {"high", "max", "low", "off"}

PLANTILLA_DEFECTO = (
    "Eres un experto en copywriting para venta por WhatsApp en Colombia.\n"
    "Basado en la información del producto que deseo vender en Colombia,\n"
    "teniendo en cuenta que cuento con estos beneficios:\n"
    "- Pago contra entrega\n"
    "- Envío gratis\n"
    "- Ofertas por cantidad\n"
    "\n"
    "OBJETIVO: crea una descripción completa para que mi chatbot que vende\n"
    "por WhatsApp tenga la capacidad de aclarar todas las dudas posibles\n"
    "de este producto (qué es, para quién es, cómo se usa, qué contiene,\n"
    "beneficios, dudas frecuentes).\n"
    "\n"
    "IMPORTANTE:\n"
    "- Ignora precio y garantía: esa información ya la sabe el bot.\n"
    "- Si crees que me hace falta información del producto para venderlo\n"
    "  mejor, dime exactamente cuál, para que yo la busque y te la dé.\n"
    "- Usa un tono cercano y claro, sin exagerar.\n"
    "- Entrega el texto listo para pegar en el chatbot."
)


# ------------------------------------------------------- config IA (Z.ai) --
def _normalizar_nivel(valor: str) -> str:
    """Unifica niveles viejos (enabled/disabled) y nuevos (high/max/low/off)."""
    v = (valor or "").strip().lower()
    if v in ("enabled", ""):
        return "high"
    if v == "disabled":
        return "off"
    return v if v in NIVELES_PENSAMIENTO else "high"


def leer_config_ia() -> dict:
    try:
        cfg = json.loads(ARCHIVO_CONFIG_IA.read_text())
    except Exception:
        cfg = {}
    return {
        "key": cfg.get("key") or "",
        "modelo": cfg.get("modelo") or MODELO_DEFECTO,
        "pensamiento": _normalizar_nivel(cfg.get("pensamiento")),
        "plan": cfg.get("plan") or "coding",
        "plantilla": cfg.get("plantilla") or PLANTILLA_DEFECTO,
        "key_openai": cfg.get("key_openai") or "",
        "modelo_imagen": cfg.get("modelo_imagen") or "gpt-image-1",
    }


def guardar_config_ia(nueva: dict):
    cfg = leer_config_ia()
    if nueva.get("key"):
        cfg["key"] = nueva["key"].strip()
    if nueva.get("modelo"):
        cfg["modelo"] = nueva["modelo"].strip()
    if nueva.get("pensamiento"):
        cfg["pensamiento"] = _normalizar_nivel(nueva["pensamiento"])
    if nueva.get("plan") in ("coding", "normal"):
        cfg["plan"] = nueva["plan"]
    if nueva.get("key_openai"):
        cfg["key_openai"] = nueva["key_openai"].strip()
    if nueva.get("modelo_imagen"):
        cfg["modelo_imagen"] = nueva["modelo_imagen"].strip()
    if nueva.get("plantilla") is not None:
        cfg["plantilla"] = nueva["plantilla"]
    ARCHIVO_CONFIG_IA.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


# ------------------------------------------------------------ base datos --
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS productos (
                id            INTEGER PRIMARY KEY,
                nombre        TEXT,
                descripcion   TEXT,
                sku           TEXT,
                precio        TEXT,
                activo        INTEGER,
                categorias    TEXT,
                fotos         TEXT,
                guardado_en   TEXT,
                actualizado_en TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS descripciones (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                producto_id  INTEGER NOT NULL,
                texto        TEXT,
                creada_en    TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS plantillas (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre        TEXT UNIQUE,
                texto         TEXT,
                creada_en     TEXT,
                actualizada_en TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS modulos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                producto_id  INTEGER NOT NULL,
                nombre       TEXT NOT NULL,
                contenido    TEXT,
                orden        INTEGER DEFAULT 0,
                creada_en    TEXT
            )
        """)
        # migraciones para bases creadas antes de estas funciones
        for sql in ("ALTER TABLE productos ADD COLUMN proveedor TEXT",
                    "ALTER TABLE productos ADD COLUMN proveedor_id INTEGER",
                    "ALTER TABLE plantillas ADD COLUMN tipo TEXT DEFAULT 'texto'"):
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass  # la columna ya existe
        # sembrar la plantilla por defecto la primera vez
        hay = con.execute("SELECT COUNT(*) c FROM plantillas").fetchone()["c"]
        if not hay:
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
            con.execute("INSERT INTO plantillas (nombre, texto, creada_en, "
                        "actualizada_en, tipo) VALUES (?,?,?,?,?)",
                        ("Descripción chatbot WhatsApp", PLANTILLA_DEFECTO,
                         ahora, ahora, "texto"))


init_db()


# ------------------------------------------------------------ plantillas --
def listar_plantillas():
    with db() as con:
        filas = con.execute(
            "SELECT * FROM plantillas ORDER BY actualizada_en DESC").fetchall()
    return [{"id": f["id"], "nombre": f["nombre"], "texto": f["texto"],
             "creada_en": f["creada_en"], "actualizada_en": f["actualizada_en"],
             "tipo": f["tipo"] or "texto"}
            for f in filas]


def guardar_plantilla(pid, nombre, texto, tipo="texto"):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    tipo = "imagen" if tipo == "imagen" else "texto"
    with db() as con:
        if pid:
            con.execute("UPDATE plantillas SET nombre=?, texto=?, tipo=?, "
                        "actualizada_en=? WHERE id=?",
                        (nombre, texto, tipo, ahora, pid))
        else:
            cur = con.execute(
                "INSERT INTO plantillas (nombre, texto, creada_en, "
                "actualizada_en, tipo) VALUES (?,?,?,?,?)",
                (nombre, texto, ahora, ahora, tipo))
            pid = cur.lastrowid
    return pid


def eliminar_plantilla(pid):
    with db() as con:
        con.execute("DELETE FROM plantillas WHERE id=?", (pid,))


# --------------------------------------------------------------- módulos --
def listar_modulos(producto_id: int):
    with db() as con:
        filas = con.execute(
            "SELECT * FROM modulos WHERE producto_id=? ORDER BY orden, id",
            (producto_id,)).fetchall()
    return [{"id": f["id"], "producto_id": f["producto_id"],
             "nombre": f["nombre"], "contenido": f["contenido"] or ""}
            for f in filas]


def guardar_modulo(mid, producto_id, nombre, contenido):
    with db() as con:
        existe = con.execute("SELECT 1 FROM productos WHERE id=?",
                             (producto_id,)).fetchone()
        if not existe:
            raise RuntimeError("Ese producto no está guardado en la base.")
        if mid:
            con.execute("UPDATE modulos SET nombre=?, contenido=? WHERE id=?",
                        (nombre, contenido, mid))
        else:
            con.execute("INSERT INTO modulos (producto_id, nombre, contenido, "
                        "creada_en) VALUES (?,?,?,?)",
                        (producto_id, nombre, contenido,
                         datetime.now().strftime("%Y-%m-%d %H:%M")))


def eliminar_modulo(mid):
    with db() as con:
        con.execute("DELETE FROM modulos WHERE id=?", (mid,))


# ---------------------------------------------------------- descripciones --
def listar_descripciones(producto_id: int):
    with db() as con:
        filas = con.execute(
            "SELECT * FROM descripciones WHERE producto_id=? ORDER BY id DESC",
            (producto_id,)).fetchall()
    return [{"id": f["id"], "producto_id": f["producto_id"],
             "texto": f["texto"], "creada_en": f["creada_en"]} for f in filas]


def guardar_descripcion(producto_id: int, texto: str):
    with db() as con:
        existe = con.execute("SELECT 1 FROM productos WHERE id=?",
                             (producto_id,)).fetchone()
        if not existe:
            raise RuntimeError("Ese producto no está guardado en la base.")
        con.execute("INSERT INTO descripciones (producto_id, texto, creada_en) "
                    "VALUES (?,?,?)",
                    (producto_id, texto,
                     datetime.now().strftime("%Y-%m-%d %H:%M")))


def eliminar_descripcion(did: int):
    with db() as con:
        con.execute("DELETE FROM descripciones WHERE id=?", (did,))


def editar_descripcion_producto(producto_id: int, descripcion: str):
    with db() as con:
        con.execute("UPDATE productos SET descripcion=?, actualizado_en=? "
                    "WHERE id=?", (descripcion,
                                   datetime.now().strftime("%Y-%m-%d %H:%M"),
                                   producto_id))


def guardar_producto(p: dict):
    """Guarda (o actualiza) un producto y descarga sus imágenes."""
    pid = int(p["id"])
    CARPETA_IMAGENES.mkdir(exist_ok=True)
    dir_prod = CARPETA_IMAGENES / str(pid)
    dir_prod.mkdir(exist_ok=True)

    fotos = []
    for i, f in enumerate(p.get("fotos") or [], start=1):
        entrada = {"url": f.get("url"), "main": bool(f.get("main")), "local": None}
        try:
            st, datos = dropi.http("GET", f["url"])
            if st == 200 and isinstance(datos, bytes) and datos[:100].strip():
                ext = Path(unquote(urlparse(f["url"]).path)).suffix or ".png"
                destino = dir_prod / f"{i:02d}{ext}"
                destino.write_bytes(datos)
                entrada["local"] = f"/productos_img/{pid}/{destino.name}"
        except Exception:
            pass  # si falla la descarga, queda solo la URL remota
        fotos.append(entrada)

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db() as con:
        previo = con.execute("SELECT guardado_en FROM productos WHERE id=?",
                             (pid,)).fetchone()
        con.execute("""
            INSERT INTO productos
                (id, nombre, descripcion, sku, precio, activo, categorias,
                 fotos, guardado_en, actualizado_en, proveedor, proveedor_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                nombre=excluded.nombre, descripcion=excluded.descripcion,
                sku=excluded.sku, precio=excluded.precio, activo=excluded.activo,
                categorias=excluded.categorias, fotos=excluded.fotos,
                actualizado_en=excluded.actualizado_en,
                proveedor=excluded.proveedor, proveedor_id=excluded.proveedor_id
        """, (pid, p.get("nombre") or "", p.get("descripcion") or "",
              p.get("sku") or "", str(p.get("precio") or ""),
              1 if p.get("activo") else 0,
              json.dumps(p.get("categorias") or [], ensure_ascii=False),
              json.dumps(fotos, ensure_ascii=False),
              previo["guardado_en"] if previo else ahora, ahora,
              p.get("proveedor") or "", p.get("proveedor_id")))


def listar_productos():
    with db() as con:
        filas = con.execute(
            "SELECT * FROM productos ORDER BY actualizado_en DESC").fetchall()
        modulos_por_producto = {}
        for m in con.execute(
                "SELECT * FROM modulos ORDER BY orden, id").fetchall():
            modulos_por_producto.setdefault(m["producto_id"], []).append(
                {"id": m["id"], "nombre": m["nombre"],
                 "contenido": m["contenido"] or ""})
    out = []
    for f in filas:
        out.append({
            "id": f["id"], "nombre": f["nombre"],
            "descripcion": f["descripcion"], "sku": f["sku"],
            "precio": f["precio"], "activo": bool(f["activo"]),
            "categorias": json.loads(f["categorias"] or "[]"),
            "fotos": json.loads(f["fotos"] or "[]"),
            "guardado_en": f["guardado_en"],
            "actualizado_en": f["actualizado_en"],
            "proveedor": f["proveedor"] or "",
            "proveedor_id": f["proveedor_id"],
            "modulos": modulos_por_producto.get(f["id"], []),
        })
    return out


def eliminar_producto(pid: int):
    with db() as con:
        con.execute("DELETE FROM productos WHERE id=?", (pid,))
        con.execute("DELETE FROM descripciones WHERE producto_id=?", (pid,))
        con.execute("DELETE FROM modulos WHERE producto_id=?", (pid,))
    dir_prod = CARPETA_IMAGENES / str(pid)
    if dir_prod.exists():
        shutil.rmtree(dir_prod, ignore_errors=True)


# ------------------------------------------------------------ Z.ai (GLM) --
def zai_generar(key: str, modelo: str, plantilla: str, producto: dict,
                pensamiento: str = "high", plan: str = "coding",
                extra: str = ""):
    """Llama a la API de Z.ai y devuelve el texto generado."""
    url = ZAI_URL_CODING if plan == "coding" else ZAI_URL
    datos_producto = (
        f"PRODUCTO ID: {producto['id']}\n"
        f"NOMBRE: {producto['nombre']}\n"
        f"CATEGORÍAS: {', '.join(producto.get('categorias') or []) or '(ninguna)'}\n"
        f"ESTADO: {'Activo' if producto.get('activo') else 'Inactivo'}\n"
        f"DESCRIPCIÓN ORIGINAL DEL PROVEEDOR:\n"
        f"{producto.get('descripcion') or '(sin descripción)'}"
    )
    if extra:
        datos_producto += "\n\nINFORMACIÓN ADICIONAL GUARDADA POR EL VENDEDOR:\n" + extra
    cuerpo = {
        "model": modelo,
        "messages": [
            {"role": "system", "content": plantilla},
            {"role": "user", "content": datos_producto},
        ],
        "temperature": 0.7,
    }
    # GLM-5.x controla el razonamiento con reasoning_effort (low/high/max)
    # y no se puede apagar; GLM-4.x usa thinking.type (enabled/disabled).
    if modelo.startswith("glm-5"):
        esfuerzo = {"high": "high", "max": "max", "low": "low",
                    "off": "low"}[_normalizar_nivel(pensamiento)]
        cuerpo["reasoning_effort"] = esfuerzo
    else:
        activo = _normalizar_nivel(pensamiento) not in ("off", "low")
        cuerpo["thinking"] = {"type": "enabled" if activo else "disabled"}
    req = urllib.request.Request(
        url, data=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.loads(r.read().decode())
    try:
        mensaje = resp["choices"][0]["message"]
        texto = mensaje.get("content") or mensaje.get("reasoning_content") or ""
    except Exception:
        raise RuntimeError("Respuesta inesperada de Z.ai: "
                           + json.dumps(resp)[:300])
    if not texto.strip():
        raise RuntimeError("Z.ai devolvió una respuesta vacía.")
    return texto.strip()


OPENAI_URL = "https://api.openai.com/v1/images/generations"
OPENAI_EDITS = "https://api.openai.com/v1/images/edits"


def _multipart(campos: dict, archivo: tuple):
    """Construye un cuerpo multipart/form-data (campo_imagen, nombre, bytes)."""
    limite = "----dropi" + uuid.uuid4().hex
    partes = []
    for k, v in campos.items():
        partes.append(
            f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\""
            f"\r\n\r\n{v}\r\n".encode())
    campo, nombre, contenido = archivo
    partes.append(
        f"--{limite}\r\nContent-Disposition: form-data; name=\"{campo}\"; "
        f"filename=\"{nombre}\"\r\nContent-Type: image/png\r\n\r\n".encode()
        + contenido + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={limite}"


def _html_a_texto(html: str, limite: int = 1200) -> str:
    import re as _re
    texto = _re.sub(r"<[^>]+>", " ", html or " ")
    return " ".join(texto.split())[:limite]


def openai_generar_imagen(key: str, modelo: str, prompt: str, producto_id,
                           ruta_referencia: str = ""):
    """Genera una imagen con OpenAI y la guarda junto al producto.

    Si se indica ruta_referencia (una imagen local del producto) se usa el
    endpoint de ediciones para que el modelo la tome como referencia.
    """
    ruta_ref = ""
    if ruta_referencia and ruta_referencia.startswith("/productos_img/"):
        candidata = (CARPETA_IMAGENES
                     / ruta_referencia[len("/productos_img/"):]).resolve()
        if (str(candidata).startswith(str(CARPETA_IMAGENES.resolve()))
                and candidata.exists()):
            ruta_ref = str(candidata)

    if ruta_ref:
        cuerpo, ctype = _multipart(
            {"model": modelo, "prompt": prompt[:3800], "n": "1",
             "size": "1024x1024"},
            ("image", Path(ruta_ref).name, Path(ruta_ref).read_bytes()))
        req = urllib.request.Request(
            OPENAI_EDITS, data=cuerpo,
            headers={"Content-Type": ctype,
                     "Authorization": f"Bearer {key}"},
            method="POST")
    else:
        cuerpo = {"model": modelo, "prompt": prompt[:3800], "n": 1,
                  "size": "1024x1024"}
        req = urllib.request.Request(
            OPENAI_URL, data=json.dumps(cuerpo).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read().decode())
    try:
        dato = resp["data"][0]
    except Exception:
        raise RuntimeError("Respuesta inesperada de OpenAI: "
                           + json.dumps(resp)[:300])
    CARPETA_IMAGENES.mkdir(exist_ok=True)
    dir_prod = CARPETA_IMAGENES / str(producto_id)
    dir_prod.mkdir(exist_ok=True)
    destino = dir_prod / f"gen_{int(time.time())}.png"
    if dato.get("b64_json"):
        import base64
        destino.write_bytes(base64.b64decode(dato["b64_json"]))
    elif dato.get("url"):
        with urllib.request.urlopen(dato["url"], timeout=120) as r:
            destino.write_bytes(r.read())
    else:
        raise RuntimeError("OpenAI no devolvió imagen.")
    return f"/productos_img/{producto_id}/{destino.name}"


def agregar_imagen_producto(producto_id: int, nombre: str, data_b64: str):
    """Guarda una imagen subida por el usuario y la agrega a la galería."""
    import base64
    try:
        contenido = base64.b64decode(data_b64.split(",")[-1])
    except Exception:
        raise RuntimeError("La imagen enviada no es válida.")
    if len(contenido) > 25 * 1024 * 1024:
        raise RuntimeError("La imagen supera 25 MB.")
    ext = ".png"
    if nombre.lower().endswith((".jpg", ".jpeg")):
        ext = ".jpg"
    elif nombre.lower().endswith(".webp"):
        ext = ".webp"
    CARPETA_IMAGENES.mkdir(exist_ok=True)
    dir_prod = CARPETA_IMAGENES / str(producto_id)
    dir_prod.mkdir(exist_ok=True)
    destino = dir_prod / f"agg_{int(time.time() * 1000) % 10**10}{ext}"
    destino.write_bytes(contenido)
    ruta = f"/productos_img/{producto_id}/{destino.name}"
    agregar_foto_producto(producto_id, ruta)
    return ruta


def eliminar_imagen_producto(producto_id: int, ruta: str):
    """Quita una imagen de la galería y borra el archivo si es local nuestro."""
    with db() as con:
        fila = con.execute("SELECT fotos FROM productos WHERE id=?",
                           (producto_id,)).fetchone()
        if not fila:
            raise RuntimeError("Ese producto no está guardado.")
        fotos = json.loads(fila["fotos"] or "[]")
        fotos = [f for f in fotos
                 if f.get("local") != ruta and f.get("url") != ruta]
        if len(fotos) == 0:
            raise RuntimeError("El producto debe conservar al menos una imagen.")
        con.execute("UPDATE productos SET fotos=? WHERE id=?",
                    (json.dumps(fotos, ensure_ascii=False), producto_id))
    if ruta.startswith("/productos_img/"):
        archivo = (CARPETA_IMAGENES
                   / ruta[len("/productos_img/"):]).resolve()
        if (str(archivo).startswith(str(CARPETA_IMAGENES.resolve()))
                and archivo.exists()
                and archivo.name.startswith(("agg_", "gen_"))):
            archivo.unlink()  # solo borramos del disco las que creamos nosotros


def agregar_foto_producto(producto_id: int, ruta: str):
    """Agrega una imagen (p. ej. generada) a la galería del producto."""
    if not ruta.startswith("/productos_img/"):
        raise RuntimeError("Ruta de imagen no válida.")
    with db() as con:
        fila = con.execute("SELECT fotos FROM productos WHERE id=?",
                           (producto_id,)).fetchone()
        if not fila:
            raise RuntimeError("Ese producto no está guardado.")
        fotos = json.loads(fila["fotos"] or "[]")
        if not any(f.get("local") == ruta or f.get("url") == ruta
                   for f in fotos):
            fotos.append({"url": ruta, "local": ruta, "main": False,
                          "generada": True})
        con.execute("UPDATE productos SET fotos=? WHERE id=?",
                    (json.dumps(fotos, ensure_ascii=False), producto_id))

# jobs de generación de reportes: id -> estado
JOBS: dict = {}
LOCK = threading.Lock()


# ------------------------------------------------------------- trabajos --
def _contar_filas_xlsx(ruta: Path) -> int:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(ruta, read_only=True)
        return sum(1 for _ in wb.active.iter_rows()) - 1
    except Exception:
        return -1


def correr_job_cartera(job_id: str, ses: dict, desde: str, hasta: str):
    """La cartera se descarga directo (sin cola de reportes)."""
    job = JOBS[job_id]

    def log(msg: str):
        with LOCK:
            job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg})

    try:
        log(f"Descargando historial de cartera del {desde} al {hasta}…")
        params = [
            ("orderBy", "id"), ("orderDirection", "desc"),
            ("result_number", 5000), ("start", 0), ("textToSearch", ""),
            ("user_id", ses["user_id"]), ("type", "null"),
            ("from", desde), ("until", hasta), ("token", ses["token"]),
        ]
        st, contenido = dropi.http("GET", f"{dropi.API}/wallet/exportexcel",
                                   params=params, token=ses["token"])
        if st != 200 or not isinstance(contenido, bytes):
            raise RuntimeError(f"La descarga falló (HTTP {st}): "
                               f"{str(contenido)[:300]}")
        dropi.CARPETA_REPORTES.mkdir(exist_ok=True)
        destino = dropi.CARPETA_REPORTES / f"cartera_{desde}_{hasta}.xlsx"
        destino.write_bytes(contenido)
        filas = _contar_filas_xlsx(destino)
        if filas == 0:
            raise RuntimeError("El archivo vino vacío: no hay movimientos en "
                               "ese rango de fechas.")
        with LOCK:
            job["archivo"] = destino.name
            job["estado"] = "listo"
        log(f"¡Listo! {filas} movimientos guardados." if filas > 0
            else "¡Descarga completada!")
    except (Exception, SystemExit) as e:
        msg = str(e) or e.__class__.__name__
        log(f"Error: {msg}")
        with LOCK:
            job["estado"] = "error"
            job["error"] = msg


def correr_job_imagen(job_id: str, datos: dict, producto: dict):
    """Genera una imagen con OpenAI como trabajo en segundo plano."""
    job = JOBS[job_id]

    def log(msg: str):
        with LOCK:
            job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg})

    try:
        log("Enviando a OpenAI la plantilla con la imagen de referencia…")
        cfg_img = leer_config_ia()
        resumen = (f"Producto: {producto['nombre']}\n"
                   f"Categorías: {', '.join(producto.get('categorias') or [])}\n"
                   f"Descripción: {_html_a_texto(producto.get('descripcion'))}")
        for m in (producto.get("modulos") or []):
            resumen += f"\n[Módulo {m['nombre']}]: {_html_a_texto(m['contenido'], 500)}"
        prompt_img = ((datos.get("prompt") or "").strip()
                      + "\n\nContexto del producto:\n" + resumen)
        log("OpenAI está pintando la imagen (suele tardar 1-2 minutos)…")
        ruta = openai_generar_imagen(
            cfg_img["key_openai"], cfg_img["modelo_imagen"], prompt_img,
            producto["id"], datos.get("referencia") or "")
        with LOCK:
            job["imagen"] = ruta
            job["estado"] = "listo"
        log("¡Imagen lista!")
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        with LOCK:
            job["estado"] = "error"
            job["error"] = f"OpenAI respondió HTTP {e.code}: {detalle}"
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        with LOCK:
            job["estado"] = "error"
            job["error"] = msg
        log("Error: " + msg)


def correr_job(job_id: str, ses: dict, desde: str, hasta: str):
    job = JOBS[job_id]

    def log(msg: str):
        with LOCK:
            job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg})

    try:
        log(f"Solicitando a Dropi el reporte del {desde} al {hasta}…")
        ids_previos = {r.get("id") for r in dropi.listar_reportes(ses)}
        dropi.crear_reporte(ses, desde, hasta)
        log("Dropi está generando el archivo (un producto por fila)…")
        inicio = time.time()
        while time.time() - inicio < 300:
            time.sleep(8)
            for rep in dropi.listar_reportes(ses):
                if rep.get("id") in ids_previos:
                    continue
                if "productsByRow" not in (rep.get("report_name") or ""):
                    continue
                if rep.get("error"):
                    raise RuntimeError("El reporte falló al generarse en Dropi.")
                if rep.get("ready"):
                    filas = rep.get("ready_records") or 0
                    if filas == 0:
                        raise RuntimeError(
                            "El reporte quedó listo pero sin filas: "
                            "revisa el rango de fechas.")
                    log(f"Reporte listo ({filas} filas). Descargando…")
                    destino = dropi.descargar(ses, rep)
                    with LOCK:
                        job["archivo"] = destino.name
                        job["estado"] = "listo"
                    log("¡Descarga completada!")
                    return
        raise RuntimeError("El reporte no estuvo listo en 5 minutos; "
                           "revisa Reportes → Descargas en la web de Dropi.")
    except (Exception, SystemExit) as e:  # dropi.* usa SystemExit para errores
        msg = str(e) or e.__class__.__name__
        log(f"Error: {msg}")
        with LOCK:
            job["estado"] = "error"
            job["error"] = msg


# -------------------------------------------------------------- servidor --
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # silenciar access-log
        pass

    # ---------- respuestas ----------
    def _json(self, codigo: int, datos: dict):
        body = json.dumps(datos).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _leer_json(self) -> dict:
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(largo) or b"{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        ruta = urlparse(self.path).path

        if ruta in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if ruta == "/api/estado":
            ses = dropi.cargar_sesion()
            if ses:
                expira = ses.get("exp")
                restante = max(0, int(((expira or 0) - time.time()) / 60)) if expira else None
                self._json(200, {"sesion": True, "user_id": ses.get("user_id"),
                                 "minutos_restantes": restante})
            else:
                self._json(200, {"sesion": False})
            return

        if ruta == "/api/producto":
            pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0].strip()
            if not pid.isdigit():
                self._json(400, {"ok": False, "error": "Escribe un ID de producto numérico."})
                return
            ses = dropi.cargar_sesion()
            if not ses:
                self._json(200, {"ok": False, "sesion_expirada": True})
                return
            st, resp = dropi.http("GET",
                                  f"{dropi.API}/products/productlist/v1/show/",
                                  params=[("id", pid)],
                                  token=ses["token"], host=ses.get("pais"))
            if st != 200 or not isinstance(resp, dict):
                self._json(200, {"ok": False,
                                 "error": f"Dropi respondió HTTP {st}."})
                return
            o = resp.get("objects") or resp
            if isinstance(o, list):
                o = o[0] if o else {}
            if not isinstance(o, dict) or not o.get("id"):
                self._json(200, {"ok": False,
                                 "error": "No se encontró un producto con ese ID."})
                return
            fotos = []
            for g in (o.get("gallery") or []):
                u = g.get("urlS3") or g.get("url")
                if u:
                    fotos.append({"url": CLOUDFRONT_IMG + u,
                                  "main": bool(g.get("main"))})
            fotos.sort(key=lambda f: not f["main"])  # la principal primero
            categorias = [c.get("name") for c in (o.get("categories") or [])
                          if c.get("name")]
            # proveedor: v2 trae user_id y de ahí sacamos la tienda
            proveedor, proveedor_id = "", None
            try:
                st2, r2 = dropi.http("GET", f"{dropi.API}/products/v2/{pid}",
                                     token=ses["token"], host=ses.get("pais"))
                if st2 == 200 and isinstance(r2, dict):
                    o2 = r2.get("objects") or r2
                    if isinstance(o2, list):
                        o2 = o2[0] if o2 else {}
                    proveedor_id = (o2 or {}).get("user_id")
                    if proveedor_id:
                        st3, r3 = dropi.http(
                            "GET", f"{dropi.API}/products/supplier/v1",
                            params=[("user_id", proveedor_id)],
                            token=ses["token"], host=ses.get("pais"))
                        if st3 == 200 and isinstance(r3, dict):
                            o3 = r3.get("objects") or r3
                            if isinstance(o3, list):
                                o3 = o3[0] if o3 else {}
                            proveedor = (o3 or {}).get("store_name") or ""
            except Exception:
                pass
            self._json(200, {
                "ok": True,
                "id": o.get("id"),
                "nombre": o.get("name") or "",
                "descripcion": o.get("description") or "",
                "sku": o.get("bar_code") or o.get("external_code") or "",
                "precio": o.get("sale_price"),
                "activo": o.get("active"),
                "categorias": categorias,
                "fotos": fotos,
                "proveedor": proveedor,
                "proveedor_id": proveedor_id,
                "modulos": listar_modulos(int(pid)),
            })
            return

        if ruta.startswith("/productos_img/"):
            rel = unquote(ruta[len("/productos_img/"):])
            archivo = (CARPETA_IMAGENES / rel).resolve()
            # evitar saltos de carpeta
            if (not str(archivo).startswith(str(CARPETA_IMAGENES.resolve()))
                    or not archivo.exists()):
                self._json(404, {"error": "imagen no encontrada"})
                return
            cuerpo = archivo.read_bytes()
            self.send_response(200)
            tipo = ("image/png" if archivo.suffix == ".png"
                    else "image/jpeg" if archivo.suffix in (".jpg", ".jpeg")
                    else "image/webp" if archivo.suffix == ".webp"
                    else "application/octet-stream")
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
            return

        if ruta.startswith("/api/job/"):
            jid = ruta.split("/api/job/", 1)[1]
            with LOCK:
                job = JOBS.get(jid)
                if not job:
                    self._json(404, {"error": "trabajo desconocido"})
                    return
                self._json(200, {k: list(v) if isinstance(v, list) else v
                                 for k, v in job.items() if k != "hilo"})
            return

        if ruta.startswith("/reportes/"):
            nombre = Path(unquote(ruta[len("/reportes/"):])).name  # sin rutas
            archivo = dropi.CARPETA_REPORTES / nombre
            if not nombre.endswith(".xlsx") or not archivo.exists():
                self._json(404, {"error": "archivo no encontrado"})
                return
            body = archivo.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument"
                             ".spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if ruta == "/api/productos":
            self._json(200, {"ok": True, "productos": listar_productos()})
            return

        if ruta == "/api/ia/estado":
            cfg = leer_config_ia()
            key = cfg["key"]
            self._json(200, {
                "ok": True,
                "configurada": bool(key),
                "mascara": (key[:4] + "…" + key[-4:]) if key else "",
                "modelo": cfg["modelo"],
                "pensamiento": cfg["pensamiento"],
                "plan": cfg["plan"],
                "plantilla": cfg["plantilla"],
                "openai": bool(cfg["key_openai"]),
                "modelo_imagen": cfg["modelo_imagen"],
            })
            return

        if ruta == "/api/plantillas":
            self._json(200, {"ok": True, "plantillas": listar_plantillas()})
            return

        if ruta == "/api/descripciones":
            qs = parse_qs(urlparse(self.path).query)
            try:
                pid = int((qs.get("producto_id") or [""])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido"})
                return
            self._json(200, {"ok": True,
                             "descripciones": listar_descripciones(pid)})
            return

        self._json(404, {"error": "ruta no encontrada"})

    # ---------- POST ----------
    def do_POST(self):
        ruta = urlparse(self.path).path

        if ruta == "/api/login":
            datos = self._leer_json()
            email = (datos.get("email") or "").strip()
            password = datos.get("password") or ""
            otp = (datos.get("otp") or "").strip() or None
            if not email or not password:
                self._json(400, {"ok": False, "error": "Falta usuario o contraseña"})
                return
            try:
                ses, necesita_2fa = dropi.login(email, password, otp)
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)[:300]})
                return
            if necesita_2fa:
                if otp:
                    self._json(200, {"ok": False, "error":
                                     "Ese código no fue aceptado. Pide uno nuevo "
                                     "en tu app de autenticación e inténtalo de nuevo."})
                else:
                    self._json(200, {"ok": True, "necesita_2fa": True})
                return
            dropi.ARCHIVO_SESION.write_text(json.dumps(ses))
            self._json(200, {"ok": True, "necesita_2fa": False,
                             "user_id": ses.get("user_id")})
            return

        if ruta == "/api/reporte":
            datos = self._leer_json()
            desde, hasta = datos.get("desde", ""), datos.get("hasta", "")
            tipo = datos.get("tipo", "pedidos")
            if tipo not in ("pedidos", "cartera"):
                self._json(400, {"ok": False, "error": "Tipo de reporte desconocido"})
                return
            if not desde or not hasta or desde > hasta:
                self._json(400, {"ok": False,
                                 "error": "Rango de fechas inválido"})
                return
            ses = dropi.cargar_sesion()
            if not ses:
                self._json(200, {"ok": False, "sesion_expirada": True})
                return
            jid = uuid.uuid4().hex[:12]
            with LOCK:
                JOBS[jid] = {"estado": "corriendo", "log": [], "archivo": None,
                             "error": None}
            destino_job = correr_job_cartera if tipo == "cartera" else correr_job
            hilo = threading.Thread(target=destino_job,
                                    args=(jid, ses, desde, hasta), daemon=True)
            JOBS[jid]["hilo"] = hilo
            hilo.start()
            self._json(200, {"ok": True, "job": jid})
            return

        if ruta == "/api/producto/guardar":
            p = self._leer_json()
            try:
                int(str(p.get("id", "")))
            except ValueError:
                self._json(400, {"ok": False, "error": "Producto sin ID válido."})
                return
            try:
                guardar_producto(p)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:300]})
            return

        if ruta == "/api/ia/config":
            datos = self._leer_json()
            try:
                cfg = guardar_config_ia(datos)
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:200]})
                return
            self._json(200, {"ok": True, "configurada": bool(cfg["key"]),
                             "modelo": cfg["modelo"]})
            return

        if ruta == "/api/ia/generar":
            datos = self._leer_json()
            cfg = leer_config_ia()
            if not cfg["key"]:
                self._json(200, {"ok": False,
                                 "error": "Primero configura tu API key de Z.ai."})
                return
            try:
                pid = int(str(datos.get("id", "")))
            except ValueError:
                self._json(400, {"ok": False, "error": "Producto inválido."})
                return
            producto = next((p for p in listar_productos() if p["id"] == pid), None)
            if not producto:
                self._json(200, {"ok": False,
                                 "error": "Ese producto no está guardado en la base."})
                return
            # guardar la plantilla editada para la próxima vez
            guardar_config_ia({"plantilla": datos.get("prompt") or None,
                               "modelo": datos.get("modelo"),
                               "pensamiento": datos.get("pensamiento"),
                               "plan": datos.get("plan")})
            # ------- rama de generación de imagen: trabajo en 2do plano -------
            if datos.get("tipo") == "imagen":
                cfg_img = leer_config_ia()
                if not cfg_img["key_openai"]:
                    self._json(200, {"ok": False,
                                     "error": "Configura primero tu API key "
                                              "de OpenAI (imágenes)."})
                    return
                jid = uuid.uuid4().hex[:12]
                with LOCK:
                    JOBS[jid] = {"estado": "corriendo", "log": [],
                                 "archivo": None, "imagen": None, "error": None}
                hilo = threading.Thread(target=correr_job_imagen,
                                        args=(jid, datos, producto),
                                        daemon=True)
                JOBS[jid]["hilo"] = hilo
                hilo.start()
                self._json(200, {"ok": True, "job": jid})
                return
            try:
                cfg_final = leer_config_ia()
                guardadas = listar_descripciones(pid)
                extra = "\n\n".join(
                    f"[Guardada el {g['creada_en']}]\n{g['texto']}"
                    for g in guardadas[:5])
                modulos_txt = "\n\n".join(
                    f"[Módulo: {m['nombre']}]\n{_html_a_texto(m['contenido'], 800)}"
                    for m in (producto.get("modulos") or []))
                if modulos_txt:
                    extra = (extra + "\n\n" if extra else "") + modulos_txt
                texto = zai_generar(cfg_final["key"], cfg_final["modelo"],
                                    datos.get("prompt") or cfg_final["plantilla"],
                                    producto, cfg_final["pensamiento"],
                                    cfg_final["plan"], extra)
                self._json(200, {"ok": True, "texto": texto})
            except urllib.error.HTTPError as e:
                detalle = e.read().decode(errors="replace")[:300]
                self._json(200, {"ok": False,
                                 "error": f"Z.ai respondió HTTP {e.code}: {detalle}"})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:300]})
            return

        if ruta == "/api/producto/eliminar":
            p = self._leer_json()
            try:
                pid = int(str(p.get("id", "")))
            except ValueError:
                self._json(400, {"ok": False, "error": "ID inválido."})
                return
            eliminar_producto(pid)
            self._json(200, {"ok": True})
            return

        if ruta == "/api/ia/desconectar":
            d = self._leer_json()
            cfg = leer_config_ia()
            if d.get("que") == "openai":
                cfg["key_openai"] = ""
            elif d.get("que") == "zai":
                cfg["key"] = ""
            else:
                self._json(400, {"ok": False, "error": "¿qué desconecto?"})
                return
            ARCHIVO_CONFIG_IA.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2))
            self._json(200, {"ok": True})
            return

        if ruta == "/api/modulo/guardar":
            d = self._leer_json()
            nombre = (d.get("nombre") or "").strip()
            if not nombre:
                self._json(400, {"ok": False,
                                 "error": "El módulo necesita un nombre."})
                return
            try:
                guardar_modulo(d.get("id"), int(str(d.get("producto_id", ""))),
                               nombre, d.get("contenido") or "")
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido."})
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if ruta == "/api/modulo/eliminar":
            d = self._leer_json()
            try:
                eliminar_modulo(int(str(d.get("id", ""))))
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "ID inválido."})
            return

        if ruta == "/api/plantillas/guardar":
            d = self._leer_json()
            nombre = (d.get("nombre") or "").strip()
            texto = d.get("texto") or ""
            if not nombre or not texto.strip():
                self._json(400, {"ok": False,
                                 "error": "La plantilla necesita nombre y texto."})
                return
            try:
                pid_pl = guardar_plantilla(d.get("id"), nombre, texto,
                                           d.get("tipo"))
                self._json(200, {"ok": True, "id": pid_pl})
            except sqlite3.IntegrityError:
                self._json(200, {"ok": False,
                                 "error": "Ya existe una plantilla con ese nombre."})
            return

        if ruta == "/api/plantillas/eliminar":
            d = self._leer_json()
            try:
                eliminar_plantilla(int(str(d.get("id", ""))))
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "ID inválido."})
            return

        if ruta == "/api/descripcion/guardar":
            d = self._leer_json()
            try:
                pid_d = int(str(d.get("producto_id", "")))
                guardar_descripcion(pid_d, d.get("texto") or "")
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido."})
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if ruta == "/api/descripcion/eliminar":
            d = self._leer_json()
            try:
                eliminar_descripcion(int(str(d.get("id", ""))))
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "ID inválido."})
            return

        if ruta == "/api/imagen/agregar":
            d = self._leer_json()
            try:
                ruta_nueva = agregar_imagen_producto(
                    int(str(d.get("producto_id", ""))),
                    d.get("nombre") or "imagen.png",
                    d.get("data") or "")
                self._json(200, {"ok": True, "ruta": ruta_nueva})
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido."})
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if ruta == "/api/imagen/eliminar":
            d = self._leer_json()
            try:
                eliminar_imagen_producto(int(str(d.get("producto_id", ""))),
                                         d.get("ruta") or "")
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido."})
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if ruta == "/api/imagen/producto":
            d = self._leer_json()
            try:
                agregar_foto_producto(int(str(d.get("producto_id", ""))),
                                      d.get("ruta") or "")
                self._json(200, {"ok": True})
            except ValueError:
                self._json(400, {"ok": False, "error": "producto_id inválido."})
            except RuntimeError as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if ruta == "/api/producto/editar":
            d = self._leer_json()
            try:
                pid_e = int(str(d.get("id", "")))
            except ValueError:
                self._json(400, {"ok": False, "error": "ID inválido."})
                return
            with db() as con:
                existe = con.execute("SELECT 1 FROM productos WHERE id=?",
                                     (pid_e,)).fetchone()
            if not existe:
                self._json(200, {"ok": False,
                                 "error": "Ese producto no está guardado."})
                return
            editar_descripcion_producto(pid_e, d.get("descripcion") or "")
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "ruta no encontrada"})


def ip_red_local():
    """IP privada de este equipo en la red local (para mostrar la URL de red)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # no envía nada, solo descubre la IP
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Página web local para descargar "
                                             "reportes de Dropi")
    ap.add_argument("--puerto", type=int, default=8765)
    ap.add_argument("--sin-abrir", action="store_true",
                    help="no abrir el navegador automáticamente")
    ap.add_argument("--lan", action="store_true",
                    help="escuchar en toda la red local (acceso desde otros equipos)")
    args = ap.parse_args()

    escucha = "0.0.0.0" if args.lan else "127.0.0.1"
    servidor = ThreadingHTTPServer((escucha, args.puerto), Handler)
    url = f"http://127.0.0.1:{args.puerto}"
    print(f"Página de reportes Dropi: {url}  (Ctrl+C para salir)")
    if args.lan:
        ip = ip_red_local() or "<IP-de-este-equipo>"
        print(f"Accesible desde otros equipos de la red: http://{ip}:{args.puerto}")
    if not args.sin_abrir:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor cerrado.")


if __name__ == "__main__":
    main()
