"""
panel_routes.py
====================================================================
Blueprint con las rutas /api/* que usa WebPanel.html / Sidebar.html de Apps Script.
Se registra en app.py con:

    from panel_routes import panel_bp
    app.register_blueprint(panel_bp)

Requiere las variables de entorno:
  SHEET_ID                      -> ID del Google Sheet (el mismo de siempre)
  GOOGLE_SERVICE_ACCOUNT_JSON    -> JSON de la cuenta de servicio (o GOOGLE_SERVICE_ACCOUNT_FILE)
  FLASK_SECRET_KEY               -> clave para firmar la cookie de sesión
====================================================================
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import auth_service
import caratulas_service
import compilador_service
import google_services as gs

panel_bp = Blueprint("panel", __name__, url_prefix="/api")


def _err(exc: Exception, code: int = 400):
    return jsonify({"status": "error", "detail": str(exc)}), code


# ====================================================================
# AUTENTICACIÓN
# ====================================================================

@panel_bp.post("/login")
def api_login():
    try:
        data = request.get_json(silent=True) or {}
        resultado = auth_service.login(data.get("usuario"), data.get("password"))
        return jsonify({"status": "success", **resultado})
    except ValueError as exc:
        return _err(exc, 401)
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.post("/logout")
def api_logout():
    auth_service.logout()
    return jsonify({"status": "success"})


@panel_bp.get("/me")
def api_me():
    usuario = auth_service.current_user()
    if not usuario:
        return jsonify({"status": "error", "detail": "Sesión no iniciada."}), 401
    return jsonify({"status": "success", **usuario})


# ====================================================================
# CONFIGURACIÓN (Panel de Parámetros de cada hoja)
# ====================================================================

@panel_bp.get("/config/<hoja>")
@auth_service.login_required
def api_config(hoja: str):
    """Devuelve C2:C8 de la hoja pedida (CARATULAS Y COMPILADOS o TOMOS) tal como se ven en el Sheet."""
    try:
        filas = gs.read_table(hoja, "B2:C8")
        etiquetas = []
        for fila in filas:
            etiqueta = fila[0] if len(fila) > 0 else ""
            valor = fila[1] if len(fila) > 1 else ""
            etiquetas.append({"label": etiqueta, "value": valor})
        return jsonify({"status": "success", "hoja": hoja, "parametros": etiquetas})
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.get("/plantillas")
@auth_service.login_required
def api_plantillas():
    try:
        modelos = caratulas_service.obtener_modelos_de_caratula("CARATULAS Y COMPILADOS")
        return jsonify(modelos)
    except Exception as exc:
        return _err(exc, 500)


# ====================================================================
# NAVEGACIÓN DE DRIVE (árbol de carpetas)
# ====================================================================

@panel_bp.get("/carpetas/raiz/<hoja>/<celda>")
@auth_service.login_required
def api_carpeta_raiz(hoja: str, celda: str):
    try:
        folder_id = gs.get_id_from_cell(hoja, celda)
        return jsonify({"status": "success", "id": folder_id})
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.get("/carpetas/<folder_id>/subcarpetas")
@auth_service.login_required
def api_subcarpetas(folder_id: str):
    try:
        return jsonify(gs.list_subfolders(folder_id))
    except Exception as exc:
        return _err(exc, 500)


# ====================================================================
# CARÁTULAS
# ====================================================================

@panel_bp.post("/caratulas/procesar")
@auth_service.login_required
def api_caratulas_procesar():
    try:
        data = request.get_json(silent=True) or {}
        resultado = caratulas_service.procesar_seleccionados(
            data.get("seleccionados") or [], data.get("ubicacion") or {}, data.get("caratula") or {},
        )
        return jsonify(resultado)
    except ValueError as exc:
        return _err(exc, 400)
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.post("/caratulas/restaurar-base")
@auth_service.login_required
def api_caratulas_restaurar_base():
    try:
        data = request.get_json(silent=True) or {}
        resultado = caratulas_service.restaurar_caratulas_base(data.get("idPlantilla"), data.get("tipo"))
        return jsonify(resultado)
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.post("/caratulas/crear-carpeta")
@auth_service.login_required
def api_caratulas_crear_carpeta():
    try:
        data = request.get_json(silent=True) or {}
        resultado = caratulas_service.crear_carpeta_libre(data.get("nombre"))
        return jsonify(resultado)
    except ValueError as exc:
        return _err(exc, 400)
    except Exception as exc:
        return _err(exc, 500)


# ====================================================================
# COMPILADOR
# ====================================================================

@panel_bp.post("/compilador/ejecutar")
@auth_service.login_required
def api_compilador_ejecutar():
    try:
        from app import compile_in_parts  # se importa acá para evitar import circular

        data = request.get_json(silent=True) or {}
        usuario = auth_service.current_user()["usuario"]
        resultado = compilador_service.procesar_compilacion(
            data.get("seleccionados") or [], data.get("config") or {},
            compile_in_parts, gs.drive_service(), usuario,
        )
        return jsonify(resultado)
    except ValueError as exc:
        return _err(exc, 400)
    except Exception as exc:
        return _err(exc, 500)


@panel_bp.get("/compilador/servidor/probar")
@auth_service.login_required
def api_probar_servidor_interno():
    # El compilador ahora corre en el mismo proceso: "probar servidor" es un health-check local.
    return jsonify({"status": "success", "mensaje": "✅ Servidor activo (compilador integrado en este mismo servicio)."})


# ====================================================================
# HISTORIAL
# ====================================================================

@panel_bp.get("/historial/<hoja>")
@auth_service.login_required
def api_historial(hoja: str):
    try:
        filas = gs.read_table(hoja, "A15:F500")
        filas = [f for f in filas if any(f)]
        filas = list(reversed(filas))[:100]
        return jsonify({"status": "success", "filas": filas})
    except Exception as exc:
        return _err(exc, 500)
