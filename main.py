# -*- coding: utf-8 -*-
"""
Procesador de codigos QR de facturas (PDF e imagenes).

Estrategia (de mas fiable a menos fiable):
  A) PDF: extraer las IMAGENES INCRUSTADAS (sin perdida, sin re-renderizar).
  B) Render a 300 DPI + zxing-cpp (pagina completa y por teselas con solape)
     para QR dibujados como vector o PDF escaneados.
  C) Verificacion: cada PNG guardado se vuelve a leer de disco y se comprueba
     que es escaneable. Un documento solo pasa a 'Procesados' si TODO esta OK.

Uso:
  main.py                         -> procesa todo lo que haya en Facturas/
  main.py a.pdf b.png             -> procesa solo esos archivos (para FoxPro)
  main.py a.pdf --esperados 33    -> ademas exige exactamente 33 QR
Codigos de salida: 0 = todo OK | 1 = algun documento incompleto | 2 = error
"""
import argparse, base64, csv, io, json, re, shutil, sys, traceback
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import zxingcpp

# ------------------------------------------------------------------ rutas
# Como .exe (PyInstaller) la base es la carpeta donde esta el ejecutable (NO la carpeta
# temporal de extraccion), asi funciona desde cualquier ubicacion e instalacion.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent


def _carpeta(*nombres):
    """Usa la primera carpeta que ya exista; si no existe ninguna, la primera por defecto."""
    for n in nombres:
        if (BASE / n).is_dir():
            return BASE / n
    return BASE / nombres[0]


CARP_FACTURAS = _carpeta("Facturas", "factura")
CARP_QR = BASE / "Codigos_QR"
CARP_PROCESADOS = _carpeta("Procesados", "procesado")
CARP_REGISTRO = BASE / "Registro"

DPI = 300
EXT_IMG = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
QUIET_ZONE_FRAC = 0.12     # borde blanco anadido a cada recorte (>= 4 modulos)
MIN_LADO_EMBEBIDA = 50     # px minimos para considerar una imagen incrustada
FMT_QR = zxingcpp.BarcodeFormat.QRCode


# ------------------------------------------------------------ utilidades
def a_gris(img):
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def decodifica_qr(img):
    """Devuelve lista de resultados zxing (solo QR). El contenido NO se guarda."""
    g = a_gris(img)
    return zxingcpp.read_barcodes(g, formats=FMT_QR, try_rotate=True, try_downscale=True)


def es_escaneable(img):
    """Prueba con borde blanco; si falla, con variantes de preprocesado."""
    g = a_gris(img)
    variantes = [g]
    h, w = g.shape
    if max(h, w) < 400:
        variantes.append(cv2.resize(g, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
    _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variantes.append(otsu)
    for v in variantes:
        if decodifica_qr(v):
            return True
    return False


def con_quiet_zone(img, frac=QUIET_ZONE_FRAC):
    """Anade un marco blanco para garantizar la zona de silencio del QR."""
    h, w = img.shape[:2]
    m = max(24, int(round(max(h, w) * frac)))
    blanco = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.copyMakeBorder(img, m, m, m, m, cv2.BORDER_CONSTANT, value=blanco)


def pixmap_a_bgr(pm):
    if pm.alpha:  # componer sobre blanco
        pm = pymupdf.Pixmap(pymupdf.csRGB, pm) if pm.colorspace.n != 3 else pm
    if pm.colorspace is None or pm.colorspace.n == 1:
        a = np.frombuffer(pm.samples, np.uint8).reshape(pm.h, pm.w, pm.n)[:, :, 0]
        return cv2.cvtColor(a, cv2.COLOR_GRAY2BGR)
    if pm.colorspace.n != 3:
        pm = pymupdf.Pixmap(pymupdf.csRGB, pm)
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.h, pm.w, pm.n)
    if pm.n == 4:  # RGBA -> sobre blanco
        alfa = a[:, :, 3:4].astype(np.float32) / 255
        rgb = a[:, :, :3].astype(np.float32) * alfa + 255 * (1 - alfa)
        a = rgb.astype(np.uint8)
    return cv2.cvtColor(a, cv2.COLOR_RGB2BGR)


def iou(a, b):
    ax1, ay1, ax2, ay2 = a; bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1)); iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


