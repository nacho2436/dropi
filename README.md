# Descarga automática de reportes de Dropi

Herramienta local para `app.dropi.co` usando los endpoints oficiales, sin
abrir el navegador ni filtrar a mano. La página tiene dos pestañas:

- **📊 Reportes**
  - **📦 Pedidos** — "Órdenes con Productos (un producto por fila)"
  - **💰 Cartera** — historial de movimientos de tu wallet
    (`/dashboard/historywallet`), con rango de fechas.
- **🛍️ Productos** — buscas por **ID de producto** y te trae la ficha:
  imágenes (galería con foto principal y miniaturas), descripción completa,
  precio, SKU, **proveedor**, categorías y estado.
  - **💾 Guardar producto**: lo almacena en la base de datos local
    `productos.sqlite` y **descarga las imágenes** a `productos_images/`
    (quedan tuyas aunque Dropi borre el producto).
  - **📚 Productos guardados**: lista de todo lo guardado, con botones
    *Ver ficha* (usa tus imágenes locales) y *Eliminar*.
  - **✏️ Editar descripción**: complementa la descripción del proveedor;
    la IA usa siempre la versión editada.
- **🤖 IA (Z.ai + OpenAI)**
  - Genera **descripciones** con GLM (Z.ai) usando plantillas de prompt.
  - Genera **imágenes** con la API de OpenAI (`gpt-image-1`): las plantillas
    tienen una casilla «genera imágenes»; puedes elegir una **imagen de
    referencia** del producto (se envía al endpoint de edición de OpenAI);
    el resultado puede agregarse a la galería del producto.
  - **Galería editable**: desde la ficha del producto puedes **agregar**
    imágenes (➕) y **eliminarlas** (✕ sobre la miniatura).
  - **Plantillas**: crear, guardar, eliminar; de texto (Z.ai) o imagen (OpenAI).
  - **Descripciones generadas**: se guardan dentro del producto y se usan
    como contexto adicional en las siguientes generaciones.

Hay dos formas de usarlo:

## 1. Página web amigable (recomendada)

```bash
./dropi.sh
```

Se abre un **menú de selección** con el estado actual del servicio:

```
 Estado: ● ACTIVO  →  http://127.0.0.1:8765

   1) ▶  Iniciar servicio
   2) ■  Detener servicio
   3) ↻  Reiniciar servicio
   4) 👁  Ver estado
   5) 🌐  Abrir la página
   0) ✕  Salir
```

También acepta el comando directo si lo prefieres:
`./dropi.sh iniciar|detener|reiniciar|estado|abrir`
(otro puerto con `PUERTO=9000 ./dropi.sh`). En la página puedes:

- elegir el **rango de fechas** (con botones rápidos: mes actual, mes
  anterior, solo hoy);
- si la sesión expiró, te pide **usuario, contraseña y el código de dos
  factores** ahí mismo;
- ver el progreso en vivo y **guardar el Excel** con un clic.

Opciones: `--puerto 9000` (otro puerto), `--sin-abrir` (no abrir navegador).

### Abrir la página desde otro equipo de la red

Arranca el servicio con `LAN=1`:

```bash
LAN=1 ./dropi.sh          # o:  LAN=1 ./dropi.sh iniciar
LAN=1 PUERTO=9000 ./dropi.sh
```

El servidor pasa a escuchar en toda tu red local y el menú te muestra la
URL para los otros equipos, por ejemplo **http://192.168.1.17:8765**:
ábrela en el navegador del teléfono o del otro PC y listo (la página, los
reportes, los productos guardados y la IA funcionan igual).

Sin `LAN=1` sigue escuchando solo en `127.0.0.1`, nada expuesto a la red.

> ⚠️ Ojo: quien entre desde otro equipo de la red **usa la sesión de Dropi
> ya iniciada en el servidor** (el token vive en este equipo). Activa `LAN=1`
> solo en redes de confianza (tu casa/oficina), y date cuenta de que la
> base de productos y las claves de IA también quedan accesibles desde
> esos equipos.

## 2. Línea de comandos

```bash
python3 dropi_reporte_mensual.py                 # mes actual
python3 dropi_reporte_mensual.py --mes 2026-08   # un mes específico
python3 dropi_reporte_mensual.py --desde 2026-08-01 --hasta 2026-08-31
```

Los archivos quedan en `reportes/` con el nombre que usa Dropi
(p. ej. `ordenes_productos_20260928_122755.xlsx`).

## Login y 2FA

- La primera vez pide usuario, contraseña y el código de 6 dígitos de tu
  app de autenticación.
- El token se guarda en `.dropi_sesion.json` y sirve ~12 horas; mientras
  no expire, las corridas siguientes no piden nada.

## Endpoints que usa (descubiertos del frontend v3.4.0)

| Paso | Endpoint |
|---|---|
| Login + 2FA | `POST https://api-v2.dropi.co/bff/auth/core/login` |
| Crear reporte de pedidos | `GET https://reports.dropi.co/api/orders/exportexcel?exportAs=productsbyRow&from=…&until=…` |
| Estado del reporte | `GET https://api.dropi.co/api/reports/index` |
| Descargar pedidos (xlsx) | `GET https://d1l4mzebo786pw.cloudfront.net/reports/orders/…` |
| Exportar cartera (xlsx directo) | `GET https://api.dropi.co/api/wallet/exportexcel?from=…&until=…&user_id=…&token=…` |
| Ficha de producto | `GET https://api.dropi.co/api/products/productlist/v1/show/?id=…` |
| Imágenes de producto | `https://d39ru7awumhhs2.cloudfront.net/colombia/products/…` |

Nota: `api.dropi.co` rechaza con 403 peticiones sin cabeceras de navegador
(`Sec-Fetch-*`, `Accept-Language`, etc.); el script ya las envía.

## Seguridad

`.dropi_sesion.json` contiene el token de sesión: está en `.gitignore`
y **nunca** debe subirse a git.
