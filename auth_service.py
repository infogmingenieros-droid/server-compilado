"""
auth_service.py
====================================================================
Login propio (usuario/contraseña), independiente de la cuenta de
Google, para poder registrar QUIÉN ejecutó cada carátula/compilado/
tomo.

Requiere una pestaña llamada 'USUARIOS' en el mismo Google Sheet, con
esta estructura (fila 1 = encabezados):

  A: usuario | B: password_hash | C: rol | D: activo

Para dar de alta un usuario, generar el hash con:
    python -c "from werkzeug.security import generate_password_hash as g; print(g('la_contraseña'))"
y pegar usuario + hash + rol (admin/operador) + TRUE en esa hoja.
====================================================================
"""
from __future__ import annotations

import os
from functools import wraps

from flask import jsonify, session
from werkzeug.security import check_password_hash

from google_services import read_table

USUARIOS_HOJA = "USUARIOS"

# Cuenta de respaldo opcional, configurada SOLO por variables de entorno.
# No se guarda ningún usuario, contraseña ni hash en el repositorio.
BACKUP_USER = os.getenv("BACKUP_ADMIN_USER", "").strip()
BACKUP_PASSWORD_HASH = os.getenv("BACKUP_ADMIN_PASSWORD_HASH", "").strip()

USUARIO_RESPALDO = None
if BACKUP_USER and BACKUP_PASSWORD_HASH:
    USUARIO_RESPALDO = {
        "usuario": BACKUP_USER,
        "password_hash": BACKUP_PASSWORD_HASH,
        "rol": os.getenv("BACKUP_ADMIN_ROLE", "admin").strip() or "admin",
        "activo": True,
    }


def _cargar_usuarios() -> dict[str, dict[str, str]]:
    filas = read_table(USUARIOS_HOJA, "A2:D1000")
    usuarios: dict[str, dict[str, str]] = {}
    for fila in filas:
        if not fila or not fila[0]:
            continue
        usuario = str(fila[0]).strip()
        password_hash = fila[1] if len(fila) > 1 else ""
        rol = fila[2] if len(fila) > 2 else "operador"
        activo = (fila[3] if len(fila) > 3 else "TRUE")
        usuarios[usuario.lower()] = {
            "usuario": usuario,
            "password_hash": password_hash,
            "rol": rol or "operador",
            "activo": str(activo).strip().upper() not in ("FALSE", "0", ""),
        }
    return usuarios


def login(usuario: str, password: str) -> dict:
    usuario = str(usuario or "").strip()
    password = str(password or "")
    if not usuario or not password:
        raise ValueError("Usuario y contraseña son obligatorios.")

    registro = None
    if USUARIO_RESPALDO and usuario.lower() == USUARIO_RESPALDO["usuario"].lower():
        registro = USUARIO_RESPALDO
    else:
        try:
            usuarios = _cargar_usuarios()
        except Exception:
            usuarios = {}
        registro = usuarios.get(usuario.lower())

    if not registro or not registro["activo"]:
        raise ValueError("Usuario o contraseña incorrectos.")

    if not check_password_hash(registro["password_hash"], password):
        raise ValueError("Usuario o contraseña incorrectos.")

    session.clear()
    session["usuario"] = registro["usuario"]
    session["rol"] = registro["rol"]
    session.permanent = True
    return {"usuario": registro["usuario"], "rol": registro["rol"]}


def logout() -> None:
    session.clear()


def current_user() -> dict | None:
    if "usuario" not in session:
        return None
    return {"usuario": session["usuario"], "rol": session.get("rol", "operador")}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "usuario" not in session:
            return jsonify({"status": "error", "detail": "Sesión no iniciada."}), 401
        return fn(*args, **kwargs)
    return wrapper