# ------------------------------------------------- numero de pedido (PED.)
# Busca el campo "PED." / "PED:" / "PED. No." y toma el valor que le sigue
# (en la misma linea o en la siguiente). El valor debe contener al menos un digito.
RE_PED = re.compile(
    r"\bPED\b\s*\.?\s*(?:N[º°o]\.?|NRO\.?|NUM\.?|#)?\s*[:\-]?\s*"
    r"(?=[A-Z0-9\-/]*\d)([A-Z0-9][A-Z0-9\-/]*)", re.IGNORECASE)


def limpia_nombre(txt):
    """Valor seguro para usar dentro de un nombre de archivo de Windows."""
    return re.sub(r"[^\w\-]", "-", txt).strip("-_")


def extraer_pedido(pagina):
    """Numero de pedido de una pagina PDF (None si no hay campo PED. legible)."""
    texto = pagina.get_text("text", sort=True)
    m = RE_PED.search(texto)
    if not m:
        return None
    return limpia_nombre(m.group(1)) or None


def columna_ped(pagina):
    """Facturas en tabla: 'PED' es el ENCABEZADO de una columna y el numero de cada fila
    esta debajo. Devuelve [(y_centro, valor)] de los textos situados bajo ese encabezado."""
    palabras = pagina.get_text("words")
    cab = [w for w in palabras if w[4].strip(" .:").upper() == "PED"]
    if not cab:
        return []
    cab = min(cab, key=lambda w: w[1])            # el encabezado mas alto de la pagina
    cx = (cab[0] + cab[2]) / 2
    tol = max(cab[2] - cab[0], 14) + 8            # ancho de la columna (en puntos)
    valores = []
    for w in palabras:
        txt = w[4].strip()
        if w[1] > cab[3] and abs((w[0] + w[2]) / 2 - cx) <= tol and re.search(r"\d", txt):
            v = limpia_nombre(txt)
            if v:
                valores.append(((w[1] + w[3]) / 2, v))
    return valores


def pedido_de_qr(valores, bbox):
    """Numero de pedido de la fila donde esta el QR (mismo alto que el QR)."""
    x1, y1, x2, y2 = bbox
    cy = (y1 + y2) / 2
    dentro = [(abs(c - cy), v) for c, v in valores if y1 - 3 <= c <= y2 + 3]
    return min(dentro)[1] if dentro else None


def nombre_unico(base, usados):
    """Si varios QR comparten pedido, anade _2, _3... para no sobrescribir."""
    cand, k = base, 1
    while cand.lower() in usados:
        k += 1
        cand = f"{base}_{k}"
    usados.add(cand.lower())
    return cand + ".png"


# --------------------------------------------- A) imagenes incrustadas (PDF)
def extraer_incrustadas(doc, pagina):
    """QR candidatos = imagenes incrustadas aproximadamente cuadradas."""
    salida = []
    for info in pagina.get_image_info(xrefs=True):
        xref = info["xref"]
        if not xref:
            continue
        w, h = info["width"], info["height"]
        if min(w, h) < MIN_LADO_EMBEBIDA or not (0.85 <= w / h <= 1.18):
            continue
        # descarta imagenes que ocupan casi toda la pagina (pagina escaneada)
        bb = pymupdf.Rect(info["bbox"])
        if bb.width > pagina.rect.width * 0.6:
            continue
        try:
            pm = pymupdf.Pixmap(doc, xref)
            if info.get("has-mask") or pm.alpha:
                smask = doc.extract_image(xref).get("smask", 0)
                if smask and not pm.alpha:
                    pm = pymupdf.Pixmap(pm, pymupdf.Pixmap(doc, smask))
            img = pixmap_a_bgr(pm)
        except Exception:
            continue
        salida.append({"img": img, "bbox": tuple(bb), "metodo": "incrustada"})
    return salida


# ------------------------------------------- B) render + zxing (con teselas)
def detectar_en_imagen(img_bgr, escala_pt=1.0):
    """Devuelve [(bbox_px)] de todos los QR; pasada completa + teselas solapadas."""
    g = a_gris(img_bgr)
    H, W = g.shape
    cajas = []

    def anade(res, ox=0, oy=0):
        for r in res:
            p = r.position
            xs = [p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x]
            ys = [p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y]
            c = (min(xs) + ox, min(ys) + oy, max(xs) + ox, max(ys) + oy)
            if not any(iou(c, o) > 0.3 for o in cajas):
                cajas.append(c)

    anade(decodifica_qr(g))
    # teselas 3x3 con 25 % de solape (rescata QR cuando hay muchos o hay ruido)
    th, tw = H // 3, W // 3
    sx, sy = int(tw * .25), int(th * .25)
    for iy in range(3):
        for ix in range(3):
            x1, y1 = max(0, ix * tw - sx), max(0, iy * th - sy)
            x2, y2 = min(W, (ix + 1) * tw + sx), min(H, (iy + 1) * th + sy)
            anade(decodifica_qr(g[y1:y2, x1:x2]), x1, y1)
    return cajas


