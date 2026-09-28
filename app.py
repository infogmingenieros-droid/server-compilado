from __future__ import annotations

import gc
import errno
import traceback
import io
import logging
import shutil
import subprocess
import sys
import signal
import os
import re
import threading
import time
import uuid
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from flask import Flask, jsonify, request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF: ruta especial para PDFs gigantes, sin alterar el flujo normal

# ================================================================
# CONFIGURACIÓN GENERAL (OPTIMIZADA PARA RAILWAY / HASTA 8 GB RAM)
# ================================================================

PORT = int(os.getenv("PORT", "8080"))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))
UPLOAD_CHUNK_SIZE = max(256 * 1024, int(os.getenv("UPLOAD_CHUNK_SIZE", str(4 * 1024 * 1024))))
JOB_TTL_SECONDS = max(60, int(os.getenv("JOB_TTL_SECONDS", str(6 * 60 * 60))))
GC_COLLECT_EVERY_FILES = max(5, int(os.getenv("GC_COLLECT_EVERY_FILES", "20")))
SISTEMA_MAESTRO_KEY = os.getenv("SISTEMA_MAESTRO_KEY", "").strip()
SERVER_INSTANCE_ID = uuid.uuid4().hex
SERVER_STARTED_AT = time.time()

# PDFs gigantes: solo activa una ruta alternativa cuando una fuente supera
# este tamaño. Los PDFs normales siguen usando el compilador actual (pypdf).
HEAVY_PDF_THRESHOLD_MB = max(100, int(os.getenv("HEAVY_PDF_THRESHOLD_MB", "150")))
HEAVY_PDF_THRESHOLD_BYTES = HEAVY_PDF_THRESHOLD_MB * 1024 * 1024

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "").strip() or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12 horas
)

job_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
# TOMOS se serializan entre sí; el compilador conserva sus 2 workers originales.
tomos_semaphore = threading.BoundedSemaphore(1)
job_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="pdf-job")
jobs_lock = threading.RLock()
jobs: dict[str, dict[str, Any]] = {}
request_index: dict[str, str] = {}

output_locks_guard = threading.RLock()
output_locks: dict[str, threading.RLock] = {}


# ================================================================
# AUTENTICACIÓN Y GOOGLE DRIVE
# ================================================================

def get_bearer_token() -> str:
    if SISTEMA_MAESTRO_KEY:
        received_key = (request.headers.get("X-Sistema-Maestro-Key") or "").strip()
        if received_key != SISTEMA_MAESTRO_KEY:
            raise ValueError("Clave privada del Sistema Maestro inválida.")

    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise ValueError("Falta el encabezado Authorization: Bearer <token>.")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise ValueError("El token OAuth está vacío.")
    return token


def get_drive_service(token: str):
    credentials = Credentials(token=token)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def download_drive_file_to_path(service, file_id: str, directory: str) -> str:
    if not file_id:
        raise ValueError("Se recibió un ID de archivo vacío.")

    path = os.path.join(directory, f"source_{uuid.uuid4().hex}.pdf")
    media_request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(path, "wb") as output:
        downloader = MediaIoBaseDownload(output, media_request, chunksize=UPLOAD_CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=5)
    return path


def open_pdf_reader_path(path: str, file_id: str) -> tuple[Any, PdfReader]:
    handle = open(path, "rb")
    try:
        reader = PdfReader(handle, strict=False)
        if reader.is_encrypted:
            decrypted = reader.decrypt("")
            if not decrypted:
                raise ValueError(f"El PDF {file_id} está protegido con contraseña.")
        return handle, reader
    except Exception:
        handle.close()
        raise


def write_pdf_to_path(writer: PdfWriter, directory: str, prefix: str) -> str:
    path = os.path.join(directory, f"{prefix}_{uuid.uuid4().hex}.pdf")
    with open(path, "wb") as output:
        writer.write(output)
    return path


def upload_pdf_path(service, path: str, folder_id: str, filename: str, replace_existing: bool = False) -> dict[str, Any]:
    if not folder_id:
        raise ValueError("No se indicó la carpeta de destino.")
    safe_name = sanitize_filename(filename)
    media = MediaFileUpload(path, mimetype="application/pdf", resumable=True, chunksize=UPLOAD_CHUNK_SIZE)
    metadata = {"name": safe_name, "parents": [folder_id]}
    output_lock = get_output_lock(folder_id, safe_name)

    with output_lock:
        uploaded = service.files().create(
            body=metadata, media_body=media,
            fields="id,name,webViewLink,size,createdTime",
            supportsAllDrives=True
        ).execute(num_retries=5)

        removed_duplicates = 0
        if replace_existing:
            removed_duplicates = trash_duplicate_files(service, folder_id, safe_name, str(uploaded.get("id") or ""))

    return {
        "id": uploaded.get("id", ""),
        "url": uploaded.get("webViewLink", ""),
        "final_name": uploaded.get("name", safe_name),
        "size": uploaded.get("size", ""),
        "created_time": uploaded.get("createdTime", ""),
        "duplicates_removed": removed_duplicates,
    }


def trash_file_ids(service, file_ids: Iterable[str]) -> None:
    for file_id in file_ids:
        if not file_id:
            continue
        try:
            service.files().update(fileId=file_id, body={"trashed": True}, fields="id,trashed", supportsAllDrives=True).execute(num_retries=5)
        except Exception:
            app.logger.exception("No se pudo retirar la salida parcial %s", file_id)


