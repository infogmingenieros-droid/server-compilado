"""
compilador_service.py
====================================================================
Puerto de 4_Compilador.txt. A diferencia del original (que en Apps
Script llamaba por HTTP a un servidor externo), aquí corremos en el
MISMO proceso Flask que ya tiene compile_in_parts() (ver app.py), así
que armamos la secuencia de archivos exactamente igual que el
original y luego llamamos directo a esa función — sin red de por
medio, sin polling de trabajos asíncronos.
====================================================================
"""
from __future__ import annotations

import datetime
from typing import Any

import google_services as gs

CONFIG = {
    "LIMITE_PAGINAS": 500,
    "PETICIONES_PARALELAS": 2,
    "MODO_ESTRICTO_COMPILADOR": True,
}


class CaratulaCache(list):
    """Lista de carátulas compatible con el código existente + índices O(1)."""

    def __init__(self, items: list[dict]):
        super().__init__(items)
        self.by_clean: dict[str, dict] = {}
        self.by_name: dict[str, dict] = {}
        for item in items:
            name_norm = item.get("nameNorm", "")
            clean_norm = item.get("cleanNorm", "")
            if name_norm and name_norm not in self.by_name:
                self.by_name[name_norm] = item
            if clean_norm and clean_norm not in self.by_clean:
                self.by_clean[clean_norm] = item


# ====================================================================
# Carátulas especiales / búsqueda de carátulas (buscarCaratulaPorNombreExactoContiene_, etc.)
# ====================================================================

def _buscar_caratula_contiene(cache_caratulas: list[dict], patron: str) -> dict | None:
    patron_norm = gs.normalize_text(patron)
    for c in cache_caratulas:
        if patron_norm in c.get("nameNorm", ""):
            return c
    return None


def _obtener_caratula_especial(nombre_archivo: str, cache_caratulas: list[dict]) -> dict | None:
    texto = gs.simplify(nombre_archivo)
    if "reniec" in texto or "dni" in texto:
        return _buscar_caratula_contiene(cache_caratulas, "reniec")
    if "ruc" in texto:
        return _buscar_caratula_contiene(cache_caratulas, "ruc")
    if "declaracion" in texto or "jurada" in texto:
        return _buscar_caratula_contiene(cache_caratulas, "declaracion jurada")
    if "partida" in texto or "registral" in texto:
        return _buscar_caratula_contiene(cache_caratulas, "partida registral")
    if "constancia" in texto or "posesion" in texto:
        return _buscar_caratula_contiene(cache_caratulas, "constancia de posesion")
    return None


def _extraer_todas_las_caratulas(carpeta_id: str) -> CaratulaCache:
    pdfs = gs.list_pdfs_recursive(carpeta_id)
    lista = []
    for p in pdfs:
        name = p["name"]
        lista.append({
            "id": p["id"],
            "name": name,
            "nameNorm": gs.normalize_text(name),
            "cleanNorm": gs.normalize_text(gs.clean_pdf_name(name)),
        })
    lista.sort(key=lambda x: x["nameNorm"])
    return CaratulaCache(lista)


def _encontrar_caratula_por_carpeta(nombre_carpeta: str, lista_caratulas: list[dict], permitir_parcial: bool) -> dict | None:
    nombre_norm = gs.normalize_text(gs.clean_pdf_name(nombre_carpeta))
    if not nombre_norm:
        return None

    # Coincidencia exacta O(1) cuando usamos CaratulaCache.
    indice = getattr(lista_caratulas, "by_clean", None)
    if indice is not None:
        exacta = indice.get(nombre_norm)
        if exacta:
            return exacta
    else:
        # Compatibilidad con listas normales si alguna llamada externa las usa.
        for c in lista_caratulas:
            clean_norm = c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"]))
            if clean_norm == nombre_norm:
                return c

    if permitir_parcial:
        for c in lista_caratulas:
            cn = c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"]))
            if cn and (cn in nombre_norm or nombre_norm in cn):
                return c
    return None


# ====================================================================
# Determinar primer orden / grupo anexo
# ====================================================================

