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
# El barrido recorre el catálogo entero en lotes y, al terminar la vuelta, descansa.
# Con 4,364 plantillas a 200 por lote son ~22 lotes: a 15 min, un ciclo dura ~5.6 h.
#
# El antiguo DEFAULT_STOCK_REFRESH_HOURS ("Frecuencia de refresco (horas)") se retiró:
# frenaba entre LOTES, no entre ciclos, así que no podía expresar "un ciclo por semana"
# --ponerlo en 168 daba un lote por semana, o sea 22 semanas por ciclo--.
DEFAULT_STOCK_REFRESH_CYCLE_DAYS = 7     # descanso entre vueltas completas al catálogo
DEFAULT_STOCK_REFRESH_BATCH_MINUTES = 15  # separación entre lotes; gobierna el cron 58
# Suelo duro: por debajo de esto un lote de 200 no cabe entre corridas y el cron se
# solaparía consigo mismo contra el rate limit de SYSCOM.
DEFAULT_STOCK_REFRESH_BATCH_MINUTES_MIN = 5

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
#
# BAJADO A 1 el 21/08/2026 al pasar el refresco a ciclo semanal. Con un ciclo por
# semana, exigir 3 intentos seguidos serian TRES SEMANAS hasta despublicar un producto
# retirado.
#
# OJO A COMO SE COMBINAN, que es contraintuitivo: con 1 intento y 24 h, y un ciclo
# semanal, NO se despublica al primer 404. El primer 404 pone la marca de inicio y la
# condicion de horas todavia no se cumple; la despublicacion llega en el SEGUNDO ciclo,
# una semana despues. O sea que SI hay una confirmacion, la del ciclo siguiente, y un
# 404 espurio aislado no llega a despublicar nada.
#
# Efecto real: retirada de verdad fuera de la tienda en ~2 semanas; fallo puntual de la
# API, sin consecuencia. Si alguna vez se quiere despublicar al primer 404, basta con
# poner tambien retirado_404_min_horas a 0: no hace falta tocar codigo.
DEFAULT_RETIRADO_404_MIN_INTENTOS = 1
DEFAULT_RETIRADO_404_MIN_HORAS = 24