def escape_drive_query_literal(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def get_output_lock(folder_id: str, filename: str) -> threading.RLock:
    key = f"{folder_id}::{filename}".casefold()
    with output_locks_guard:
        lock = output_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            output_locks[key] = lock
        return lock


def trash_duplicate_files(service, folder_id: str, filename: str, keep_file_id: str) -> int:
    escaped_folder = escape_drive_query_literal(folder_id)
    escaped_name = escape_drive_query_literal(filename)
    query = f"'{escaped_folder}' in parents and name = '{escaped_name}' and trashed = false"

    removed = 0
    page_token: str | None = None

    while True:
        response = service.files().list(
            q=query, fields="nextPageToken,files(id,name)",
            pageToken=page_token, spaces="drive",
            supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute(num_retries=5)

        for item in response.get("files", []):
            file_id = str(item.get("id") or "").strip()
            if not file_id or file_id == keep_file_id:
                continue
            service.files().update(fileId=file_id, body={"trashed": True}, fields="id,trashed", supportsAllDrives=True).execute(num_retries=5)
            removed += 1

        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return removed


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", str(filename or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "SALIDA.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def split_pdf_extension(filename: str) -> tuple[str, str]:
    safe = sanitize_filename(filename)
    return safe[:-4], ".pdf"


def validate_payload(data: Any, required: Iterable[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("El cuerpo debe ser un JSON válido.")
    for key in required:
        if data.get(key) in (None, "", []):
            raise ValueError(f"Falta el campo obligatorio: {key}.")
    return data


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "si", "sí", "yes"}


# ================================================================
# DIAGNÓSTICO DE TOMOS: MEMORIA / DISCO / TIPO DE ERROR
# ================================================================
# Las mediciones son instantáneas y pueden cambiar al salir el proceso.
# Si Railway finaliza el contenedor (OOM / reinicio), Python no puede
# emitir una excepción final: en ese caso verificar Metrics y Deploy Logs.

def _read_number_file(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii") as handle:
            raw = handle.read().strip()
        return int(raw) if raw.isdigit() else None
    except (OSError, ValueError):
        return None


def _memory_events() -> dict[str, int]:
    try:
        with open("/sys/fs/cgroup/memory.events", "r", encoding="ascii") as handle:
            return {key: int(value) for key, value in
                    (line.strip().split() for line in handle if line.strip())}
    except (OSError, ValueError):
        return {}


def tomos_diagnostics() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "server_instance_id": SERVER_INSTANCE_ID,
        "memory_used_mb": None,
        "memory_limit_mb": None,
        "memory_available_mb": None,
        "process_rss_mb": None,
        "disk_free_mb": None,
        "oom_kill_count": None,
    }
    used = _read_number_file("/sys/fs/cgroup/memory.current")
    limit = _read_number_file("/sys/fs/cgroup/memory.max")
    if used is not None:
        snapshot["memory_used_mb"] = round(used / 1048576, 1)
    if limit is not None and limit < (1 << 60):
        snapshot["memory_limit_mb"] = round(limit / 1048576, 1)
        if used is not None:
            snapshot["memory_available_mb"] = round(max(0, limit - used) / 1048576, 1)
    try:
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    snapshot["process_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        snapshot["disk_free_mb"] = round(shutil.disk_usage(tempfile.gettempdir()).free / 1048576, 1)
    except OSError:
        pass
    events = _memory_events()
    if "oom_kill" in events:
        snapshot["oom_kill_count"] = events["oom_kill"]
    return snapshot


def classify_tomos_error(exc: Exception, stage: str = "") -> str:
    message = str(exc).lower()
    if isinstance(exc, MemoryError) or "cannot allocate memory" in message or "out of memory" in message:
        return "MEMORIA_INSUFICIENTE"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return "DISCO_INSUFICIENTE"
    if "no space left" in message:
        return "DISCO_INSUFICIENTE"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or "timed out" in message or "timeout" in message:
        return "TIEMPO_AGOTADO"
    if "integridad" in message or "se esperaban" in message and "página" in message:
        return "INTEGRIDAD_PDF"
    if "password" in message or "contraseña" in message or "encrypted" in message:
        return "PDF_PROTEGIDO"
    if "404" in message or "not found" in message or "no existe" in message:
        return "ARCHIVO_NO_ENCONTRADO"
    if "403" in message or "permission" in message or "acceso" in message:
        return "ACCESO_DRIVE"
    if stage in ("reading", "merging"):
        return "PDF_LECTURA_O_FUSION"
    if stage in ("downloading", "uploading"):
        return "GOOGLE_DRIVE"
    return "ERROR_PROCESAMIENTO"


def tomo_failure(exc: Exception, stage: str, file_id: str = "") -> dict[str, Any]:
    return {
        "file_id": file_id,
        "stage": stage or "desconocida",
        "error_code": classify_tomos_error(exc, stage),
        "detail": str(exc)[:1400] or type(exc).__name__,
        "diagnostics": tomos_diagnostics(),
    }


# ================================================================
# ALMACÉN DE TRABAJOS ASÍNCRONOS
# ================================================================

def cleanup_expired_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        expired_ids = [job_id for job_id, job in jobs.items() if float(job.get("updated_at", 0)) < cutoff]
        for job_id in expired_ids:
            request_id = str(jobs[job_id].get("request_id") or "").strip()
            jobs.pop(job_id, None)
            if request_id and request_index.get(request_id) == job_id:
                request_index.pop(request_id, None)


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = time.time()


def update_job_progress(job_id: str, **progress_changes: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        progress = dict(job.get("progress") or {})
        progress.update(progress_changes)
        job["progress"] = progress
        job["updated_at"] = time.time()


def create_or_reuse_job(*, job_type: str, request_id: str, token: str, payload: dict[str, Any]) -> tuple[str, bool]:
    cleanup_expired_jobs()
    normalized_request_id = str(request_id or "").strip()

    with jobs_lock:
        if normalized_request_id:
            existing_job_id = request_index.get(normalized_request_id)
            if existing_job_id and existing_job_id in jobs:
                return existing_job_id, False

        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "job_id": job_id,
            "request_id": normalized_request_id,
            "job_type": job_type,
            "job_state": "queued",
            "status": "accepted",
            "created_at": time.time(),
            "updated_at": time.time(),
            "progress": {
                "processed_files": 0,
                "total_files": len(payload.get("file_ids") or []),
                "parts_created": 0,
                "pages_in_current_part": 0,
                "stage": "queued",
            },
        }
        if normalized_request_id:
            request_index[normalized_request_id] = job_id

    job_executor.submit(run_background_job, job_id, job_type, token, payload)
    return job_id, True


def get_public_job(job_id: str) -> dict[str, Any] | None:
    cleanup_expired_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return None
        public_job = {
            "job_id": job.get("job_id"),
            "job_type": job.get("job_type"),
            "job_state": job.get("job_state"),
            "status": job.get("status"),
            "progress": dict(job.get("progress") or {}),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "server_instance_id": SERVER_INSTANCE_ID,
        }
        result = job.get("result")
        if isinstance(result, dict):
            public_job.update(result)
        if job.get("detail"):
            public_job["detail"] = job.get("detail")
        return public_job


# ================================================================
# COMPILADOR Y TOMOS
# ================================================================

ProgressCallback = Callable[..., None]


def get_drive_pdf_size(service, file_id: str) -> tuple[int, str]:
    """Devuelve (tamaño_en_bytes, nombre) sin descargar el PDF."""
    meta = service.files().get(
        fileId=str(file_id),
        fields="id,name,size,mimeType",
        supportsAllDrives=True,
    ).execute(num_retries=5)
    try:
        size = int(meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return size, str(meta.get("name") or file_id)


def compile_in_parts_heavy(
    service, file_ids: list[str], destination_folder_id: str,
    output_filename: str, page_limit: int,
    progress_callback: ProgressCallback | None = None,
    replace_existing: bool = False, strict_mode: bool = True,
    expected_source_count: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """
    Ruta exclusiva para PDFs gigantes.

    Descarga cada fuente a /tmp con la función existente y usa PyMuPDF
    para copiar páginas desde archivos físicos. No reemplaza el flujo normal:
    compile_in_parts() solo entra aquí si detecta una fuente >= umbral pesado.
    """
    if page_limit < 1:
        raise ValueError("El límite de páginas debe ser mayor que cero.")
    if expected_source_count and len(file_ids) != expected_source_count:
        raise ValueError("La cantidad de fuentes recibidas no coincide con la esperada.")

    base_name, extension = split_pdf_extension(output_filename)
    parts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    part_number = 1
    pages_in_current_part = 0
    output_doc = fitz.open()

    def report(**changes: Any) -> None:
        if progress_callback:
            progress_callback(**changes)

    def flush_current(temp_dir: str, is_split: bool) -> None:
        nonlocal output_doc, pages_in_current_part, part_number
        if pages_in_current_part == 0:
            return

        if is_split or part_number > 1:
            part_name = f"{base_name} (Parte {part_number}) ({pages_in_current_part} páginas){extension}"
        else:
            part_name = f"{base_name} ({pages_in_current_part} páginas){extension}"

        output_path = os.path.join(temp_dir, f"compilado_pesado_{uuid.uuid4().hex}.pdf")
        try:
            # garbage=0 y deflate=False evitan recomprimir planos/imágenes gigantes.
            # Esto prioriza bajo uso de RAM y velocidad; conserva los streams.
            output_doc.save(output_path, garbage=0, deflate=False, clean=False)
            output_doc.close()
            output_doc = fitz.open()

            uploaded = upload_pdf_path(
                service,
                output_path,
                destination_folder_id,
                part_name,
                replace_existing=replace_existing,
            )
            uploaded["paginas"] = pages_in_current_part
            parts.append(uploaded)
            report(
                parts_created=len(parts),
                pages_in_current_part=0,
                last_created_file=uploaded.get("final_name", part_name),
                processing_mode="heavy_disk",
            )
        finally:
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass

        pages_in_current_part = 0
        part_number += 1
        gc.collect()

    total_files = len(file_ids)

    try:
        with tempfile.TemporaryDirectory(prefix="maestro_compilar_pesado_") as temp_dir:
            for index, file_id in enumerate(file_ids, start=1):
                source_path = None
                source_doc = None
                try:
                    source_path = download_drive_file_to_path(service, str(file_id), temp_dir)
                    source_doc = fitz.open(source_path)

                    if source_doc.needs_pass:
                        # Igual que el flujo normal: solo intentamos contraseña vacía.
                        if not source_doc.authenticate(""):
                            raise ValueError(f"El PDF {file_id} está protegido con contraseña.")

                    total_pages_source = source_doc.page_count
                    start_page = 0

                    while start_page < total_pages_source:
                        available = page_limit - pages_in_current_part
                        if available <= 0:
                            flush_current(temp_dir, is_split=True)
                            available = page_limit

                        take = min(available, total_pages_source - start_page)
                        end_page = start_page + take - 1

                        # insert_pdf copia objetos PDF sin rasterizar las páginas.
                        output_doc.insert_pdf(
                            source_doc,
                            from_page=start_page,
                            to_page=end_page,
                        )

                        pages_in_current_part += take
                        start_page += take

                        if pages_in_current_part >= page_limit:
                            flush_current(temp_dir, is_split=True)

                    report(
                        processed_files=index,
                        total_files=total_files,
                        parts_created=len(parts),
                        pages_in_current_part=pages_in_current_part,
                        current_file_id=str(file_id),
                        processing_mode="heavy_disk",
                    )

                except Exception as exc:
                    errors.append({"file_id": str(file_id), "detail": str(exc)})
                    report(
                        processed_files=index,
                        total_files=total_files,
                        parts_created=len(parts),
                        pages_in_current_part=pages_in_current_part,
                        current_file_id=str(file_id),
                        last_error=str(exc),
                        processing_mode="heavy_disk",
                    )
                    if strict_mode:
                        try:
                            output_doc.close()
                        except Exception:
                            pass
                        trash_file_ids(service, [p.get("id", "") for p in parts])
                        return [], errors
                finally:
                    if source_doc is not None:
                        try:
                            source_doc.close()
                        except Exception:
                            pass
                    if source_path and os.path.exists(source_path):
                        try:
                            os.remove(source_path)
                        except Exception:
                            pass
                    gc.collect()

            flush_current(temp_dir, is_split=bool(parts))
            return parts, errors
    finally:
        try:
            output_doc.close()
        except Exception:
            pass
        gc.collect()



def compile_in_parts_heavy_qpdf(
    service, file_ids: list[str], destination_folder_id: str,
    output_filename: str, page_limit: int,
    progress_callback: ProgressCallback | None = None,
    replace_existing: bool = False, strict_mode: bool = True,
    expected_source_count: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """PDFs gigantes: qpdf como proceso externo; no se cargan páginas en Python.

    Descarga a disco con la función existente y conserva el orden y la división
    por límite de páginas. No modifica el flujo normal ni las rutas HTTP.
    """
    qpdf_bin = shutil.which("qpdf")
    if not qpdf_bin:
        raise RuntimeError(
            "Falta qpdf en Railway. Agrega el Dockerfile del PASO 2 en la raíz "
            "del repositorio y despliega de nuevo."
        )
    if page_limit < 1:
        raise ValueError("El límite de páginas debe ser mayor que cero.")
    if expected_source_count and len(file_ids) != expected_source_count:
        raise ValueError("La cantidad de fuentes recibidas no coincide con la esperada.")

    def run_qpdf(args: list[str], timeout: int = 3600) -> str:
        # PASO 4: diagnósticos de la llamada a qpdf SIN modificar sus argumentos,
        # la fusión, las carátulas ni el flujo normal. Los contadores de cgroup
        # permiten detectar un OOM que no se vea en la gráfica de Railway.
        def memory_snapshot() -> dict[str, Any]:
            root = "/sys/fs/cgroup"
            snapshot: dict[str, Any] = {}
            for name in ("memory.current", "memory.peak", "memory.max"):
                try:
                    with open(os.path.join(root, name), "r", encoding="ascii") as f:
                        raw = f.read().strip()
                    snapshot[name] = int(raw) if raw.isdigit() else raw
                except (OSError, ValueError):
                    pass
            try:
                with open(os.path.join(root, "memory.events"), "r", encoding="ascii") as f:
                    snapshot["memory.events"] = {
                        key: int(value)
                        for line in f
                        for key, value in [line.strip().split()]
                    }
            except (OSError, ValueError):
                pass
            try:
                snapshot["tmp_free_mb"] = round(
                    shutil.disk_usage(tempfile.gettempdir()).free / (1024 * 1024), 1
                )
            except OSError:
                pass
            return snapshot

        before = memory_snapshot()
        operation = "fusionar" if "--pages" in args else "contar_paginas"
        app.logger.warning(
            "DIAG QPDF INICIO operacion=%s; memoria_y_disco=%s",
            operation, before,
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                [qpdf_bin, *args], capture_output=True, text=True,
                check=False, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            app.logger.error(
                "DIAG QPDF TIMEOUT operacion=%s segundos=%.1f despues=%s",
                operation, time.monotonic() - started, memory_snapshot(),
            )
            raise RuntimeError(f"qpdf agotó el tiempo de {timeout}s") from exc

        after = memory_snapshot()
        before_events = before.get("memory.events", {})
        after_events = after.get("memory.events", {})
        oom_delta = int(after_events.get("oom", 0)) - int(before_events.get("oom", 0))
        killed_delta = int(after_events.get("oom_kill", 0)) - int(before_events.get("oom_kill", 0))
        app.logger.warning(
            "DIAG QPDF FIN operacion=%s rc=%s segundos=%.1f oom_delta=%s "
            "oom_kill_delta=%s; antes=%s; despues=%s",
            operation, result.returncode, time.monotonic() - started,
            oom_delta, killed_delta, before, after,
        )

        # qpdf: 0 = correcto; 3 = advertencias recuperables; 2 = error real.
        # Los códigos negativos son señales: -9 = SIGKILL, NUNCA éxito.
        if result.returncode == 3:
            detail = (result.stderr or "Advertencias sin detalle").strip()
            app.logger.warning("qpdf recuperó un PDF con advertencias: %s", detail[:800])
        elif result.returncode != 0:
            detail = (result.stderr or result.stdout or "error no detallado").strip()
            if result.returncode == -9:
                diagnosis = (
                    "cgroup registró OOM-kill durante esta operación"
                    if killed_delta > 0 else
                    "sin aumento de OOM-kill en este cgroup; revisar memoria, disco y eventos externos"
                )
                detail = f"SIGKILL (-9); {diagnosis}. {detail}"
            raise RuntimeError(
                f"qpdf terminó con código {result.returncode}: {detail[:800]}"
            )
        return result.stdout.strip()

    def page_count(path: str) -> int:
        result = run_qpdf(["--show-npages", path], timeout=600)
        try:
            count = int(result.splitlines()[-1].strip())
        except (ValueError, IndexError) as exc:
            raise ValueError(f"qpdf no devolvió páginas válidas para {os.path.basename(path)}") from exc
        if count < 1:
            raise ValueError("El PDF no contiene páginas.")
        return count

    base_name, extension = split_pdf_extension(output_filename)
    parts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    segments: list[tuple[str, int, int]] = []
    pages_in_current_part = 0
    part_number = 1

    def report(**changes: Any) -> None:
        if progress_callback:
            progress_callback(**changes)

    def flush_current(temp_dir: str, preserve_path: str | None = None,
                      is_split: bool = False) -> None:
        nonlocal segments, pages_in_current_part, part_number
        if not segments:
            return
        count_expected = pages_in_current_part
        if is_split or part_number > 1:
            name = f"{base_name} (Parte {part_number}) ({count_expected} páginas){extension}"
        else:
            name = f"{base_name} ({count_expected} páginas){extension}"

        output_path = os.path.join(temp_dir, f"qpdf_compilado_{uuid.uuid4().hex}.pdf")
        sources_to_clean = {path for path, _, _ in segments}
        cmd = ["--empty", "--pages"]
        for path, start, end in segments:
            pages = str(start) if start == end else f"{start}-{end}"
            cmd.extend((path, pages))
        cmd.extend(("--", output_path))

        try:
            run_qpdf(cmd)
            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("qpdf no generó un PDF válido en disco.")
            actual_pages = page_count(output_path)
            if actual_pages != count_expected:
                raise RuntimeError(
                    f"Integridad: se esperaban {count_expected} páginas y qpdf generó {actual_pages}."
                )
            uploaded = upload_pdf_path(
                service, output_path, destination_folder_id, name,
                replace_existing=replace_existing,
            )
            uploaded["paginas"] = count_expected
            parts.append(uploaded)
            report(
                parts_created=len(parts), pages_in_current_part=0,
                last_created_file=uploaded.get("final_name", name),
                processing_mode="heavy_qpdf",
            )
        finally:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                app.logger.warning("No se pudo eliminar temporal de qpdf: %s", output_path)

        # Sólo después de confirmar la subida: liberar las fuentes ya utilizadas.
        # Si un PDF ocupa más de una parte, se conserva hasta acabar sus páginas.
        for old_path in sources_to_clean:
            if old_path != preserve_path:
                try:
                    os.remove(old_path)
                except FileNotFoundError:
                    pass
        segments = []
        pages_in_current_part = 0
        part_number += 1

    try:
        with tempfile.TemporaryDirectory(prefix="maestro_qpdf_pesado_") as temp_dir:
            for index, file_id in enumerate(file_ids, start=1):
                source_path = None
                try:
                    source_path = download_drive_file_to_path(service, str(file_id), temp_dir)
                    num_pages = page_count(source_path)
                    next_page = 1
                    while next_page <= num_pages:
                        available = page_limit - pages_in_current_part
                        if available <= 0:
                            flush_current(temp_dir, preserve_path=source_path, is_split=True)
                            available = page_limit
                        end_page = min(num_pages, next_page + available - 1)
                        segments.append((source_path, next_page, end_page))
                        pages_in_current_part += end_page - next_page + 1
                        next_page = end_page + 1
                        if pages_in_current_part == page_limit:
                            keep = source_path if next_page <= num_pages else None
                            flush_current(temp_dir, preserve_path=keep, is_split=True)

                    # Si el último segmento ya se subió, no hace falta
                    # conservar esta fuente hasta el final del trabajo.
                    if source_path and not any(seg[0] == source_path for seg in segments):
                        if os.path.exists(source_path):
                            os.remove(source_path)

                    report(
                        processed_files=index, total_files=len(file_ids),
                        parts_created=len(parts),
                        pages_in_current_part=pages_in_current_part,
                        current_file_id=str(file_id),
                        processing_mode="heavy_qpdf",
                    )
                except Exception as exc:
                    # Un error al construir/subir una parte no permite ignorar
                    # sólo una fuente sin arriesgar un PDF incompleto.
                    # Fallar y revertir es preferible a reportar éxito parcial.
                    if segments or parts:
                        raise
                    errors.append({"file_id": str(file_id), "detail": str(exc)})
                    report(
                        processed_files=index, total_files=len(file_ids),
                        parts_created=len(parts),
                        pages_in_current_part=pages_in_current_part,
                        current_file_id=str(file_id), last_error=str(exc),
                        processing_mode="heavy_qpdf",
                    )
                    if strict_mode:
                        return [], errors
                finally:
                    if source_path and not any(seg[0] == source_path for seg in segments):
                        try:
                            if os.path.exists(source_path):
                                os.remove(source_path)
                        except OSError:
                            pass
            flush_current(temp_dir, is_split=bool(parts))
            return parts, errors
    except Exception:
        if parts:
            trash_file_ids(service, [p.get("id", "") for p in parts])
        raise

def compile_in_parts(
    service, file_ids: list[str], destination_folder_id: str,
    output_filename: str, page_limit: int,
    progress_callback: ProgressCallback | None = None,
    replace_existing: bool = False, strict_mode: bool = True,
    expected_source_count: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if page_limit < 1:
        raise ValueError("El límite de páginas debe ser mayor que cero.")
    if expected_source_count and len(file_ids) != expected_source_count:
        raise ValueError("La cantidad de fuentes recibidas no coincide con la esperada.")

    # Detectar PDF gigante sin descargarlo. Si aparece uno, usamos la ruta
    # especial de bajo consumo de RAM. El flujo normal queda intacto.
    heavy_sources: list[tuple[str, int, str]] = []
    for _file_id in file_ids:
        try:
            _size, _name = get_drive_pdf_size(service, str(_file_id))
            if _size >= HEAVY_PDF_THRESHOLD_BYTES:
                heavy_sources.append((str(_file_id), _size, _name))
        except Exception as _meta_exc:
            # Si Drive no entrega el tamaño, no rompemos el comportamiento actual.
            app.logger.warning("No se pudo leer tamaño de %s: %s", _file_id, _meta_exc)

    if heavy_sources:
        app.logger.warning(
            "Modo PDF gigante activado. Umbral=%s MB | Fuentes=%s",
            HEAVY_PDF_THRESHOLD_MB,
            [f"{name} ({size / (1024 * 1024):.1f} MB)" for _, size, name in heavy_sources],
        )
        if progress_callback:
            progress_callback(
                processing_mode="heavy_qpdf",
                heavy_files=len(heavy_sources),
                heavy_threshold_mb=HEAVY_PDF_THRESHOLD_MB,
            )
        return compile_in_parts_heavy_qpdf(
            service=service,
            file_ids=file_ids,
            destination_folder_id=destination_folder_id,
            output_filename=output_filename,
            page_limit=page_limit,
            progress_callback=progress_callback,
            replace_existing=replace_existing,
            strict_mode=strict_mode,
            expected_source_count=expected_source_count,
        )

    base_name, extension = split_pdf_extension(output_filename)
    parts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    writer = PdfWriter()
    pages_in_current_part = 0
    part_number = 1

    def report(**changes: Any) -> None:
        if progress_callback:
            progress_callback(**changes)

    with tempfile.TemporaryDirectory(prefix="maestro_compilar_") as temp_dir:
        def flush_current(is_split: bool) -> None:
            nonlocal writer, pages_in_current_part, part_number
            if pages_in_current_part == 0:
                return
            if is_split or part_number > 1:
                part_name = f"{base_name} (Parte {part_number}) ({pages_in_current_part} páginas){extension}"
            else:
                part_name = f"{base_name} ({pages_in_current_part} páginas){extension}"

            output_path = write_pdf_to_path(writer, temp_dir, "compilado")
            uploaded = upload_pdf_path(service, output_path, destination_folder_id, part_name, replace_existing=replace_existing)
            uploaded["paginas"] = pages_in_current_part
            parts.append(uploaded)
            report(parts_created=len(parts), pages_in_current_part=0, last_created_file=uploaded.get("final_name", part_name))

            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass

            writer = PdfWriter()
            pages_in_current_part = 0
            part_number += 1
            # Liberar referencias del writer al cerrar cada parte, pero evitar
            # forzar GC después de cada PDF (coste de CPU innecesario).
            gc.collect()

        total_files = len(file_ids)
        for index, file_id in enumerate(file_ids, start=1):
            source_handle = None
            source_path = None
            try:
                source_path = download_drive_file_to_path(service, str(file_id), temp_dir)
                source_handle, reader = open_pdf_reader_path(source_path, str(file_id))

                for page in reader.pages:
                    if pages_in_current_part >= page_limit:
                        flush_current(is_split=True)
                    writer.add_page(page)
                    pages_in_current_part += 1

                report(processed_files=index, total_files=total_files, parts_created=len(parts),
                       pages_in_current_part=pages_in_current_part, current_file_id=str(file_id))
            except Exception as exc:
                errors.append({"file_id": str(file_id), "detail": str(exc)})
                report(processed_files=index, total_files=total_files, parts_created=len(parts),
                       pages_in_current_part=pages_in_current_part, current_file_id=str(file_id), last_error=str(exc))
                if strict_mode:
                    trash_file_ids(service, [p.get("id", "") for p in parts])
                    return [], errors
            finally:
                if source_handle:
                    try:
                        source_handle.close()
                    except Exception:
                        pass
                if source_path and os.path.exists(source_path):
                    try:
                        os.remove(source_path)
                    except Exception:
                        pass

            # Forzar GC solo por lotes. Los objetos temporales del lector ya no
            # necesitan una recolección completa después de cada archivo.
            if index % GC_COLLECT_EVERY_FILES == 0:
                gc.collect()

        flush_current(is_split=bool(parts))
        return parts, errors



# Solo TOMOS: motor aislado en un subproceso, con presupuesto apropiado para 1 GB.
# El compilador existente conserva su código y sus dos workers configurados.
# Railway 1 GB: el motor puede llegar a ~700 MB en PDFs difíciles, pero
# mantenemos barreras globales para no tumbar el worker de Gunicorn.
TOMOS_MAX_RSS_MB = min(760, max(128, int(os.getenv("TOMOS_MAX_RSS_MB", "720"))))
TOMOS_MAX_AS_MB = min(900, max(256, int(os.getenv("TOMOS_MAX_AS_MB", "850"))))
TOMOS_TOTAL_BUDGET_MB = min(900, max(300, int(os.getenv("TOMOS_TOTAL_BUDGET_MB", "850"))))
TOMOS_MIN_FREE_MB = max(100, int(os.getenv("TOMOS_MIN_FREE_MB", "150")))
TOMOS_MAX_SECONDS = max(60, int(os.getenv("TOMOS_MAX_SECONDS", "2400")))
TOMOS_DISK_RESERVE_MB = max(64, int(os.getenv("TOMOS_DISK_RESERVE_MB", "160")))
TOMOS_SOURCE_GROUP_SIZE = 2  # Fusión jerárquica: dos entradas consecutivas por operación.


def _tomo_rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _tomo_cgroup_available_mb() -> float | None:
    # Descuenta caché 'inactive_file' recuperable, pero mantiene un margen real.
    current = _read_number_file("/sys/fs/cgroup/memory.current")
    cap = _read_number_file("/sys/fs/cgroup/memory.max")
    if current is None or cap is None or cap >= 1 << 60:
        return None
    reclaimable = 0
    try:
        with open("/sys/fs/cgroup/memory.stat", encoding="ascii") as f:
            for line in f:
                if line.startswith("inactive_file "):
                    reclaimable = int(line.split()[1]); break
    except (OSError, ValueError, IndexError):
        pass
    # No se puede reclamar mas memoria que la actualmente contabilizada.
    effective_used = max(0, current - min(current, reclaimable))
    return max(0.0, (cap - effective_used) / 1048576.0)


def _tomo_require_disk(directory: str, minimum_mb: int = TOMOS_DISK_RESERVE_MB) -> None:
    free = shutil.disk_usage(directory).free / 1048576.0
    if free < minimum_mb:
        raise RuntimeError(
            f"DISCO_INSUFICIENTE: solo {free:.0f} MB disponibles; "
            f"reserva mínima {minimum_mb} MB. No se subió ningún tomo."
        )


def _tomo_run_pdf(command: list[str], stage: str,
                  progress_callback: ProgressCallback | None = None,
                  timeout: int = TOMOS_MAX_SECONDS,
                  engine: str | None = None) -> str:
    """Ejecuta una utilidad PDF fuera del worker Gunicorn, con barreras preventivas.

    RLIMIT_AS es un limite de direcciones virtuales, no una garantía del RSS.
    El watchdog tambien verifica RSS, cgroup y consumo total observado.
    """
    engine_name = str(engine or (os.path.basename(command[0]) if command else "motor-pdf")).strip()
    if not command or not shutil.which(command[0]):
        raise RuntimeError("MOTOR_PDF_NO_INSTALADO: falta " + (command[0] if command else "comando"))
    wrapper = ("import os,resource,sys;"
               "n=int(sys.argv[1])*1048576;"
               "resource.setrlimit(resource.RLIMIT_AS,(n,n));"
               "os.execv(sys.argv[2],sys.argv[2:])")
    argv = [sys.executable, "-c", wrapper, str(TOMOS_MAX_AS_MB), *command]
    start = time.monotonic()
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout, \
         tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr,
                                text=True, start_new_session=True)
        peak_rss = 0.0
        latest_diagnostic = {}
        def stop():
            if proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
        try:
            while proc.poll() is None:
                rss = _tomo_rss_mb(proc.pid)
                if rss is not None: peak_rss = max(peak_rss, rss)
                snap = tomos_diagnostics()
                latest_diagnostic = snap
                cgroup_free = _tomo_cgroup_available_mb()
                host_rss = float(snap.get("process_rss_mb") or 0)
                # Cuando no hay cgroup medible, limite de mejor esfuerzo: Python + motor.
                total_est = host_rss + float(rss or 0)
                violation = None
                if rss is not None and rss >= TOMOS_MAX_RSS_MB:
                    violation = f"Motor PDF {rss:.0f} MB >= límite {TOMOS_MAX_RSS_MB} MB"
                elif cgroup_free is not None and cgroup_free < TOMOS_MIN_FREE_MB:
                    violation = f"Reserva del contenedor {cgroup_free:.0f} MB < {TOMOS_MIN_FREE_MB} MB"
                elif total_est >= TOMOS_TOTAL_BUDGET_MB:
                    violation = f"Memoria observada Python+motor {total_est:.0f} MB >= {TOMOS_TOTAL_BUDGET_MB} MB"
                if progress_callback:
                    progress_callback(stage="merging" if stage.startswith("merging") else stage,
                                      merge_step=stage, merge_engine=engine_name,
                                      pdf_process_rss_mb=round(rss or 0),
                                      pdf_process_peak_rss_mb=round(peak_rss),
                                      cgroup_available_mb=round(cgroup_free) if cgroup_free is not None else None,
                                      diagnostics=snap)
                if violation:
                    stop()
                    raise RuntimeError(
                        f"MEMORIA_INSUFICIENTE: {stage}: {engine_name}: {violation}. "
                        f"Pico del motor {peak_rss:.0f} MB. No se subió tomo incompleto. "
                        "Este PDF podría necesitar más memoria; utiliza el servidor de Colab."
                    )
                if time.monotonic() - start > timeout:
                    stop()
                    raise RuntimeError(f"TIEMPO_AGOTADO: {stage}, más de {timeout} segundos.")
                time.sleep(.25)
        finally:
            stop()
        stdout.seek(0); stderr.seek(0)
        text_out = stdout.read(65536)
        text_err = stderr.read(65536)[-2000:]
        if proc.returncode != 0:
            raise RuntimeError(
                f"MOTOR_PDF_ERROR: {stage}, motor {engine_name}, código {proc.returncode}, "
                f"pico RSS {peak_rss:.0f} MB, memoria contenedor "
                f"{latest_diagnostic.get('memory_used_mb')} MB: "
                + (text_err or text_out or 'sin detalle')[-1200:]
            )
        return text_out


def _tomo_pdf_count(path: str, progress_callback=None) -> int:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        raise RuntimeError("PDFINFO_NO_INSTALADO: falta poppler-utils en Dockerfile.")
    info = _tomo_run_pdf([pdfinfo, path], "validating", progress_callback, timeout=180)
    match = re.search(r"^Pages:\s*(\d+)\s*$", info, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("PDF_INVALIDO: el motor no pudo contar las páginas de " + os.path.basename(path))
    return int(match.group(1))


def _tomo_merge_group_with_fallback(
    group: list[str],
    output: str,
    source_pages: int,
    block_stage: str,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, str]:
    """Une un bloque con MuPDF y usa pdfunite como segundo motor.

    Ambos motores se ejecutan bajo las mismas barreras de memoria. El archivo
    solo se acepta si existe y conserva exactamente la cantidad de páginas.
    """
    mutool = shutil.which("mutool")
    pdfunite = shutil.which("pdfunite")
    if not mutool:
        raise RuntimeError("MOTOR_PDF_NO_INSTALADO: falta mutool (mupdf-tools).")
    if not pdfunite:
        raise RuntimeError("MOTOR_PDF_NO_INSTALADO: falta pdfunite (poppler-utils).")

    errores: list[str] = []
    intentos = [
        ("mutool", [mutool, "merge", "-o", output, *group]),
        ("pdfunite", [pdfunite, *group, output]),
    ]

    for indice, (engine, command) in enumerate(intentos, start=1):
        try:
            if os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass

            if progress_callback:
                progress_callback(
                    stage="merging",
                    merge_step=block_stage,
                    merge_engine=engine,
                    merge_attempt=indice,
                    merge_total_attempts=len(intentos),
                    diagnostics=tomos_diagnostics(),
                )

            _tomo_run_pdf(
                command,
                block_stage,
                progress_callback=progress_callback,
                engine=engine,
            )

            if not os.path.isfile(output) or os.path.getsize(output) <= 0:
                raise RuntimeError(
                    f"INTEGRIDAD_PDF: {engine} no creó un bloque PDF válido."
                )

            actual_pages = _tomo_pdf_count(
                output,
                progress_callback=progress_callback,
            )
            if actual_pages != source_pages:
                raise RuntimeError(
                    f"INTEGRIDAD_PDF: {block_stage}, motor {engine}, "
                    f"esperadas {source_pages}, obtenidas {actual_pages} páginas."
                )

            return actual_pages, engine

        except Exception as exc:
            errores.append(f"{engine}: {exc}")
            if os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass

            if indice < len(intentos):
                if progress_callback:
                    progress_callback(
                        stage="merging",
                        merge_step=block_stage,
                        merge_engine="fallback-pdfunite",
                        last_error=str(exc),
                        diagnostics=tomos_diagnostics(),
                    )
                gc.collect()
                time.sleep(0.35)
                continue
            break

    raise RuntimeError(
        "FUSION_MOTORES_FALLIDA: " + block_stage + ". " + " | ".join(errores)
    )


def assemble_tomo(
    service, source_ids: list[str], destination_folder_id: str,
    output_filename: str, progress_callback: ProgressCallback | None = None,
    replace_existing: bool = False, strict_mode: bool = True,
    expected_pages: int = 0, expected_source_count: int = 0,
) -> tuple[dict[str, Any] | None, int, list[dict[str, Any]]]:
    """TOMOS exclusivamente: descarga a disco, une con mutool, verifica y sube.

    No rasteriza páginas. NO promete que cualquier expediente se pueda procesar
    en 1 GB: si rebasa los límites detiene el subproceso y reporta el motivo.
    """
    errors: list[dict[str, Any]] = []
    total_files = len(source_ids)
    if expected_source_count and total_files != expected_source_count:
        return None, 0, [{"file_id": "", "stage": "validating", "error_code": "INTEGRIDAD_FUENTES",
                          "detail": "La cantidad de fuentes recibidas no coincide con la esperada."}]
    if not source_ids:
        return None, 0, [{"file_id": "", "stage": "validating", "error_code": "PDF_VACIO",
                          "detail": "No llegaron PDF para este tomo."}]
    stage = "validating"
    current_file_id = ""
    total_pages = 0
    def report(**kwargs):
        if progress_callback: progress_callback(**kwargs)
    with tempfile.TemporaryDirectory(prefix="railway_tomo_mutool_") as temp_dir:
        try:
            if not shutil.which("mutool") or not shutil.which("pdfinfo"):
                raise RuntimeError("MOTOR_PDF_NO_INSTALADO: Dockerfile debe instalar mupdf-tools y poppler-utils.")
            pending: list[str] = []
            pages_by_path: dict[str, int] = {}
            ids_by_path: dict[str, list[str]] = {}
            for index, file_id in enumerate(source_ids, 1):
                current_file_id = str(file_id)
                stage = "downloading"
                _tomo_require_disk(temp_dir)
                report(stage=stage, processed_files=index - 1, total_files=total_files,
                       current_file_id=current_file_id, diagnostics=tomos_diagnostics())
                path = download_drive_file_to_path(service, current_file_id, temp_dir)
                stage = "reading"
                _tomo_require_disk(temp_dir)
                pages = _tomo_pdf_count(path)
                total_pages += pages
                pages_by_path[path] = pages
                ids_by_path[path] = [current_file_id]
                pending.append(path)
                report(stage=stage, processed_files=index, total_files=total_files,
                       current_file_id=current_file_id, pages_in_current_part=total_pages,
                       diagnostics=tomos_diagnostics())
            if expected_pages and total_pages != expected_pages:
                raise RuntimeError(f"INTEGRIDAD_PDF: se esperaban {expected_pages} páginas y "
                                   f"las fuentes suman {total_pages}. No se subió nada.")
            _tomo_require_disk(temp_dir)
            level = 0
            while len(pending) > 1:
                level += 1
                next_level: list[str] = []
                groups = [pending[i:i + TOMOS_SOURCE_GROUP_SIZE]
                          for i in range(0, len(pending), TOMOS_SOURCE_GROUP_SIZE)]
                for group_number, group in enumerate(groups, 1):
                    if len(group) == 1:
                        next_level.append(group[0]); continue
                    stage = "merging"
                    block_ids = [fid for input_path in group for fid in ids_by_path[input_path]]
                    current_file_id = block_ids[0]
                    block_stage = (f"merging nivel {level}, bloque {group_number}/{len(groups)} "
                                   f"(fuentes: {', '.join(block_ids[:3])}"
                                   f"{' ...' if len(block_ids) > 3 else ''})")
                    source_pages = sum(pages_by_path[p] for p in group)
                    _tomo_require_disk(temp_dir)
                    output = os.path.join(temp_dir, f"nivel{level}_bloque{group_number}_{uuid.uuid4().hex}.pdf")
                    report(stage="merging", merge_step=block_stage, merge_level=level,
                           merge_block=group_number, merge_total_blocks=len(groups),
                           processed_files=total_files, total_files=total_files,
                           merge_engine="mutool", diagnostics=tomos_diagnostics())
                    actual_pages, motor_usado = _tomo_merge_group_with_fallback(
                        group=group,
                        output=output,
                        source_pages=source_pages,
                        block_stage=block_stage,
                        progress_callback=report,
                    )
                    stage = "validating"
                    report(stage=stage, merge_step=block_stage, merge_engine=motor_usado,
                           processed_files=total_files, total_files=total_files,
                           diagnostics=tomos_diagnostics())
                    next_level.append(output)
                    pages_by_path[output] = actual_pages
                    ids_by_path[output] = block_ids
                    for consumed in group:
                        # Ningún bloque se borra antes de crear y validar el reemplazo.
                        try: os.remove(consumed)
                        except OSError: pass
                        pages_by_path.pop(consumed, None)
                        ids_by_path.pop(consumed, None)
                    _tomo_require_disk(temp_dir)
                pending = next_level
            final_path = pending[0]
            stage = "validating"
            final_pages = _tomo_pdf_count(final_path)
            if final_pages != total_pages or (expected_pages and final_pages != expected_pages):
                raise RuntimeError(f"INTEGRIDAD_PDF: final {final_pages} páginas, "
                                   f"origen {total_pages}, esperadas {expected_pages}.")
            stage = "uploading"
            report(stage=stage, processed_files=total_files, total_files=total_files,
                   pages_in_current_part=final_pages, diagnostics=tomos_diagnostics())
            _tomo_require_disk(temp_dir)
            uploaded = upload_pdf_path(service, final_path, destination_folder_id, output_filename,
                                       replace_existing=replace_existing)
            uploaded["paginas"] = final_pages
            report(stage="finished", processed_files=total_files, total_files=total_files,
                   pages_in_current_part=final_pages, diagnostics=tomos_diagnostics())
            return uploaded, final_pages, []
        except Exception as exc:
            fail = tomo_failure(exc, stage, current_file_id)
            detalle_exc = str(exc)
            if "MEMORIA_INSUFICIENTE" in detalle_exc:
                fail["error_code"] = "MEMORIA_INSUFICIENTE"
            elif "MOTOR_PDF_ERROR" in detalle_exc or "FUSION_MOTORES_FALLIDA" in detalle_exc:
                fail["error_code"] = "MOTOR_PDF_ERROR"
            errors.append(fail)
            report(stage=stage, current_file_id=current_file_id,
                   last_error=fail["detail"], error_code=fail["error_code"],
                   diagnostics=fail["diagnostics"])
            app.logger.error("TOMO MUTOOL FALLO etapa=%s codigo=%s detalle=%s", stage,
                             fail["error_code"], fail["detail"], exc_info=True)
            return None, total_pages, errors


def run_background_job(job_id: str, job_type: str, token: str, payload: dict[str, Any]) -> None:
    update_job(job_id, job_state="running", status="running")
    update_job_progress(job_id, stage="initializing", diagnostics=tomos_diagnostics() if job_type == "tomos" else {})
    try:
        with job_semaphore:
            service = get_drive_service(token)
            def progress_callback(**changes: Any) -> None:
                update_job_progress(job_id, **changes)

            if job_type == "compilar":
                file_ids = [str(value).strip() for value in payload["file_ids"] if str(value).strip()]
                page_limit = int(payload.get("limite_paginas", 500))
                parts, errors = compile_in_parts(
                    service=service, file_ids=file_ids,
                    destination_folder_id=str(payload["destination_folder_id"]),
                    output_filename=str(payload["output_filename"]),
                    page_limit=page_limit, progress_callback=progress_callback,
                    replace_existing=as_bool(payload.get("replace_existing")),
                    strict_mode=as_bool(payload.get("strict_mode", True)),
                    expected_source_count=int(payload.get("expected_source_count") or 0),
                )

                if not parts:
                    update_job(job_id, job_state="failed", status="error",
                               detail=(errors[-1].get("detail") if errors else "No se pudo generar ningún PDF."),
                               result={"partes": [], "errores": errors})
                    return

                final_status = "partial" if errors else "success"
                update_job(job_id, job_state="finished", status=final_status,
                           result={"status": final_status, "message": "Compilación terminada.", "partes": parts, "errores": errors})
                return

            if job_type == "tomos":
                source_ids: list[str] = []
                caratula_id = str(payload.get("caratula_id") or "").strip()
                if caratula_id:
                    source_ids.append(caratula_id)
                source_ids.extend(str(value).strip() for value in payload["file_ids"] if str(value).strip())

                with tomos_semaphore:
                    uploaded, total_pages, errors = assemble_tomo(
                        service=service, source_ids=source_ids,
                        destination_folder_id=str(payload["destination_folder_id"]),
                        output_filename=str(payload["output_filename"]),
                        progress_callback=progress_callback,
                        replace_existing=as_bool(payload.get("replace_existing")),
                        strict_mode=as_bool(payload.get("strict_mode", True)),
                        expected_pages=int(payload.get("expected_pages") or 0),
                        expected_source_count=int(payload.get("expected_source_count") or 0),
                    )

                if not uploaded:
                    failure = errors[-1] if errors else {"detail": "No se pudo leer ninguna página.",
                                                        "stage": "unknown", "error_code": "ERROR_PROCESAMIENTO"}
                    update_job(job_id, job_state="failed", status="error",
                               detail=failure["detail"],
                               result={"errores": errors, "error_code": failure.get("error_code"),
                                       "stage": failure.get("stage"), "diagnostics": failure.get("diagnostics")})
                    return

                final_status = "partial" if errors else "success"
                update_job(job_id, job_state="finished", status=final_status,
                           result={"status": final_status, "message": "Tomo ensamblado.", "id": uploaded["id"],
                                   "url": uploaded["url"], "final_name": uploaded["final_name"],
                                   "paginas": total_pages, "errores": errors})
                return

    except Exception as exc:
        app.logger.exception("Error crítico en trabajo %s", job_id)
        if job_type == "tomos":
            with jobs_lock:
                stage = str((jobs.get(job_id) or {}).get("progress", {}).get("stage") or "unknown")
            failure = tomo_failure(exc, stage)
            update_job(job_id, job_state="failed", status="error", detail=failure["detail"],
                       result={"errores": [failure], "stage": failure["stage"],
                               "error_code": failure["error_code"], "diagnostics": failure["diagnostics"]})
        else:
            update_job(job_id, job_state="failed", status="error", detail=str(exc))
    finally:
        gc.collect()


# ================================================================
# RUTAS HTTP
# ================================================================

from panel_routes import panel_bp  # noqa: E402  (registrado luego de definir `app`)
app.register_blueprint(panel_bp)


@app.get("/health")
def health():
    cleanup_expired_jobs()
    with jobs_lock:
        active_jobs = sum(1 for job in jobs.values() if job.get("job_state") in {"queued", "running"})
    return jsonify({"status": "ok", "service": "sistema-maestro-pdf", "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
                    "active_jobs": active_jobs, "server_instance_id": SERVER_INSTANCE_ID,
                    "started_at": SERVER_STARTED_AT}), 200


@app.get("/trabajos/<job_id>")
def consultar_trabajo(job_id: str):
    try:
        get_bearer_token()
        public_job = get_public_job(str(job_id).strip())
        if not public_job:
            return jsonify({"status": "error", "job_state": "not_found", "server_instance_id": SERVER_INSTANCE_ID,
                            "detail": "Trabajo no encontrado en esta instancia. Puede haber vencido o el servidor reinició; "
                                      "revisa los registros de Railway para conocer la causa. "
                                      "Un 404 por sí solo no demuestra falta de memoria."}), 404
        return jsonify(public_job), 200
    except ValueError as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Error consultando trabajo %s", job_id)
        return jsonify({"status": "error", "detail": str(exc)}), 500


@app.post("/compilar")
def compilar_general():
    try:
        data = validate_payload(request.get_json(silent=True), ["file_ids", "destination_folder_id", "output_filename"])
        token = get_bearer_token()
        file_ids = [str(value).strip() for value in data["file_ids"] if str(value).strip()]

        if not file_ids:
            raise ValueError("La lista file_ids está vacía.")
        if len(file_ids) > 3000:
            raise ValueError("La solicitud supera el máximo de 3000 archivos.")

        data["file_ids"] = file_ids
        data["limite_paginas"] = int(data.get("limite_paginas", 500))

        if as_bool(data.get("modo_async")):
            request_id = str(data.get("request_id") or uuid.uuid4().hex).strip()
            job_id, created = create_or_reuse_job(job_type="compilar", request_id=request_id, token=token, payload=data)
            return jsonify({"status": "accepted", "job_state": "queued", "job_id": job_id, "request_id": request_id,
                            "reused": not created, "poll_url": f"/trabajos/{job_id}", "message": "Trabajo recibido."}), 202

        service = get_drive_service(token)
        with job_semaphore:
            parts, errors = compile_in_parts(
                service=service, file_ids=file_ids, destination_folder_id=str(data["destination_folder_id"]),
                output_filename=str(data["output_filename"]), page_limit=int(data.get("limite_paginas", 500)),
                replace_existing=as_bool(data.get("replace_existing")), strict_mode=as_bool(data.get("strict_mode", True)),
                expected_source_count=int(data.get("expected_source_count") or 0),
            )

        if not parts:
            return jsonify({"status": "error", "detail": (errors[-1].get("detail") if errors else "No se pudo generar ningún PDF."), "errores": errors}), 422
        return jsonify({"status": "partial" if errors else "success", "message": "Compilación terminada.", "partes": parts, "errores": errors}), 200

    except ValueError as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Error crítico en /compilar")
        return jsonify({"status": "error", "detail": str(exc)}), 500




# ================================================================
# TOMOS: conteo REAL de páginas de PDFs sin cantidad en el nombre.
# Se descarga UN archivo por vez, se cuenta con qpdf y se borra el
# temporal. Independiente de las rutas del compilador y de carátulas.
# ================================================================
@app.post("/contar-paginas-tomos")
def contar_paginas_tomos():
    try:
        token = get_bearer_token()
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("file_ids"), list):
            return jsonify({"status": "error", "detail": "Se requiere file_ids (lista de IDs)."}), 400
        file_ids = [str(item).strip() for item in data["file_ids"] if str(item).strip()]
        if not file_ids or len(file_ids) > 8 or len(set(file_ids)) != len(file_ids):
            return jsonify({"status": "error", "detail": "Indica entre 1 y 8 IDs únicos por consulta."}), 400
        service = get_drive_service(token)
        resultados = []
        for file_id in file_ids:
            try:
                metadata = service.files().get(fileId=file_id,
                    fields="id,name,mimeType,size", supportsAllDrives=True).execute(num_retries=3)
                if metadata.get("mimeType") != "application/pdf":
                    raise ValueError("El archivo no es un PDF.")
                with tempfile.TemporaryDirectory(prefix="tomo_paginas_") as temp_dir:
                    tamano = int(metadata.get("size") or 0)
                    disco_libre = shutil.disk_usage(temp_dir).free
                    if tamano and tamano + 64 * 1024 * 1024 > disco_libre:
                        raise RuntimeError("ESPACIO_INSUFICIENTE: no cabe el PDF temporal para contar páginas.")
                    archivo = download_drive_file_to_path(service, file_id, temp_dir)
                    intento = subprocess.run(["qpdf", "--show-npages", archivo],
                        capture_output=True, text=True, timeout=180, check=False)
                    if intento.returncode not in (0, 3):
                        raise RuntimeError("qpdf: " + (intento.stderr or intento.stdout or
                            "no pudo contar las páginas")[-450:])
                    salida = (intento.stdout or "").strip().splitlines()
                    paginas = int(salida[-1].strip()) if salida else 0
                    if paginas < 1:
                        raise ValueError("El PDF no contiene páginas verificables.")
                    resultados.append({"id": file_id, "nombre": metadata.get("name", ""),
                        "paginas": paginas, "error": ""})
            except Exception as error_pdf:
                app.logger.warning("TOMOS: fallo conteo de PDF %s: %s", file_id, error_pdf)
                resultados.append({"id": file_id, "paginas": 0,
                    "error": str(error_pdf)[:500]})
        return jsonify({"status": "partial" if any(r["error"] for r in resultados) else "success",
            "resultados": resultados}), 200
    except ValueError as error:
        return jsonify({"status": "error", "detail": str(error)}), 400
    except Exception as error:
        app.logger.exception("TOMOS: error general de conteo")
        return jsonify({"status": "error", "detail": str(error)[:500]}), 500


@app.post("/tomos")
def ensamblar_tomo():
    try:
        data = validate_payload(request.get_json(silent=True), ["file_ids", "destination_folder_id", "output_filename"])
        token = get_bearer_token()
        data["file_ids"] = [str(value).strip() for value in data["file_ids"] if str(value).strip()]

        if as_bool(data.get("modo_async")):
            request_id = str(data.get("request_id") or uuid.uuid4().hex).strip()
            job_id, created = create_or_reuse_job(job_type="tomos", request_id=request_id, token=token, payload=data)
            return jsonify({"status": "accepted", "job_state": "queued", "job_id": job_id, "request_id": request_id,
                            "reused": not created, "poll_url": f"/trabajos/{job_id}", "message": "Trabajo de tomo recibido."}), 202

        source_ids: list[str] = []
        caratula_id = str(data.get("caratula_id") or "").strip()
        if caratula_id:
            source_ids.append(caratula_id)
        source_ids.extend(data["file_ids"])

        if not source_ids:
            raise ValueError("No hay archivos para ensamblar.")

        service = get_drive_service(token)
        with job_semaphore:
            with tomos_semaphore:
                uploaded, total_pages, errors = assemble_tomo(
                    service=service, source_ids=source_ids, destination_folder_id=str(data["destination_folder_id"]),
                    output_filename=str(data["output_filename"]), replace_existing=as_bool(data.get("replace_existing")),
                    strict_mode=as_bool(data.get("strict_mode", True)), expected_pages=int(data.get("expected_pages") or 0),
                    expected_source_count=int(data.get("expected_source_count") or 0),
                )

        if not uploaded:
            failure = errors[-1] if errors else {"detail": "No se pudo leer ninguna página.", "stage": "unknown"}
            return jsonify({"status": "error", "detail": failure["detail"],
                            "stage": failure.get("stage"), "error_code": failure.get("error_code"),
                            "diagnostics": failure.get("diagnostics"), "errores": errors}), 422
        return jsonify({"status": "partial" if errors else "success", "message": "Tomo ensamblado.", "id": uploaded["id"],
                        "url": uploaded["url"], "final_name": uploaded["final_name"], "paginas": total_pages, "errores": errors}), 200

    except ValueError as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Error crítico en /tomos")
        failure = tomo_failure(exc, "request")
        return jsonify({"status": "error", "detail": failure["detail"], "stage": failure["stage"],
                        "error_code": failure["error_code"], "diagnostics": failure["diagnostics"],
                        "errores": [failure]}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
