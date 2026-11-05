"""Carátulas PDF en paralelo con la cuenta del usuario de Apps Script.

Apps Script decide QUÉ lleva cada carátula (textos, reglas de anexos y carátula
maestra). Este módulo hace el trabajo en Drive y Slides, varias a la vez y con
el mismo resultado que crearPDFDesdePlantilla_ de Apps Script:

  copiar plantilla -> reemplazar y centrar -> exportar a PDF -> subir -> retirar copia

Solo atiende plantillas de Google Slides; las de Docs siguen en Apps Script.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import fitz
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

MIME_CARPETA = "application/vnd.google-apps.folder"
REINTENTOS = 6
INTENTOS_EXPORTAR = 3
ESPERA_EXPORTAR_S = 2.0
MAX_ITEMS_POR_LOTE = 200

CAMPOS_SLIDES = (
    "slides(pageElements(objectId,"
    "shape(text(textElements(textRun(content),autoText(content)))),"
    "table(tableRows(tableCells(location,text(textElements(textRun(content),autoText(content))))))))"
)


def _env_int(nombre: str, predeterminado: int) -> int:
    try:
        return int(os.getenv(nombre, "") or predeterminado)
    except ValueError:
        return predeterminado


class _Ritmo:
    """Reparte las escrituras en el tiempo para no chocar con el límite de Google."""

    def __init__(self, por_minuto: int) -> None:
        self.intervalo = 60.0 / max(1, por_minuto)
        self.candado = threading.Lock()
        self.siguiente = 0.0

    def esperar(self) -> None:
        with self.candado:
            ahora = time.monotonic()
            turno = max(ahora, self.siguiente)
            self.siguiente = turno + self.intervalo
        if turno > ahora:
            time.sleep(turno - ahora)


# Google permite unas 60 escrituras de Slides por minuto a cada usuario.
_ritmo_slides = _Ritmo(_env_int("CARATULAS_SLIDES_POR_MINUTO", 55))

# Compartidos entre trabajos: dos lotes no deben crear la misma carpeta a la vez.
_guardia_carpetas = threading.Lock()
_candados_carpetas: dict[tuple[str, str], threading.Lock] = {}


def _candado_carpeta(clave: tuple[str, str]) -> threading.Lock:
    with _guardia_carpetas:
        return _candados_carpetas.setdefault(clave, threading.Lock())


def _literal(valor: str) -> str:
    return str(valor or "").replace("\\", "\\\\").replace("'", "\\'")


def _texto_de(texto: dict[str, Any] | None) -> str:
    partes = []
    for elemento in (texto or {}).get("textElements") or []:
        partes.append(((elemento.get("textRun") or elemento.get("autoText")) or {}).get("content") or "")
    return "".join(partes)


def _aplicar_reemplazos(texto: str, valores: list[list[str]]) -> str:
    # Igual que Slides: sin distinguir mayúsculas y en el orden recibido.
    for marcador, valor in valores:
        texto = re.sub(re.escape(marcador), lambda _: valor, texto, flags=re.IGNORECASE)
    return texto


def peticiones_slides(presentacion: dict[str, Any], valores: list[list[str]], buscados: list[str]) -> list[dict[str, Any]]:
    """Reemplazo y centrado en una sola escritura de Slides."""
    peticiones: list[dict[str, Any]] = [
        {"replaceAllText": {"containsText": {"text": marcador, "matchCase": False}, "replaceText": valor}}
        for marcador, valor in valores
    ]

    def coincide(texto: dict[str, Any] | None) -> bool:
        contenido = _aplicar_reemplazos(_texto_de(texto), valores)
        return any(buscado in contenido for buscado in buscados)

    for diapositiva in presentacion.get("slides") or []:
        for elemento in diapositiva.get("pageElements") or []:
            objeto = elemento.get("objectId")
            shape = elemento.get("shape")
            if shape and coincide(shape.get("text")):
                peticiones.append({"updateParagraphStyle": {
                    "objectId": objeto, "textRange": {"type": "ALL"},
                    "style": {"alignment": "CENTER"}, "fields": "alignment",
                }})
            for fila in (elemento.get("table") or {}).get("tableRows") or []:
                for celda in fila.get("tableCells") or []:
                    if coincide(celda.get("text")):
                        peticiones.append({"updateParagraphStyle": {
                            "objectId": objeto, "cellLocation": celda.get("location") or {},
                            "textRange": {"type": "ALL"},
                            "style": {"alignment": "CENTER"}, "fields": "alignment",
                        }})
    return peticiones


def quedaron_marcadores(pdf: bytes, valores: list[list[str]]) -> bool:
    """True si el PDF aún muestra XXXX o YYYY: Drive exportó antes del cambio."""
    insertado = " ".join(valor for _, valor in valores)
    marcadores = [marcador for marcador, _ in valores if marcador and marcador not in insertado]
    try:
        with fitz.open(stream=pdf, filetype="pdf") as documento:
            texto = " ".join(pagina.get_text() for pagina in documento)
    except Exception:
        return False
    return any(marcador in texto for marcador in marcadores)


class Lote:
    def __init__(self, token: str, payload: dict[str, Any]) -> None:
        self.token = token
        self.plantilla = str(payload["plantilla_id"])
        self.raiz = str(payload["destino_raiz_id"])
        self.items = list(payload["items"])
        self.local = threading.local()
        self.guardia = threading.Lock()
        self.carpetas: dict[tuple[str, str], str] = {}
        self.candados: dict[Any, threading.Lock] = {}
        self.maestros: dict[str, str] = {}

    def candado(self, clave: Any) -> threading.Lock:
        with self.guardia:
            return self.candados.setdefault(clave, threading.Lock())

    # El cliente de Google no es seguro entre hilos: uno propio por hilo.
    def drive(self):
        if not hasattr(self.local, "drive"):
            self.local.drive = build("drive", "v3", credentials=Credentials(token=self.token), cache_discovery=False)
        return self.local.drive

    def slides(self):
        if not hasattr(self.local, "slides"):
            self.local.slides = build("slides", "v1", credentials=Credentials(token=self.token), cache_discovery=False)
        return self.local.slides

    def buscar(self, consulta: str) -> list[dict[str, Any]]:
        respuesta = self.drive().files().list(
            q=consulta, fields="files(id,name,webViewLink)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=REINTENTOS)
        return respuesta.get("files") or []

    def subcarpeta(self, padre: str, nombre: str) -> str:
        nombre = str(nombre or "").strip()
        if not nombre:
            raise ValueError("Se intentó crear una carpeta sin nombre.")
        clave = (padre, nombre.upper())
        conocida = self.carpetas.get(clave)
        if conocida:
            return conocida

        with _candado_carpeta(clave):
            conocida = self.carpetas.get(clave)
            if conocida:
                return conocida
            base = (f"'{_literal(padre)}' in parents and name = '{_literal(nombre)}' "
                    f"and mimeType = '{MIME_CARPETA}'")
            halladas = self.buscar(base + " and trashed = false")
            if halladas:
                identificador = halladas[0]["id"]
            else:
                # Igual que Apps Script: una carpeta en la papelera se recupera.
                en_papelera = self.buscar(base + " and trashed = true")
                if en_papelera:
                    identificador = en_papelera[0]["id"]
                    self.drive().files().update(
                        fileId=identificador, body={"trashed": False}, fields="id",
                        supportsAllDrives=True,
                    ).execute(num_retries=REINTENTOS)
                else:
                    identificador = self.drive().files().create(
                        body={"name": nombre, "mimeType": MIME_CARPETA, "parents": [padre]},
                        fields="id", supportsAllDrives=True,
                    ).execute(num_retries=REINTENTOS)["id"]
            self.carpetas[clave] = identificador
            return identificador

    def carpeta_de(self, ruta: list[str]) -> str:
        actual = self.raiz
        for nombre in ruta or []:
            actual = self.subcarpeta(actual, nombre)
        return actual

    def existente(self, carpeta: str, nombre: str) -> dict[str, Any] | None:
        halladas = self.buscar(
            f"'{_literal(carpeta)}' in parents and name = '{_literal(nombre)}' "
            f"and mimeType != '{MIME_CARPETA}' and trashed = false"
        )
        return halladas[0] if halladas else None

    def copiar_pdf(self, origen: str, carpeta: str, nombre: str) -> dict[str, Any]:
        return self.drive().files().copy(
            fileId=origen, body={"name": nombre, "parents": [carpeta]},
            fields="id,name,webViewLink", supportsAllDrives=True,
        ).execute(num_retries=REINTENTOS)

    def exportar(self, temporal: str, valores: list[list[str]]) -> bytes:
        for intento in range(1, INTENTOS_EXPORTAR + 1):
            pdf = self.drive().files().export_media(
                fileId=temporal, mimeType="application/pdf"
            ).execute(num_retries=REINTENTOS)
            if not quedaron_marcadores(pdf, valores):
                return pdf
            if intento < INTENTOS_EXPORTAR:
                time.sleep(ESPERA_EXPORTAR_S)
        raise RuntimeError("El PDF salió con el texto de la plantilla sin reemplazar; no se subió.")

    def generar(self, item: dict[str, Any], carpeta: str) -> dict[str, Any]:
        nombre = str(item["nombre"])
        valores = [[str(marcador), str(valor or "")] for marcador, valor in item.get("valores") or []]
        buscados = [str(texto) for texto in item.get("textos") or [] if texto]

        temporal = self.drive().files().copy(
            fileId=self.plantilla,
            body={"name": f"TEMP_{uuid.uuid4().hex[:8]}_{nombre}", "parents": [carpeta]},
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=REINTENTOS)["id"]

        try:
            presentacion = self.slides().presentations().get(
                presentationId=temporal, fields=CAMPOS_SLIDES
            ).execute(num_retries=REINTENTOS)
            peticiones = peticiones_slides(presentacion, valores, buscados)
            if peticiones:
                _ritmo_slides.esperar()
                self.slides().presentations().batchUpdate(
                    presentationId=temporal, body={"requests": peticiones}
                ).execute(num_retries=REINTENTOS)

            pdf = self.exportar(temporal, valores)
            return self.drive().files().create(
                body={"name": nombre, "parents": [carpeta]},
                media_body=MediaIoBaseUpload(io.BytesIO(pdf), mimetype="application/pdf", resumable=False),
                fields="id,name,webViewLink", supportsAllDrives=True,
            ).execute(num_retries=REINTENTOS)
        finally:
            try:
                self.drive().files().update(
                    fileId=temporal, body={"trashed": True}, fields="id", supportsAllDrives=True,
                ).execute(num_retries=REINTENTOS)
            except Exception as exc:
                log.warning("No se pudo retirar el temporal %s: %s", temporal, exc)

    def producir(self, item: dict[str, Any], carpeta: str, nombre: str) -> tuple[str, dict[str, Any]]:
        maestro = str(item.get("maestro_id") or "")
        if maestro:
            return "copiado", self.copiar_pdf(maestro, carpeta, nombre)

        clave = str(item.get("clave_reutilizable") or "")
        if not clave:
            return "creado", self.generar(item, carpeta)

        # Carátulas con el mismo nombre: la primera se genera y las demás se copian.
        with self.candado(("nombre", clave)):
            fuente = self.maestros.get(clave)
            if fuente:
                return "copiado", self.copiar_pdf(fuente, carpeta, nombre)
            creado = self.generar(item, carpeta)
            self.maestros[clave] = creado["id"]
            return "creado", creado

    def procesar(self, indice: int, item: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {"indice": indice, "clave": item.get("clave"), "name": item.get("nombre")}
        try:
            nombre = str(item.get("nombre") or "").strip()
            if not nombre:
                raise ValueError("Falta el nombre del PDF.")
            carpeta = self.carpeta_de(item.get("ruta") or [])
            with self.candado(("archivo", carpeta, nombre.upper())):
                hallado = self.existente(carpeta, nombre)
                accion, archivo = ("existente", hallado) if hallado else self.producir(item, carpeta, nombre)
            base.update(accion=accion, id=archivo.get("id"), url=archivo.get("webViewLink", ""),
                        name=archivo.get("name", nombre))
        except Exception as exc:
            log.warning("Carátula %s: %s", item.get("nombre"), exc)
            base.update(accion="error", detalle=str(exc)[:500])
        return base


def generar_lote(token: str, payload: dict[str, Any], hilos: int,
                 progreso: Callable[..., None] | None = None) -> dict[str, Any]:
    lote = Lote(token, payload)
    total = len(lote.items)
    resultados: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, hilos), thread_name_prefix="caratula") as pool:
        futuros = [pool.submit(lote.procesar, indice, item) for indice, item in enumerate(lote.items)]
        for futuro in as_completed(futuros):
            resultados.append(futuro.result())
            if progreso:
                progreso(stage="generating", processed_files=len(resultados), total_files=total)

    resultados.sort(key=lambda resultado: resultado["indice"])
    hay_errores = any(resultado.get("accion") == "error" for resultado in resultados)
    return {"status": "partial" if hay_errores else "success",
            "message": "Carátulas terminadas.", "resultados": resultados}
