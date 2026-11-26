# Extractor de códigos QR de facturas

Herramienta de línea de comandos que recorre facturas en **PDF o imagen**, extrae cada código QR, comprueba que se puede leer, lo asocia con su **número de pedido** y deja un registro en CSV y Excel.

Complemento: [qr-base64-decoder](https://github.com/alizmarillera-glitch/qr-base64-decoder) convierte de vuelta a imágenes los QR que este programa guarda en Base64 dentro del Excel.

## Qué hace

1. **Extrae las imágenes incrustadas** del PDF, sin pérdida y sin volver a dibujar la página (método más fiable).
2. Si el QR está dibujado como vector o el PDF es un escaneo, **renderiza a 300 DPI** y lo busca con zxing-cpp, en la página completa y por recortes con solape.
3. **Verifica** cada PNG guardado: lo vuelve a leer desde disco y confirma que es escaneable.
4. **Asocia el pedido**: lee el campo `PED.` de la factura (también cuando está en una tabla) y nombra cada archivo como `QR_<documento>_<pedido>.png`. Si varios QR comparten pedido, los numera para no sobrescribir.
5. **Registra** el resultado en `registro_documentos.csv`, `registro_qr.csv` y `registro_qr.xlsx` (con la imagen del QR incrustada y su versión en Base64).
6. Un documento solo pasa a `Procesados/` si **todos** sus QR quedaron verificados; si no, queda para revisión.

## Instalación

Requiere Python 3.10 o superior.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      |  Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
python main.py                          # procesa todo lo que haya en Facturas/
python main.py a.pdf b.png              # procesa solo esos archivos
python main.py a.pdf --esperados 33     # además exige exactamente 33 QR
```

| Opción | Descripción |
|---|---|
| `--esperados N` | Número exacto de QR esperado (solo con un documento) |
| `--no-mover` | No mover el documento a `Procesados/` |
| `--json RUTA` | Escribe el resultado en JSON, para integrarlo con otros sistemas |
| `--sin-pausa` | No esperar Enter al terminar |

**Códigos de salida:** `0` todo correcto · `1` algún documento incompleto · `2` error.

## Carpetas

El programa las crea solas junto a `main.py`:

```
Facturas/      documentos de entrada (PDF e imágenes)
Codigos_QR/    un subdirectorio por documento con sus QR en PNG
Procesados/    documentos ya completados
Registro/      CSV, Excel y error.log
```

## Integración

Pensado para ser llamado desde otro sistema (por ejemplo, Visual FoxPro) con los argumentos `--json` y `--sin-pausa`, y para usarse el código de salida. Detecta si corre como `.exe` (PyInstaller) y entonces busca sus carpetas junto al ejecutable.

## Privacidad

Este repositorio contiene solo el código. Las facturas, los QR generados y los registros no se suben (están en `.gitignore`): el Base64 del Excel permite reconstruir cada QR y el contenido al que apunta.
