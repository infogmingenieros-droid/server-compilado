"""
google_services.py
====================================================================
Reemplaza, usando una cuenta de servicio de Google, lo que en el
sistema original hacía Apps Script con SpreadsheetApp / DriveApp.

Helpers portados 1:1 (en espíritu) desde 2_Utilidades.txt:
  - extraerIdDeCeldaSegura / obtenerIdDesdeHoja  -> get_id_from_cell
  - obtenerSubcarpetas                            -> list_subfolders
  - getOrCreateFolder                             -> get_or_create_folder
  - normalizarTexto / simplificar                 -> normalize_text / simplify
  - limpiarNombrePDF / extraerPrefijoAvanzado ...  -> funciones homónimas
  - obtenerRutaDesdeOrigen                        -> path_from_root
  - filtrarSeleccionadosMasEspecificos             -> filter_most_specific
====================================================================
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from functools import lru_cache
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
]

SHEET_ID = os.getenv("SHEET_ID", "").strip()


def _load_credentials() -> service_account.Credentials:
    """
    Carga la cuenta de servicio desde la variable de entorno
    GOOGLE_SERVICE_ACCOUNT_JSON (el JSON completo, tal cual lo descarga
    Google Cloud Console) o desde GOOGLE_SERVICE_ACCOUNT_FILE (ruta a
    ese mismo archivo, útil en local).
    """
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if path:
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

    raise RuntimeError(
        "Falta configurar GOOGLE_SERVICE_ACCOUNT_JSON (o GOOGLE_SERVICE_ACCOUNT_FILE) "
        "con las credenciales de la cuenta de servicio."
    )


@lru_cache(maxsize=1)
def _credentials() -> service_account.Credentials:
    return _load_credentials()


@lru_cache(maxsize=1)
def sheets_service():
    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def drive_service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def docs_service():
    return build("docs", "v1", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def slides_service():
    return build("slides", "v1", credentials=_credentials(), cache_discovery=False)


# ====================================================================
# LECTURA / ESCRITURA DE CELDAS (equivalente a obtenerHojaSegura, etc.)
# ====================================================================

def get_cell_display_value(hoja: str, celda: str) -> str:
    """Lee el valor visible de una celda. Equivale a range.getDisplayValue()."""
    rango = f"'{hoja}'!{celda}"
    resp = sheets_service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=rango, valueRenderOption="FORMATTED_VALUE"
    ).execute()
    valores = resp.get("values", [])
    return str(valores[0][0]) if valores and valores[0] else ""


def get_cell_formula(hoja: str, celda: str) -> str:
    rango = f"'{hoja}'!{celda}"
    resp = sheets_service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=rango, valueRenderOption="FORMULA"
    ).execute()
    valores = resp.get("values", [])
    return str(valores[0][0]) if valores and valores[0] else ""


_ID_PATTERNS = [
    re.compile(r"/folders/([A-Za-z0-9_-]{20,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{20,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{20,})"),
    re.compile(r'HYPERLINK\s*\(\s*["\'].*?([A-Za-z0-9_-]{20,})', re.IGNORECASE),
]


def extract_google_id(contenido: str | None) -> str | None:
    """Puerto de extraerIdGoogle_."""
    if not contenido:
        return None
    texto = str(contenido).strip()
    if not texto:
        return None

    for patron in _ID_PATTERNS:
        m = patron.search(texto)
        if m:
            return m.group(1)

    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", texto):
        return texto

    if re.search(r"drive\.google\.com|docs\.google\.com", texto, re.IGNORECASE):
        m = re.search(r"([A-Za-z0-9_-]{25,})", texto)
        if m:
            return m.group(1)

    return None


def get_id_from_cell(hoja: str, celda: str) -> str:
    """Puerto de obtenerIdDesdeHoja: prueba fórmula (por si es un HYPERLINK) y valor mostrado."""
    for valor in (get_cell_formula(hoja, celda), get_cell_display_value(hoja, celda)):
        gid = extract_google_id(valor)
        if gid:
            return gid
    raise ValueError(f"No se encontró un enlace o ID válido en {hoja}!{celda}.")


def append_log_rows(hoja: str, filas: list[list[Any]]) -> int:
    """
    Equivalente a escribirLogsCaratulas_ / escribirLogsCompilador_:
    agrega filas a partir de la fila 15 (o la siguiente libre) en A:F.
    """
    if not filas:
        return 0
    sheet = sheets_service().spreadsheets()
    # Determinar la última fila usada en la columna A
    resp = sheet.values().get(spreadsheetId=SHEET_ID, range=f"'{hoja}'!A:A").execute()
    ultima_fila = len(resp.get("values", []))
    inicio = max(15, ultima_fila + 1)
    rango = f"'{hoja}'!A{inicio}:F{inicio + len(filas) - 1}"
    sheet.values().update(
        spreadsheetId=SHEET_ID, range=rango, valueInputOption="USER_ENTERED",
        body={"values": filas},
    ).execute()
    return len(filas)


def read_table(hoja: str, rango: str) -> list[list[str]]:
    resp = sheets_service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{hoja}'!{rango}", valueRenderOption="FORMATTED_VALUE"
    ).execute()
    return resp.get("values", [])


# ====================================================================
# TEXTO (normalizarTexto, simplificar, limpiarNombrePDF, ...)
# ====================================================================

def normalize_text(texto: Any) -> str:
    if texto is None:
        return ""
    s = str(texto).lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip()


def simplify(texto: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_text(texto))


def clean_pdf_name(nombre: str | None) -> str:
    if not nombre:
        return ""
    s = re.sub(r"\.pdf$", "", str(nombre), flags=re.IGNORECASE)
    s = re.sub(r"^(?:anexo\s+\d+(?:\.\d+)*\.?|\d+(?:\.\d+)*\.?)\s*[-–—.:]*\s*", "", s, flags=re.IGNORECASE)
    return s.strip()


def extract_advanced_prefix(nombre: str | None) -> str:
    if not nombre:
        return ""
    m = re.match(r"^(anexo\s+\d+(?:\.\d+)*\.?|\d+(?:\.\d+)*\.?)\s*", str(nombre), re.IGNORECASE)
    return m.group(1).strip() if m else ""


def clean_text_without_prefix(nombre: str | None) -> str:
    if not nombre:
        return ""
    s = re.sub(r"^(anexo\s+\d+(?:\.\d+)*\.?|\d+(?:\.\d+)*\.?)\s*[-–—.:]*\s*", "", str(nombre), flags=re.IGNORECASE)
    return s.strip()


def get_visual_text(nombre: str | None) -> str:
    if not nombre:
        return ""
    texto = str(nombre).strip()
    m = re.match(r"^(ANEXO\s*\d+(?:\.\d+)*\.?)\s*[-–—.:]*\s*(.*)", texto, re.IGNORECASE)
    if m:
        resto = m.group(2)
        return m.group(1).upper() + ("\n" + resto if resto else "")
    m2 = re.match(r"^(\d+(?:\.\d+)*\.?)\s*[-–—.:]*\s*(.*)", texto)
    return m2.group(2) if m2 else texto


def sanitize_filename(nombre: str | None) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", str(nombre or ""))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ====================================================================
# DRIVE: carpetas, rutas, filtros de selección
# ====================================================================

def list_subfolders(folder_id: str) -> list[dict[str, str]]:
    """Puerto de obtenerSubcarpetas."""
    salida: list[dict[str, str]] = []
    page_token = None
    q = f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    while True:
        resp = drive_service().files().list(
            q=q, fields="nextPageToken, files(id, name)",
            pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            salida.append({"id": f["id"], "name": f["name"]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    salida.sort(key=lambda x: normalize_text(x["name"]))
    return salida


def get_or_create_folder(parent_id: str, name: str, cache: dict[str, str] | None = None) -> dict[str, str]:
    """Puerto de getOrCreateFolder. cache evita relecturas repetidas en un mismo lote."""
    nombre = str(name or "").strip()
    if not nombre:
        raise ValueError("Se intentó crear una carpeta sin nombre.")

    cache = cache if cache is not None else {}
    cache_key = f"{parent_id}::{nombre.upper()}"
    if cache_key in cache:
        return {"id": cache[cache_key], "name": nombre}

    escaped = nombre.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"'{parent_id}' in parents and name = '{escaped}' and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    resp = drive_service().files().list(
        q=q, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if files:
        cache[cache_key] = files[0]["id"]
        return {"id": files[0]["id"], "name": files[0]["name"]}

    creada = drive_service().files().create(
        body={"name": nombre, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id, name", supportsAllDrives=True,
    ).execute()
    cache[cache_key] = creada["id"]
    return {"id": creada["id"], "name": creada["name"]}


def get_folder_meta(folder_id: str) -> dict[str, str]:
    return drive_service().files().get(
        fileId=folder_id, fields="id, name, webViewLink, mimeType", supportsAllDrives=True
    ).execute()


def get_parents(file_id: str) -> list[str]:
    meta = drive_service().files().get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    return meta.get("parents", []) or []


def path_from_root(folder_id: str, root_id: str) -> list[str]:
    """Puerto de obtenerRutaDesdeOrigen: nombres de carpetas entre root y folder_id (sin incluir ninguno de los dos extremos)."""
    ruta: list[str] = []
    actual_id = folder_id
    visitados = set()

    if actual_id == root_id:
        return ruta

    while True:
        if actual_id in visitados:
            break
        visitados.add(actual_id)
        parents = get_parents(actual_id)
        if not parents:
            break
        padre_id = parents[0]
        if padre_id == root_id:
            break
        padre_meta = get_folder_meta(padre_id)
        ruta.insert(0, padre_meta["name"])
        actual_id = padre_id

    return ruta


def filter_most_specific(seleccionados: list[dict[str, str]]) -> list[dict[str, str]]:
    """Puerto de filtrarSeleccionadosMasEspecificos: si se seleccionó una carpeta padre
    Y una hija dentro de ella, se descarta el padre."""
    if not seleccionados:
        return []

    ids = {item["id"] for item in seleccionados if item.get("id")}
    padres_seleccionados: set[str] = set()

    for item in seleccionados:
        if not item.get("id"):
            continue
        actual_id = item["id"]
        for _ in range(100):
            parents = get_parents(actual_id)
            if not parents:
                break
            padre_id = parents[0]
            if padre_id in ids:
                padres_seleccionados.add(padre_id)
            actual_id = padre_id

    return [item for item in seleccionados if item.get("id") and item["id"] not in padres_seleccionados]


def list_pdfs_in_folder(folder_id: str) -> list[dict[str, str]]:
    salida: list[dict[str, str]] = []
    page_token = None
    q = f"'{folder_id}' in parents and mimeType = 'application/pdf' and trashed = false"
    while True:
        resp = drive_service().files().list(
            q=q, fields="nextPageToken, files(id, name)",
            pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        salida.extend({"id": f["id"], "name": f["name"]} for f in resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    salida.sort(key=lambda x: normalize_text(x["name"]))
    return salida


def list_pdfs_recursive(folder_id: str) -> list[dict[str, str]]:
    """Puerto (simplificado) de extraerTodasLasCaratulas_ / recorrido recursivo de PDFs."""
    salida = list_pdfs_in_folder(folder_id)
    for sub in list_subfolders(folder_id):
        salida.extend(list_pdfs_recursive(sub["id"]))
    salida.sort(key=lambda x: normalize_text(x["name"]))
    return salida