def _determinar_primer_orden(sel: dict, ruta: list[str], id_origen: str) -> str:
    if ruta:
        return gs.sanitize_filename(ruta[0].upper())
    nombre_sel = str(sel.get("name", "")).upper().strip()
    if "ANEXO" in nombre_sel:
        return gs.sanitize_filename(nombre_sel)
    meta = gs.get_folder_meta(id_origen)
    return gs.sanitize_filename(meta["name"].upper()) or "COMPILADOS"


def _determinar_grupo_anexo(primer_orden: str) -> str:
    texto = gs.normalize_text(primer_orden)
    if "anexo 11" in texto:
        return "ANEXO 11"
    if "anexo 13" in texto:
        return "ANEXO 13"
    return ""


def _buscar_caratula_macro_anexo(grupo: str, primer_orden: str, caratulas: list[dict]) -> dict | None:
    exacta = _encontrar_caratula_por_carpeta(primer_orden, caratulas, False)
    if exacta:
        return exacta
    grupo_norm = gs.normalize_text(grupo)
    candidatas = [
        c for c in caratulas
        if (c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"]))).startswith(grupo_norm)
        and "compilado" not in (c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"])))
        and "caratula tomo" not in (c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"])))
    ]
    candidatas.sort(key=lambda c: (-len(c.get("cleanNorm") or gs.normalize_text(gs.clean_pdf_name(c["name"]))), c["nameNorm"]))
    return candidatas[0] if candidatas else None


def _es_primer_codigo_real_anexo_especial(folder_id: str, id_origen: str, grupo: str) -> bool:
    try:
        actual_id = folder_id
        hijo_directo_id = folder_id
        carpeta_anexo_id = None

        for _ in range(100):
            parents = gs.get_parents(actual_id)
            if not parents:
                break
            padre_id = parents[0]
            if padre_id == id_origen:
                carpeta_anexo_id = actual_id
                break
            hijo_directo_id = actual_id
            actual_id = padre_id

        if not carpeta_anexo_id:
            return False
        meta_anexo = gs.get_folder_meta(carpeta_anexo_id)
        if _determinar_grupo_anexo(meta_anexo["name"]) != grupo:
            return False
        if carpeta_anexo_id == folder_id:
            return False

        hijos = []
        for sub in gs.list_subfolders(carpeta_anexo_id):
            nn = gs.normalize_text(sub["name"])
            if "caratula" in nn or "no borrar" in nn:
                continue
            hijos.append(sub)
        hijos.sort(key=lambda h: gs.normalize_text(h["name"]))
        if not hijos:
            return False

        import re
        numerados = [h for h in hijos if re.match(r"^(?:codigo\s*)?0*\d+(?:\.\d+)*\b", h["name"].strip(), re.IGNORECASE)]
        primer_hijo = numerados[0] if numerados else hijos[0]
        return primer_hijo["id"] == hijo_directo_id
    except Exception:
        return False


# ====================================================================
# Secuencias: genérica, Anexo 11, Anexo 13
# ====================================================================

def _ordenar_por_nombre(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: gs.normalize_text(x["name"]))


def _rastrear_generico(folder_id: str, is_root: bool, caratulas: list[dict]) -> list[dict]:
    secuencia = []
    files = _ordenar_por_nombre(gs.list_pdfs_in_folder(folder_id))
    for f in files:
        secuencia.append({"id": f["id"], "name": f["name"], "type": "Original"})

    subs = sorted(gs.list_subfolders(folder_id), key=lambda x: gs.normalize_text(x["name"]))
    for sub in subs:
        secuencia.extend(_rastrear_generico(sub["id"], False, caratulas))

    if not is_root and secuencia:
        meta = gs.get_folder_meta(folder_id)
        cover = _encontrar_caratula_por_carpeta(meta["name"], caratulas, False)
        if cover:
            secuencia.insert(0, {"id": cover["id"], "name": cover["name"], "type": "Carátula Carpeta"})
    return secuencia


def _rastrear_anexo13(folder_id: str, is_root: bool, in_sujeto_pasivo: bool, caratulas: list[dict]) -> list[dict]:
    import re
    secuencia = []
    meta = gs.get_folder_meta(folder_id)
    nombre_norm = gs.normalize_text(meta["name"])
    es_sujeto_pasivo = (in_sujeto_pasivo or "sujeto pasivo" in nombre_norm
                         or bool(re.match(r"^5(?:[.\s]|$)", nombre_norm)) or "5.0" in nombre_norm)

    originales = _ordenar_por_nombre(gs.list_pdfs_in_folder(folder_id))
    especiales_usadas: set[str] = set()
    for f in originales:
        if es_sujeto_pasivo:
            especial = _obtener_caratula_especial(f["name"], caratulas)
            if especial and especial["id"] not in especiales_usadas:
                secuencia.append({"id": especial["id"], "name": especial["name"], "type": "Carátula Interna Específica"})
                especiales_usadas.add(especial["id"])
        secuencia.append({"id": f["id"], "name": f["name"], "type": "Original"})

    subs = sorted(gs.list_subfolders(folder_id), key=lambda x: gs.normalize_text(x["name"]))
    for sub in subs:
        secuencia.extend(_rastrear_anexo13(sub["id"], False, es_sujeto_pasivo, caratulas))

    if not is_root and secuencia:
        cover = _encontrar_caratula_por_carpeta(meta["name"], caratulas, True)
        if cover and cover["id"] not in especiales_usadas:
            secuencia.insert(0, {"id": cover["id"], "name": cover["name"], "type": "Carátula General de Carpeta"})
    return secuencia


def _agregar_bloque(secuencia: list[dict], files: list[dict], caratulas: list[dict], patron_caratula: str) -> None:
    if not files:
        return
    cover = _buscar_caratula_contiene(caratulas, patron_caratula)
    if cover:
        secuencia.append({"id": cover["id"], "name": cover["name"], "type": "Carátula Bloque"})
    for f in files:
        secuencia.append({"id": f["id"], "name": f["name"], "type": "Original"})


def _construir_secuencia_anexo11(origen_id: str, caratulas: list[dict]) -> dict:
    encontrados = []
    existe_carpeta_cbc = False

    def recorrer(folder_id: str, contexto: dict):
        nonlocal existe_carpeta_cbc
        meta = gs.get_folder_meta(folder_id)
        nombre = gs.normalize_text(meta["name"])
        nuevo_contexto = {
            "cbc": contexto["cbc"] or "cbc" in nombre or "certificado" in nombre or "catastral" in nombre,
            "informe": contexto["informe"] or "informe" in nombre,
        }
        if nuevo_contexto["cbc"]:
            existe_carpeta_cbc = True
        for f in gs.list_pdfs_in_folder(folder_id):
            encontrados.append({"file": f, "contexto": nuevo_contexto})
        for sub in gs.list_subfolders(folder_id):
            recorrer(sub["id"], nuevo_contexto)

    recorrer(origen_id, {"cbc": False, "informe": False})

    bloques: dict[str, list[dict]] = {"b1": [], "b2": [], "b3": [], "b4": [], "b5": []}
    for item in encontrados:
        nombre = gs.simplify(item["file"]["name"])
        if "diagnosticotecnicolegal" in nombre or "fichadediagnostico" in nombre:
            bloques["b1"].append(item["file"])
        elif "plandesaneamiento" in nombre:
            bloques["b2"].append(item["file"])
        elif item["contexto"]["cbc"] or "certificadodebusqueda" in nombre:
            bloques["b4"].append(item["file"])
        elif item["contexto"]["informe"] or "informetecnico" in nombre:
            bloques["b5"].append(item["file"])
        else:
            bloques["b3"].append(item["file"])

    bloques["b1"] = _ordenar_por_nombre(bloques["b1"])
    bloques["b2"] = _ordenar_por_nombre(bloques["b2"])
    bloques["b5"] = _ordenar_por_nombre(bloques["b5"])

    import re
    planos_normales, planos_pp = [], []
    for f in bloques["b3"]:
        sin_numero = re.sub(r"^\d+", "", gs.simplify(f["name"]))
        (planos_pp if sin_numero.startswith("pp") else planos_normales).append(f)
    bloques["b3"] = _ordenar_por_nombre(planos_normales) + _ordenar_por_nombre(planos_pp)

    bloques["b4"].sort(key=lambda f: (0 if "general" in gs.normalize_text(f["name"]) else 1, gs.normalize_text(f["name"])))

    secuencia: list[dict] = []
    _agregar_bloque(secuencia, bloques["b1"], caratulas, "memoria diagnostico")
    _agregar_bloque(secuencia, bloques["b2"], caratulas, "plan de saneamiento")
    _agregar_bloque(secuencia, bloques["b3"], caratulas, "planos diagnostico")
    _agregar_bloque(secuencia, bloques["b4"], caratulas, "certificado de busqueda")
    _agregar_bloque(secuencia, bloques["b5"], caratulas, "informe tecnico")

    alerta = "⚠️ Carpeta de Certificado Catastral vacía" if existe_carpeta_cbc and not bloques["b4"] else ""
    return {"archivos": secuencia, "alerta": alerta}


def _eliminar_duplicados_por_id(secuencia: list[dict]) -> list[dict]:
    salida, vistos = [], set()
    for item in secuencia or []:
        if not item or not item.get("id") or item["id"] in vistos:
            continue
        vistos.add(item["id"])
        salida.append(item)
    return salida


# ====================================================================
# obtenerDatosParaCompilar
# ====================================================================

def obtener_datos_para_compilar(seleccionados: list[dict]) -> dict:
    hoja = "CARATULAS Y COMPILADOS"
    id_c2 = gs.get_id_from_cell(hoja, "C2")
    id_c4 = gs.get_id_from_cell(hoja, "C4")
    id_c6 = gs.get_id_from_cell(hoja, "C6")

    filtrados = gs.filter_most_specific(seleccionados)
    if not filtrados:
        raise ValueError("No quedaron carpetas válidas después de eliminar selecciones duplicadas padre/hijo.")
    filtrados.sort(key=lambda x: gs.normalize_text(x["name"]))

    caratulas = _extraer_todas_las_caratulas(id_c4)
    compilaciones = []
    caratulas_macro_usadas: set[str] = set()

    for sel in filtrados:
        ruta = gs.path_from_root(sel["id"], id_c2)
        primer_orden = _determinar_primer_orden(sel, ruta, id_c2)
        grupo = _determinar_grupo_anexo(primer_orden)
        secuencia: list[dict] = []
        alertas: list[str] = []

        if grupo and grupo not in caratulas_macro_usadas and _es_primer_codigo_real_anexo_especial(sel["id"], id_c2, grupo):
            macro = _buscar_caratula_macro_anexo(grupo, primer_orden, caratulas)
            if macro:
                secuencia.append({"id": macro["id"], "name": macro["name"], "type": f"Carátula General del {grupo}"})
                caratulas_macro_usadas.add(grupo)
            else:
                alertas.append(f"⚠️ No se encontró la carátula general de {grupo} para incorporarla al primer código")

        principal = _encontrar_caratula_por_carpeta(sel["name"], caratulas, False)
        if principal:
            secuencia.append({"id": principal["id"], "name": principal["name"], "type": "Carátula Principal"})

        if grupo == "ANEXO 11":
            resultado11 = _construir_secuencia_anexo11(sel["id"], caratulas)
            secuencia.extend(resultado11["archivos"])
            if resultado11["alerta"]:
                alertas.append(resultado11["alerta"])
        elif grupo == "ANEXO 13":
            secuencia.extend(_rastrear_anexo13(sel["id"], True, False, caratulas))
        else:
            secuencia.extend(_rastrear_generico(sel["id"], True, caratulas))

        secuencia = _eliminar_duplicados_por_id(secuencia)
        compilaciones.append({
            "nombreCarpeta": sel["name"], "archivos": secuencia,
            "alerta": " | ".join(alertas), "folderPrimerOrden": primer_orden,
        })

    return {"idC6": id_c6, "compilaciones": compilaciones}


# ====================================================================
# procesarCompilacionSegunModo — ejecuta en el mismo proceso
# ====================================================================

def procesar_compilacion(seleccionados: list[dict], config: dict, compile_in_parts_fn, drive_service, usuario: str) -> dict:
    hoja = "CARATULAS Y COMPILADOS"
    datos = obtener_datos_para_compilar(seleccionados)
    opciones = config or {}
    politica_duplicados = opciones.get("duplicados") if opciones.get("duplicados") in ("nuevo", "reemplazar", "omitir") else "nuevo"
    limite_paginas = CONFIG["LIMITE_PAGINAS"]
    estricto = CONFIG["MODO_ESTRICTO_COMPILADOR"]

    cache_carpetas: dict[str, str] = {}
    logs: list[list[Any]] = []
    omitidos = 0
    generados = []
    errores_generales = []

    for indice, comp in enumerate(datos["compilaciones"]):
        file_ids = [item["id"] for item in comp["archivos"]]
        tiene_originales = any(item["type"] == "Original" for item in comp["archivos"])

        if not tiene_originales or not file_ids:
            omitidos += 1
            continue

        destino_id = gs.get_or_create_folder(datos["idC6"], comp["folderPrimerOrden"], cache_carpetas)["id"]
        nombre_base = gs.sanitize_filename("COMPILADO_" + comp["nombreCarpeta"].upper())

        existentes = [f for f in gs.list_pdfs_in_folder(destino_id) if f["name"].upper().startswith(nombre_base)]
        if existentes and politica_duplicados == "omitir":
            omitidos += 1
            continue

        ids_reemplazar = [f["id"] for f in existentes] if politica_duplicados == "reemplazar" else []

        marca = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        nombre_salida = f"{nombre_base} ({marca}_{indice + 1}).pdf"

        try:
            parts, errors = compile_in_parts_fn(
                service=drive_service, file_ids=file_ids, destination_folder_id=destino_id,
                output_filename=nombre_salida, page_limit=limite_paginas,
                replace_existing=False, strict_mode=estricto, expected_source_count=len(file_ids),
            )
            if not parts:
                raise RuntimeError(errors[-1]["detail"] if errors else "No se pudo generar ningún PDF.")

            if ids_reemplazar:
                for old_id in ids_reemplazar:
                    try:
                        drive_service.files().update(fileId=old_id, body={"trashed": True}, supportsAllDrives=True).execute()
                    except Exception:
                        pass

            for parte in parts:
                generados.append({"nombreCarpeta": comp["nombreCarpeta"], **parte})
                logs.append([
                    datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "Panel Web - Compilador", parte.get("final_name", nombre_salida),
                    f"✅ Compilado por {usuario}" + (comp["alerta"] and f" | {comp['alerta']}" or ""),
                    parte.get("url", ""), parte.get("id", ""),
                ])
            if comp["alerta"] and not errors:
                logs[-1][3] += ""  # la alerta ya va en el estado
            for err in errors:
                errores_generales.append({"carpeta": comp["nombreCarpeta"], "detalle": err.get("detail", "")})
                logs.append([
                    datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "Panel Web - Compilador", comp["nombreCarpeta"],
                    f"⚠️ {err.get('detail', 'Error parcial')}", "", "",
                ])

        except Exception as error:
            errores_generales.append({"carpeta": comp["nombreCarpeta"], "detalle": str(error)})
            logs.append([
                datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
                "Panel Web - Compilador", comp["nombreCarpeta"], f"❌ {error}", "", "",
            ])

    registros = gs.append_log_rows(hoja, logs)

    status = "success"
    if errores_generales:
        status = "partial" if generados else "error"

    return {
        "status": status,
        "creados": len(generados),
        "omitidos": omitidos,
        "errores": errores_generales,
        "registrosAgregados": registros,
        "partes": generados,
        "mensaje": (
            ("⚠️ Compilación completada con observaciones. " if errores_generales else "✅ Compilación completada. ")
            + f"PDF creados: {len(generados)} | Omitidos: {omitidos} | Errores: {len(errores_generales)}"
        ),
    }