def recorta_caja(img, caja, margen_rel=0.15):
    x1, y1, x2, y2 = caja
    m = int(max(x2 - x1, y2 - y1) * margen_rel)
    H, W = img.shape[:2]
    return img[max(0, int(y1) - m):min(H, int(y2) + m), max(0, int(x1) - m):min(W, int(x2) + m)]


# ------------------------------------------------------- proceso por pagina
def procesar_pagina_pdf(doc, pagina):
    """Devuelve lista de dicts {img, bbox(pt), metodo} ya fusionados y ordenados."""
    k = DPI / 72
    pix = pagina.get_pixmap(matrix=pymupdf.Matrix(k, k), alpha=False)
    render = pixmap_a_bgr(pix)

    items = extraer_incrustadas(doc, pagina)
    # QR detectados en el render (cubre vectoriales y paginas escaneadas)
    for c in detectar_en_imagen(render):
        bbox_pt = tuple(v / k for v in c)
        if any(iou(bbox_pt, it["bbox"]) > 0.25 for it in items):
            continue  # ya cubierto por una imagen incrustada
        items.append({"img": recorta_caja(render, c), "bbox": bbox_pt, "metodo": "render"})
    items.sort(key=lambda d: (round(d["bbox"][1] / 10), d["bbox"][0]))  # arriba->abajo
    return items


