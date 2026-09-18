"""Configuración por variables de entorno."""
import os

DB_PATH = os.environ.get("RECURSOS_DB", "/var/lib/recursos-api/recursos.db")

# Fuentes externas (sobreescribibles para pruebas/stage).
JCLIC_PROJECTS_URL = os.environ.get(
    "JCLIC_PROJECTS_URL", "https://clic.xtec.cat/projects/projects.json"
)
H5P_HUB_API = os.environ.get("H5P_HUB_API", "https://hub-api.h5p.org/v1")
EDUHOOT_BASE_URL = os.environ.get("EDUHOOT_BASE_URL", "https://eduhoot.edutictac.es")

# Base del frontend (para construir play_url del visor propio).
APP_BASE_URL = os.environ.get("RECURSOS_APP_URL", "https://recursos.edutictac.es").rstrip("/")

# Integración server-to-server con EduTicTac Play. Sin token no se habilita la
# ingesta: así una instalación que no use Play no expone este canal.
PLAY_INGEST_TOKEN = os.environ.get("PLAY_INGEST_TOKEN", "")
PLAY_ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("PLAY_ALLOWED_HOSTS", "edutictac.es,play.edutictac.es").split(",")
    if host.strip()
}

# Caché de descripciones JClic (evita re-descargar el .jclic en cada sync).
JCLIC_DESCRIPTIONS_CACHE = os.environ.get(
    "JCLIC_DESCRIPTIONS_CACHE", "/var/lib/recursos-api/jclic-descriptions.json"
)

# Proxy de miniaturas: hosts permitidos (evita SSRF y hotlink a terceros).
THUMB_ALLOWED_HOSTS = {
    "clic.xtec.cat",
    "hub-api.h5p.org",
    "eduhoot.edutictac.es",
}

# Comportamiento HTTP (respeto a servidores externos).
HTTP_TIMEOUT = float(os.environ.get("RESOURCES_HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.environ.get("RESOURCES_HTTP_RETRIES", "3"))
HTTP_RATE_DELAY = float(os.environ.get("RESOURCES_HTTP_RATE_DELAY", "0.2"))
USER_AGENT = "EduTicTac-Resources/0.1 (+https://recursos.edutictac.es)"

# Límites de seguridad para paquetes (SCORM/zip).
MAX_ZIP_SIZE = int(os.environ.get("RESOURCES_MAX_ZIP_SIZE", str(200 * 1024 * 1024)))
MAX_ZIP_UNCOMPRESSED = int(os.environ.get("RESOURCES_MAX_ZIP_UNCOMPRESSED", str(512 * 1024 * 1024)))
MAX_ZIP_FILES = int(os.environ.get("RESOURCES_MAX_ZIP_FILES", "2000"))
MAX_ZIP_DEPTH = int(os.environ.get("RESOURCES_MAX_ZIP_DEPTH", "16"))
