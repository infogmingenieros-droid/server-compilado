# Puesta en marcha de SV-Compi (backend Railway)

Railway funciona únicamente como **backend/API y motor de procesamiento**.
La interfaz (`Sidebar.html`, `WebPanel.html` y `WebApp.gs`) se ejecuta en
Google Apps Script y se comunica con este servicio por HTTP.

El Google Sheet sigue siendo la fuente de verdad para la configuración,
usuarios, carátulas y registros.

## 1. Crear la cuenta de servicio de Google

1. Ve a [Google Cloud Console](https://console.cloud.google.com/) → usa el
   mismo proyecto que ya tengas, o crea uno nuevo.
2. Habilita estas 4 APIs: **Google Sheets API**, **Google Drive API**,
   **Google Docs API**, **Google Slides API**.
3. Ve a *IAM y administración → Cuentas de servicio → Crear cuenta de servicio*.
   No necesita ningún rol de proyecto (los permisos reales se dan
   compartiendo el Sheet/Drive, ver paso 2).
4. Entra a la cuenta creada → *Claves → Agregar clave → Crear clave nueva → JSON*.
   Se descarga un archivo `.json`. Guárdalo, lo necesitas en el paso 3.
5. Copia el **email** de la cuenta de servicio (algo como
   `panel-maestro@tu-proyecto.iam.gserviceaccount.com`).

## 2. Compartir el Sheet y las carpetas de Drive

La cuenta de servicio necesita acceso **igual que un usuario más**:

1. Abre el Google Sheet del sistema → **Compartir** → agrega el email de
   la cuenta de servicio como **Editor**.
2. Comparte también, como Editor, **cada carpeta raíz** referenciada en
   las celdas del Panel de Parámetros (C2, C3, C4, C6 de "CARATULAS Y
   COMPILADOS"; C3, C4, C5, C6 de "TOMOS"). Al compartir una carpeta raíz,
   Drive da acceso automáticamente a todo lo que esté dentro.

Si algún link apunta a "Compartido conmigo" y no a una carpeta que sea
directamente tuya, muévela o vuelve a compartirla explícitamente con la
cuenta de servicio — las cuentas de servicio no heredan "Compartido conmigo".

## 3. Crear la pestaña USUARIOS (login propio)

En el mismo Sheet, crea una pestaña llamada exactamente **USUARIOS** con
estas columnas desde la fila 1:

| A (usuario) | B (password_hash) | C (rol)  | D (activo) |
|---|---|---|---|
| jadmin | (hash) | admin | TRUE |

Para generar el hash de una contraseña, en tu computadora con Python:

```bash
python3 -c "from werkzeug.security import generate_password_hash as g; print(g('la_contraseña_elegida'))"
```

Pega el resultado en la columna B. Nunca guardes la contraseña en texto plano.

## 4. Variables de entorno en Railway

Agrega estas variables al servicio (además de las que ya tenías):

| Variable | Valor |
|---|---|
| `SHEET_ID` | El ID del Google Sheet (la parte de la URL entre `/d/` y `/edit`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | El contenido **completo** del archivo `.json` descargado en el paso 1, pegado como una sola línea/valor |
| `FLASK_SECRET_KEY` | Cualquier cadena larga y aleatoria, para firmar la cookie de sesión |
| `SISTEMA_MAESTRO_KEY` | Clave privada entre Apps Script y Railway (recomendado) |
| `MAX_CONCURRENT_JOBS` | Trabajos PDF simultáneos (por defecto `2`) |
| `UPLOAD_CHUNK_SIZE` | Tamaño de chunk en bytes para I/O de Drive (por defecto 4 MiB) |
| `GC_COLLECT_EVERY_FILES` | GC forzado cada N PDFs (por defecto `20`) |
| `BACKUP_ADMIN_USER` | Usuario de respaldo opcional (recomendado evitarlo si USUARIOS funciona) |
| `BACKUP_ADMIN_PASSWORD_HASH` | Hash Werkzeug del usuario de respaldo; **nunca la contraseña** |
| `BACKUP_ADMIN_ROLE` | Rol opcional del respaldo (por defecto `admin`) |

## 5. Archivos del proyecto

El backend no necesita servir HTML. Los archivos principales son:

```
app.py
panel_routes.py
google_services.py
auth_service.py
caratulas_service.py
compilador_service.py
requirements.txt
Procfile
```

La interfaz vive en Apps Script, fuera de este repositorio:

```
Sidebar.html
WebPanel.html
WebApp.gs
```

## 6. Qué funciona ya y qué falta

✅ **Login** con usuario/contraseña propios (pestaña USUARIOS), queda
   registrado quién hace cada acción.
✅ **Panel de Parámetros**: la página lee C2:C8 en vivo desde el Sheet.
✅ **Carátulas**: generación desde plantilla (Docs/Slides), restauración
   de carátulas base de Anexos 11/13, creación de carpetas — igual que
   el original, escribiendo el log en la hoja "CARATULAS Y COMPILADOS".
✅ **Compilador**: arma la secuencia (reglas de Anexo 11/13 incluidas) y
   compila en el mismo servidor — ya no hace falta un servidor Colab
   aparte para esto, todo corre en Railway.
✅ **Historial**: la tabla inferior lee directamente las filas ya
   guardadas en el Sheet.

✅ **Tomos**: el ensamblado de tomos ya está integrado en el backend.

### Simplificación conocida
El original centraba automáticamente ciertos textos dentro de la
carátula (alineación de párrafo). La versión Python reemplaza el texto
pero no reproduce ese centrado automático — si la plantilla ya tiene el
placeholder centrado por estilo, se ve igual; si no, hay que ajustarlo
en la plantilla una sola vez.