def procesar_documento(ruta, esperados=None):
    nombre = ruta.stem
    salida = CARP_QR / nombre
    if salida.exists():
        shutil.rmtree(salida)           # evita restos de ejecuciones anteriores
    salida.mkdir(parents=True)

    filas, incidencias, paginas = [], [], 0

    if ruta.suffix.lower() == ".pdf":
        doc = pymupdf.open(str(ruta))
        try:
            paginas = len(doc)
            por_pagina = []
            for n, p in enumerate(doc, 1):
                items = procesar_pagina_pdf(doc, p)
                col = columna_ped(p)
                for it in items:                       # pedido de la fila de cada QR
                    it["pedido"] = pedido_de_qr(col, it["bbox"])
                por_pagina.append((n, items, extraer_pedido(p)))   # respaldo: "PED.: 123"
        finally:
            doc.close()
    else:
        img = cv2.imdecode(np.fromfile(str(ruta), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("No se pudo abrir la imagen")
        paginas = 1
        cajas = sorted(detectar_en_imagen(img), key=lambda c: (round(c[1] / 40), c[0]))
        por_pagina = [(1, [{"img": recorta_caja(img, c), "bbox": c, "metodo": "render"} for c in cajas], None)]

    es_pdf = ruta.suffix.lower() == ".pdf"
    unicos = {p for _, _, p in por_pagina if p}   # pedidos distintos del documento
    unicos |= {it["pedido"] for _, its, _ in por_pagina for it in its if it.get("pedido")}
    usados = set()

    for n_pag, items, pedido_pag in por_pagina:
        for i, it in enumerate(items, 1):
            pedido = it.get("pedido") or pedido_pag
            if es_pdf and not pedido:
                if len(unicos) == 1:              # un solo pedido en todo el documento
                    pedido = next(iter(unicos))
                else:
                    pedido = "SINPED"
                    incidencias.append(f"P{n_pag} #{i}: no se encontro el numero de pedido (PED)")
            if es_pdf:   # viaje (nombre del archivo) + pedido
                png = salida / nombre_unico(f"QR_{nombre}_{pedido}", usados)
            else:        # imagenes sueltas: sin texto que leer, se mantiene el esquema anterior
                png = salida / f"QR_{nombre}_P{n_pag:02d}_{i:03d}.png"
            final = con_quiet_zone(it["img"])
            ok_escritura, buf = cv2.imencode(".png", final)
            if ok_escritura:
                buf.tofile(str(png))
            # VERIFICACION: releer de disco y comprobar que se puede escanear
            leida = cv2.imdecode(np.fromfile(str(png), np.uint8), cv2.IMREAD_COLOR)
            verificado = leida is not None and es_escaneable(leida)
            if not verificado:
                incidencias.append(f"P{n_pag} #{i}: recorte no escaneable")
            filas.append({"pagina": n_pag, "n": i, "archivo": png.name, "metodo": it["metodo"],
                          "pedido": (pedido or "") if es_pdf else "",
                          "verificado": "SI" if verificado else "NO",
                          "ancho": final.shape[1], "alto": final.shape[0]})

    # imagenes cuadradas incrustadas que NO se pudieron leer -> sospechosas
    total = len(filas)
    verificados = sum(f["verificado"] == "SI" for f in filas)
    if esperados is not None and total != esperados:
        incidencias.append(f"Esperados {esperados}, extraidos {total}")
    if total == 0:
        incidencias.append("No se detecto ningun QR")

    completo = bool(total) and verificados == total and not incidencias
    return {"documento": ruta.name, "paginas": paginas, "qr": total, "verificados": verificados,
            "completo": completo, "incidencias": incidencias, "detalle": filas}


# ---------------------------------------------------------------- registro
def registrar(res):
    CARP_REGISTRO.mkdir(exist_ok=True)
    resumen = CARP_REGISTRO / "registro_documentos.csv"
    detalle = CARP_REGISTRO / "registro_qr.csv"
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def abrir(p, cab):
        nuevo = not p.exists()
        f = open(p, "a", newline="", encoding="utf-8-sig")
        w = csv.writer(f, delimiter=";")
        if nuevo:
            w.writerow(cab)
        return f, w

    f, w = abrir(resumen, ["Fecha", "Documento", "Paginas", "QR", "Verificados", "Estado", "Incidencias"])
    w.writerow([ahora, res["documento"], res["paginas"], res["qr"], res["verificados"],
                "PROCESADO" if res["completo"] else "REVISAR", " | ".join(res["incidencias"])])
    f.close()
    f, w = abrir(detalle, ["Fecha", "Documento", "Pagina", "N", "Archivo", "Metodo", "Verificado", "Pedido", "Ruta", "QR_Base64"])
    for d in res["detalle"]:
        w.writerow([ahora, res["documento"], d["pagina"], d["n"], d["archivo"], d["metodo"], d["verificado"], d["pedido"],
                    str(CARP_QR / Path(res["documento"]).stem / d["archivo"]),
                    qr_base64(CARP_QR / Path(res["documento"]).stem / d["archivo"])])
    f.close()
    registrar_xlsx(res, ahora)


COLS_XLSX = ["Fecha", "Documento", "Pagina", "N", "Archivo", "Metodo", "Verificado",
             "Pedido", "Ruta", "Imagen QR", "QR_Base64"]


def qr_base64(png):
    """PNG del QR (gris, <=220 px) codificado en Base64: texto que SI sobrevive al pasar
    a CSV/tabla; Visual FoxPro lo recupera con STRCONV(campo, 14). Cabe en una celda de Excel."""
    img = cv2.imdecode(np.fromfile(str(png), np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    for lado in (220, 180, 150):
        k = lado / max(img.shape[:2])
        mini = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img
        ok, buf = cv2.imencode(".png", mini, [cv2.IMWRITE_PNG_COMPRESSION, 9])
        txt = base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
        if len(txt) < 32000:      # limite de caracteres por celda de Excel
            return txt
    return ""
LADO_MINI = 110   # px con que se muestra el QR dentro de la celda


def registrar_xlsx(res, ahora):
    """Mismo detalle que registro_qr.csv, pero en Excel y con la imagen del QR incrustada."""
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.styles import Font, Alignment
        ruta_xlsx = CARP_REGISTRO / "registro_qr.xlsx"
        if ruta_xlsx.exists():
            wb = load_workbook(ruta_xlsx)
            ws = wb.active
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Registro QR"
            ws.append(COLS_XLSX)
            for c in ws[1]:
                c.font = Font(bold=True)
            for col, ancho in zip("ABCDEFGHIJK", (19, 22, 8, 6, 34, 11, 11, 16, 50, 18, 30)):
                ws.column_dimensions[col].width = ancho
        for d in res["detalle"]:
            png = CARP_QR / Path(res["documento"]).stem / d["archivo"]
            ws.append([ahora, res["documento"], d["pagina"], d["n"], d["archivo"], d["metodo"],
                       d["verificado"], d["pedido"], str(png), None, qr_base64(png)])
            fila = ws.max_row
            ws.row_dimensions[fila].height = LADO_MINI * 0.75 + 6   # px -> puntos
            for c in ws[fila]:
                c.alignment = Alignment(vertical="center")
            img = cv2.imdecode(np.fromfile(str(png), np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                k = 220 / max(img.shape[:2])
                if k < 1:
                    img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".png", img)
                if ok:
                    xl = XLImage(io.BytesIO(buf.tobytes()))
                    xl.width = xl.height = LADO_MINI
                    ws.add_image(xl, f"J{fila}")
        wb.save(ruta_xlsx)
    except Exception as e:   # p. ej. el Excel esta abierto: no se pierde el CSV ni se detiene el proceso
        print(f"   ! No se pudo actualizar registro_qr.xlsx ({e}). Cierre el archivo si esta abierto.")


def mover_a_procesados(ruta):
    CARP_PROCESADOS.mkdir(exist_ok=True)
    destino = CARP_PROCESADOS / ruta.name
    if destino.exists():
        destino = CARP_PROCESADOS / f"{ruta.stem}_{datetime.now():%Y%m%d_%H%M%S}{ruta.suffix}"
    shutil.move(str(ruta), str(destino))


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Extractor de codigos QR de facturas")
    ap.add_argument("archivos", nargs="*", help="archivos a procesar (por defecto: carpeta Facturas)")
    ap.add_argument("--esperados", type=int, help="numero exacto de QR esperado (solo 1 documento)")
    ap.add_argument("--no-mover", action="store_true", help="no mover a Procesados")
    ap.add_argument("--json", help="ruta donde escribir el resultado en JSON (para FoxPro)")
    ap.add_argument("--sin-pausa", action="store_true", help="no esperar Enter al terminar")
    a = ap.parse_args(argv)

    if a.archivos:
        rutas = [Path(x) for x in a.archivos]
    else:
        CARP_FACTURAS.mkdir(exist_ok=True)
        rutas = sorted(p for p in CARP_FACTURAS.iterdir()
                       if p.is_file() and (p.suffix.lower() == ".pdf" or p.suffix.lower() in EXT_IMG))
    if not rutas:
        print("No hay documentos para procesar.")
        return 0

    resultados, hay_error = [], False
    for r in rutas:
        print(f"\n== {r.name}")
        try:
            if not r.exists():
                raise FileNotFoundError(str(r))
            res = procesar_documento(r, a.esperados if len(rutas) == 1 else None)
        except Exception as e:
            res = {"documento": r.name, "paginas": 0, "qr": 0, "verificados": 0,
                   "completo": False, "incidencias": [f"Error: {e}"], "detalle": []}
            hay_error = True
        registrar(res)
        if res["completo"] and not a.no_mover:
            mover_a_procesados(r)
        estado = "PROCESADO" if res["completo"] else "REVISAR"
        print(f"   QR: {res['qr']}  verificados: {res['verificados']}  -> {estado}")
        for inc in res["incidencias"]:
            print("   !", inc)
        resultados.append({k: v for k, v in res.items() if k != "detalle"} | {"estado": estado})

    if a.json:
        Path(a.json).write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    if hay_error:
        return 2
    return 0 if all(x["completo"] for x in resultados) else 1


def _pausa():
    try:
        input("\nProceso terminado. Pulse Enter para cerrar...")
    except Exception:
        pass


if __name__ == "__main__":
    pausa = False
    try:
        codigo = main()
        pausa = getattr(sys, "frozen", False) and len(sys.argv) == 1 and sys.stdin is not None and sys.stdin.isatty()
    except SystemExit as e:                      # --help / argumentos invalidos
        codigo = e.code if isinstance(e.code, int) else 2
    except Exception:
        CARP_REGISTRO.mkdir(exist_ok=True)
        (CARP_REGISTRO / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        print("Error inesperado; ver Registro\\error.log")
        codigo = 2
        pausa = getattr(sys, "frozen", False) and len(sys.argv) == 1
    if pausa and "--sin-pausa" not in sys.argv:
        _pausa()
    sys.exit(codigo)
