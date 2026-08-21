# Constantes del módulo sync_syscom
# Centraliza todos los valores "mágicos" para facilitar mantenimiento.

# ── API ───────────────────────────────────────────────────────────────────────
SYSCOM_DEFAULT_BASE_URL = "https://developers.syscom.mx/api/v1"
SYSCOM_DEFAULT_TIMEOUT = 30          # segundos
SYSCOM_BRAND_DETAIL_TIMEOUT = 10     # segundos, por marca individual

# ── Paginación ────────────────────────────────────────────────────────────────
# Tamaño de página que devuelve SYSCOM en /marcas/{id}/productos.
# Se usa para detectar si hay más páginas cuando la API no devuelve metadata.
SYSCOM_PAGE_SIZE = 60
# Límite de páginas a iterar para evitar bucles infinitos.
SYSCOM_PAGE_LIMIT = 200

# ── Batches de sincronización ─────────────────────────────────────────────────
DEFAULT_CATEGORY_CHUNK = 5
DEFAULT_BRAND_CHUNK = 10
DEFAULT_PRODUCT_BRAND_CHUNK = 5

# ── Batches de jobs ───────────────────────────────────────────────────────────
DEFAULT_PUBLISH_BATCH_SIZE = 10
DEFAULT_COST_BATCH_SIZE = 200
DEFAULT_DROPSHIP_BATCH_SIZE = 200
DEFAULT_PRODUCT_DATA_BATCH_SIZE = 100
DEFAULT_CATEGORY_PUBLISH_PRODUCT_CHUNK = 100

# ── Precios / costos ──────────────────────────────────────────────────────────
DEFAULT_COST_DISCOUNT_PCT = 4.0      # % de descuento sobre precio_descuento para calcular costo
DEFAULT_MIN_STOCK = 1                # stock_new mínimo para publicar un producto
DEFAULT_PRICE_CURRENCY = "usd"       # "usd" convierte con tipo de cambio; "mxn" usa tal cual

# ── Refresco de stock ─────────────────────────────────────────────────────────
DEFAULT_STOCK_REFRESH_HOURS = 4      # horas mínimas entre ejecuciones del cron de stock

# ── Logs ──────────────────────────────────────────────────────────────────────
DEFAULT_LOG_RETENTION_DAYS = 90      # días que se conservan registros en sync.syscom.log

# ── Reintentos de publicación ─────────────────────────────────────────────────
DEFAULT_PUBLISH_MAX_RETRIES = 3      # número máximo de reintentos antes de marcar "abandoned"

# ── Rate limit (HTTP 429) ─────────────────────────────────────────────────────
# Espera cuando SYSCOM devuelve 429 y NO manda cabecera Retry-After.
# MEDIDO EL 21/08/2026: SYSCOM SI la manda, siempre --550 respuestas 429 con cabecera,
# 0 sin ella-- y pide esperas cortas: 4, 9, 10, 12 segundos. Asi que este respaldo casi
# nunca entra en juego; se conserva por si la API cambia de comportamiento.
DEFAULT_RATE_LIMIT_BACKOFF = 120     # segundos
MAX_RATE_LIMIT_BACKOFF = 1800        # tope del crecimiento exponencial: 30 min
# Aplazamientos consecutivos antes de dar un job por perdido. Sin este tope, un
# rate limit permanente dejaria el job reprogramandose para siempre.
MAX_RATE_LIMIT_POSTPONES = 5

# ── Retirados de SYSCOM (HTTP 404) ────────────────────────────────────────────
# Condiciones para despublicar solo un producto que SYSCOM ya no lista. Se exigen
# LAS DOS: el contador mide intentos y el numero de intentos depende de que el cron
# corra a su ritmo; las horas son horas pase lo que pase.
DEFAULT_RETIRADO_404_MIN_INTENTOS = 3
DEFAULT_RETIRADO_404_MIN_HORAS = 24
