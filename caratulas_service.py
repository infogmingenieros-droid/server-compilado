"""
caratulas_service.py
====================================================================
Puerto de 3_Caratulas.txt (Apps Script) a Python usando las APIs de
Drive, Docs y Slides con la cuenta de servicio.

Simplificación consciente respecto al original: el original centraba
(alineación de párrafo) los textos insertados dentro de la plantilla.
Aquí se reemplaza el texto (XXXX/XXXXX/XXXXXX o YYYY) pero NO se
replica el centrado automático de párrafo, porque requiere ubicar
rangos de texto en la API de Docs/Slides con mucha más lógica.
Si la plantilla ya tiene esos placeholders centrados por estilo de
párrafo (no por selección puntual), el resultado se ve igual.
====================================================================
"""
from __future__ import annotations

import io
import uuid
from typing import Any

from googleapiclient.http import MediaIoBaseUpload

import google_services as gs

GOOGLE_DOCS_MIME = "application/vnd.google-apps.document"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"


# ====================================================================
# obtenerModelosDeCaratula
# ====================================================================

def obtener_modelos_de_caratula(hoja: str = "CARATULAS Y COMPILADOS") -> list[dict[str, str]]:
    try:
        template_id = gs.get_id_from_cell(hoja, "C3")
    except ValueError:
        return [{"id": "", "name": "⚠️ Enlace inválido o vacío en C3"}]

    drive = gs.drive_service()
    # ¿Es una carpeta?
    try:
        meta = drive.files().get(fileId=template_id, fields="id, name, mimeType", supportsAllDrives=True).execute()
    except Exception as exc:
        return [{"id": "", "name": f"⚠️ No se pudo leer C3: {exc}"}]

    if meta["mimeType"] == "application/vnd.google-apps.folder":
        resp = drive.files().list(
            q=f"'{template_id}' in parents and trashed = false and "
              f"(mimeType = '{GOOGLE_DOCS_MIME}' or mimeType = '{GOOGLE_SLIDES_MIME}')",
            fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        modelos = [{"id": f["id"], "name": f["name"]} for f in resp.get("files", [])]
        modelos.sort(key=lambda m: gs.normalize_text(m["name"]))
        return modelos or [{"id": "", "name": "⚠️ La carpeta no contiene Google Docs ni Google Slides"}]

    if meta["mimeType"] not in (GOOGLE_DOCS_MIME, GOOGLE_SLIDES_MIME):
        return [{"id": "", "name": "⚠️ C3 no apunta a un Google Docs o Google Slides"}]

    return [{"id": meta["id"], "name": meta["name"]}]


def _obtener_archivo_plantilla(template_id: str) -> dict[str, str]:
    if not template_id:
        raise ValueError("No se proporcionó un ID de plantilla.")
    drive = gs.drive_service()
    meta = drive.files().get(fileId=template_id, fields="id, name, mimeType", supportsAllDrives=True).execute()
    if meta["mimeType"] in (GOOGLE_DOCS_MIME, GOOGLE_SLIDES_MIME):
        return meta
    if meta["mimeType"] == "application/vnd.google-apps.folder":
        resp = drive.files().list(
            q=f"'{template_id}' in parents and trashed = false and "
              f"(mimeType = '{GOOGLE_DOCS_MIME}' or mimeType = '{GOOGLE_SLIDES_MIME}')",
            fields="files(id, name, mimeType)", supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        candidatos = sorted(resp.get("files", []), key=lambda f: gs.normalize_text(f["name"]))
        if not candidatos:
            raise ValueError("La carpeta de plantillas no contiene Google Docs ni Google Slides.")
        return candidatos[0]
    raise ValueError("No se pudo abrir la plantilla.")


# ====================================================================
# crearPDFDesdePlantilla_
# ====================================================================

def _crear_pdf_desde_plantilla(
    plantilla: dict[str, str],
    texto_limpio: str,
    prefijo: str,
    texto_visual: str,
    filename_final: str,
    carpeta_destino_id: str,
    tipo_caratula: str,
) -> dict[str, str]:
    texto_mayus = str(texto_limpio or "").upper().strip()
    visual_mayus = str(texto_visual or "").upper().strip()
    prefijo_mayus = str(prefijo or "").upper().strip()

    drive = gs.drive_service()
    temporal = drive.files().copy(
        fileId=plantilla["id"],
        body={"name": f"TEMP_{uuid.uuid4().hex[:8]}_{filename_final}", "parents": [carpeta_destino_id]},
        supportsAllDrives=True, fields="id",
    ).execute()
    temp_id = temporal["id"]

    try:
        if tipo_caratula == "nueva":
            valores = {"YYYY": prefijo_mayus, "XXXXXX": texto_mayus, "XXXXX": texto_mayus, "XXXX": texto_mayus}
        else:
            valores = {"XXXXXX": visual_mayus, "XXXXX": visual_mayus, "XXXX": visual_mayus}

        if plantilla["mimeType"] == GOOGLE_SLIDES_MIME:
            requests = [
                {"replaceAllText": {"containsText": {"text": marcador, "matchCase": True}, "replaceText": valor or ""}}
                for marcador, valor in valores.items()
            ]
            gs.slides_service().presentations().batchUpdate(
                presentationId=temp_id, body={"requests": requests}
            ).execute()
        elif plantilla["mimeType"] == GOOGLE_DOCS_MIME:
            requests = [
                {"replaceAllText": {"containsText": {"text": marcador, "matchCase": True}, "replaceText": valor or ""}}
                for marcador, valor in valores.items()
            ]
            gs.docs_service().documents().batchUpdate(
                documentId=temp_id, body={"requests": requests}
            ).execute()
        else:
            raise ValueError("La plantilla debe ser Google Docs o Google Slides.")

        pdf_bytes = drive.files().export(fileId=temp_id, mimeType="application/pdf").execute()

        nombre = gs.sanitize_filename(filename_final)
        if not nombre.lower().endswith(".pdf"):
            nombre += ".pdf"

        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
        nuevo = drive.files().create(
            body={"name": nombre, "parents": [carpeta_destino_id]},
            media_body=media, fields="id, name, webViewLink", supportsAllDrives=True,
        ).execute()

        _eliminar_duplicados_nombre_excepto(carpeta_destino_id, nombre, nuevo["id"])

        return {"id": nuevo["id"], "url": nuevo.get("webViewLink", ""), "name": nuevo["name"]}
    finally:
        try:
            drive.files().update(fileId=temp_id, body={"trashed": True}, supportsAllDrives=True).execute()
        except Exception:
            pass


def _eliminar_duplicados_nombre_excepto(carpeta_id: str, nombre_archivo: str, file_id_conservar: str) -> int:
    drive = gs.drive_service()
    escaped = nombre_archivo.replace("\\", "\\\\").replace("'", "\\'")
    resp = drive.files().list(
        q=f"'{carpeta_id}' in parents and name = '{escaped}' and trashed = false",
        fields="files(id, name, createdTime)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    eliminados = 0
    for f in resp.get("files", []):
        if f["id"] == file_id_conservar:
            continue
        try:
            drive.files().update(fileId=f["id"], body={"trashed": True}, supportsAllDrives=True).execute()
            eliminados += 1
        except Exception:
            pass
    return eliminados


def _existe_archivo(carpeta_id: str, nombre_archivo: str) -> dict[str, str] | None:
    drive = gs.drive_service()
    escaped = nombre_archivo.replace("\\", "\\\\").replace("'", "\\'")
    resp = drive.files().list(
        q=f"'{carpeta_id}' in parents and name = '{escaped}' and trashed = false",
        fields="files(id, name, webViewLink)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def _fila_log(origen: str, archivo: str, estado: str, url: str, file_id: str) -> list[Any]:
    import datetime
    return [datetime.datetime.now().isoformat(sep=" ", timespec="seconds"), origen, archivo, estado, url, file_id]


# ====================================================================
# procesarSeleccionados
# ====================================================================

def procesar_seleccionados(lote: list[dict[str, str]], config_ubicacion: dict, config_caratula: dict) -> dict:
    if not lote:
        raise ValueError("No se recibieron carpetas para procesar.")

    hoja = "CARATULAS Y COMPILADOS"
    id_origen = gs.get_id_from_cell(hoja, "C2")
    id_destino = gs.get_id_from_cell(hoja, "C4")

    ubicacion = config_ubicacion or {}
    caratula = config_caratula or {}
    solo_carpetas = bool(ubicacion.get("soloCarpetas"))
    tipo = "nueva" if caratula.get("tipo") == "nueva" else "original"

    plantilla = None
    if not solo_carpetas:
        id_plantilla = caratula.get("idPlantilla") or gs.get_id_from_cell(hoja, "C3")
        plantilla = _obtener_archivo_plantilla(id_plantilla)

    cache_carpetas: dict[str, str] = {}
    logs: list[list[Any]] = []
    resultado = {
        "status": "success", "procesados": 0, "carpetasPreparadas": 0,
        "pdfsCreados": 0, "omitidos": 0, "errores": [], "registrosAgregados": 0,
    }

    for item in lote:
        resultado["procesados"] += 1
        try:
            if not item or not item.get("id") or not item.get("name"):
                raise ValueError("Elemento seleccionado incompleto.")

            carpeta_guardar_id = id_destino
            ruta = gs.path_from_root(item["id"], id_origen)

            if ubicacion.get("tipo") == "automatico":
                for nombre_ruta in ruta:
                    carpeta_guardar_id = gs.get_or_create_folder(carpeta_guardar_id, nombre_ruta, cache_carpetas)["id"]
                carpeta_guardar_id = gs.get_or_create_folder(carpeta_guardar_id, item["name"], cache_carpetas)["id"]
            else:
                nombre_manual = str(ubicacion.get("nombreNuevaCarpeta") or "").strip()
                if nombre_manual:
                    carpeta_guardar_id = gs.get_or_create_folder(
                        carpeta_guardar_id, gs.sanitize_filename(nombre_manual.upper()), cache_carpetas
                    )["id"]

            resultado["carpetasPreparadas"] += 1

            if solo_carpetas:
                meta = gs.get_folder_meta(carpeta_guardar_id)
                logs.append(_fila_log(
                    "Panel Web - Carátulas", f"CARPETA: {item['name']}",
                    "📁 Carpeta preparada o verificada", meta.get("webViewLink", ""), carpeta_guardar_id,
                ))
                continue

            texto_visual = gs.get_visual_text(item["name"])
            prefijo = gs.extract_advanced_prefix(item["name"])
            texto_limpio = gs.clean_text_without_prefix(item["name"])
            filename_final = gs.sanitize_filename(item["name"].strip().upper()) + ".PDF"

            if ruta:
                primer_nivel = gs.normalize_text(ruta[0])
                es_anexo_especial = "anexo 11" in primer_nivel or "anexo 13" in primer_nivel
                if es_anexo_especial and len(ruta) == 1 and len(texto_limpio) >= 9:
                    texto_limpio = texto_limpio[-9:]
                elif es_anexo_especial and len(ruta) == 2 and len(texto_limpio) > 3:
                    texto_limpio = texto_limpio[3:].strip()

            existente = _existe_archivo(carpeta_guardar_id, filename_final)
            if existente:
                resultado["omitidos"] += 1
                logs.append(_fila_log(
                    "Panel Web - Carátulas", filename_final, "⏭️ Carátula omitida: ya existía",
                    existente.get("webViewLink", ""), existente["id"],
                ))
                continue

            pdf_creado = _crear_pdf_desde_plantilla(
                plantilla, texto_limpio, prefijo, texto_visual, filename_final, carpeta_guardar_id, tipo,
            )
            resultado["pdfsCreados"] += 1
            logs.append(_fila_log(
                "Panel Web - Carátulas", pdf_creado["name"] or filename_final, "✅ Carátula creada",
                pdf_creado["url"], pdf_creado["id"],
            ))

        except Exception as error:
            nombre_error = item.get("name", "Sin nombre") if item else "Sin nombre"
            resultado["errores"].append({"item": nombre_error, "detalle": str(error)})
            logs.append(_fila_log("Panel Web - Carátulas", nombre_error, f"❌ Error al procesar: {error}", "", ""))

    resultado["registrosAgregados"] = gs.append_log_rows(hoja, logs)

    if resultado["errores"]:
        resultado["status"] = "partial" if (resultado["pdfsCreados"] or resultado["carpetasPreparadas"]) else "error"

    partes = [
        f"Carpetas procesadas: {resultado['procesados']}",
        f"PDF creados: {resultado['pdfsCreados']}",
        f"Omitidos por existir: {resultado['omitidos']}",
        f"Actividades registradas: {resultado['registrosAgregados']}",
    ]
    if resultado["errores"]:
        partes.append(f"Errores: {len(resultado['errores'])}")
    prefijo_msg = "⚠️ Proceso completado con observaciones. " if resultado["errores"] else "✅ Proceso completado. "
    resultado["mensaje"] = prefijo_msg + " | ".join(partes)
    return resultado


# ====================================================================
# restaurarCaratulasBase
# ====================================================================

_GRUPOS_BASE = [
    {
        "carpeta": "CARATULAS ANEXO 11 (NO BORRAR)",
        "nombres": [
            "1. MEMORIA DIAGNOSTICO TÉCNICO LEGAL",
            "1.1 PLAN DE SANEAMIENTO FISICO-LEGAL",
            "2. PLANOS DIAGNOSTICO TÉCNICO LEGAL",
            "3. CERTIFICADO DE BUSQUEDA CATASTRAL",
            "5. INFORME TÉCNICO",
        ],
    },
    {
        "carpeta": "CARATULAS ANEXO 13 (NO BORRAR)",
        "nombres": [
            "1. FICHA SOCIOECONÓMICA",
            "2. FICHA TÉCNICA",
            "3. MEMORIA DESCRIPTIVA",
            "4. PLANOS",
            "5. DOC. DEL SUJETO PASIVO",
            "5.1. FICHA RENIEC",
            "5.1. FICHA RUC",
            "5.2. CONSTANCIA DE POSESIÓN",
            "5.2. DECLARACIÓN JURADA",
            "5.2. PARTIDA REGISTRAL",
            "6. INFORME TÉCNICO DE TASACIÓN",
        ],
    },
]


def restaurar_caratulas_base(id_plantilla_elegida: str | None, tipo_caratula_elegido: str | None) -> dict:
    hoja = "CARATULAS Y COMPILADOS"
    id_destino = gs.get_id_from_cell(hoja, "C4")
    id_plantilla = id_plantilla_elegida or gs.get_id_from_cell(hoja, "C3")
    plantilla = _obtener_archivo_plantilla(id_plantilla)
    tipo = "nueva" if tipo_caratula_elegido == "nueva" else "original"

    cache: dict[str, str] = {}
    logs: list[list[Any]] = []
    creadas = 0
    omitidas = 0
    errores: list[str] = []

    for grupo in _GRUPOS_BASE:
        carpeta_id = gs.get_or_create_folder(id_destino, grupo["carpeta"], cache)["id"]
        for nombre_item in grupo["nombres"]:
            filename = gs.sanitize_filename(nombre_item.upper()) + ".PDF"
            existente = _existe_archivo(carpeta_id, filename)
            if existente:
                omitidas += 1
                logs.append(_fila_log(
                    "Panel Web - Restauración", filename, "⏭️ Carátula base omitida: ya existía",
                    existente.get("webViewLink", ""), existente["id"],
                ))
                continue
            try:
                pdf_creado = _crear_pdf_desde_plantilla(
                    plantilla, gs.clean_text_without_prefix(nombre_item), gs.extract_advanced_prefix(nombre_item),
                    gs.get_visual_text(nombre_item), filename, carpeta_id, tipo,
                )
                creadas += 1
                logs.append(_fila_log(
                    "Panel Web - Restauración", pdf_creado["name"] or filename, "✅ Carátula base restaurada",
                    pdf_creado["url"], pdf_creado["id"],
                ))
            except Exception as error:
                errores.append(f"{nombre_item}: {error}")
                logs.append(_fila_log("Panel Web - Restauración", filename, f"❌ Error al restaurar: {error}", "", ""))

    registros = gs.append_log_rows(hoja, logs)
    status = ("partial" if creadas else "error") if errores else "success"
    prefijo_msg = "⚠️ Restauración completada con observaciones. " if errores else "✅ Restauración completada. "
    return {
        "status": status, "creadas": creadas, "omitidas": omitidas, "errores": errores,
        "registrosAgregados": registros,
        "mensaje": (prefijo_msg + f"Creadas: {creadas} | Omitidas por existir: {omitidas} | "
                    f"Errores: {len(errores)} | Actividades registradas: {registros}"),
    }


def crear_carpeta_libre(nombre_carpeta: str) -> dict:
    nombre = gs.sanitize_filename(str(nombre_carpeta or "").strip().upper())
    if not nombre:
        raise ValueError("El nombre de la carpeta está vacío.")

    hoja = "CARATULAS Y COMPILADOS"
    id_destino = gs.get_id_from_cell(hoja, "C4")
    carpeta = gs.get_or_create_folder(id_destino, nombre, {})
    meta = gs.get_folder_meta(carpeta["id"])

    gs.append_log_rows(hoja, [_fila_log(
        "Panel Web - Carátulas", f"CARPETA: {nombre}", "📁 Carpeta creada o verificada",
        meta.get("webViewLink", ""), carpeta["id"],
    )])

    return {
        "status": "success", "carpetaId": carpeta["id"], "carpetaUrl": meta.get("webViewLink", ""),
        "mensaje": "✅ Carpeta creada o encontrada correctamente.",
    }
