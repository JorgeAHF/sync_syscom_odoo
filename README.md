# SyncSyscom (Odoo 19) - Integracion SYSCOM (Dropship)

Modulo para Odoo 19 que sincroniza catalogos (categorias, marcas y modelos) desde la API de SYSCOM, permite publicar productos en Odoo/eCommerce, mantiene precios/costo en MXN, expone disponibilidad al cliente, y bloquea checkout/confirmacion cuando no hay stock o la API falla (politica dropship).

> Nota: este README evita incluir credenciales. El token de SYSCOM se configura en Ajustes.

## Funcionalidades (resumen)

- Catalogos (staging):
  - Categorias SYSCOM: `sync.syscom.category` (arbol + niveles 1/2/3).
  - Marcas SYSCOM: `sync.syscom.brand`.
  - Modelos/Productos SYSCOM (staging): `sync.syscom.product`.
- Publicacion de productos:
  - Crea/actualiza `product.template` por `default_code` (modelo SYSCOM).
  - Publica automaticamente en Website (si `website_sale` esta instalado).
  - Sincroniza multiples imagenes (portada + galeria).
  - Sincroniza recursos/documentos (URLs) como "Documentos del producto" (`product.document`) para que aparezcan en la PDP.
- Precios / costo (MXN):
  - Crea 3 listas de precios (MXN): Lista / Especial / Descuento.
  - Actualiza items por producto en esas listas.
  - Calcula costo (`standard_price`) a partir del precio especial con un % configurable (default 4%).
- Stock (dropship):
  - Guarda stock informativo en el producto (solo `existencia.nuevo`).
  - Muestra disponibilidad al cliente en la pagina de producto (PDP).
  - Bloquea checkout y revalida al confirmar el pedido:
    - Si la API falla -> bloquea (no deja pagar).
    - Si qty > stock -> no confirma y deja el pedido como cotizacion.
- Jobs en segundo plano:
  - Pipeline por cron para sincronizar catalogos (categorias -> marcas -> productos).
  - Refresco en background de stock/precios/costo (cada X horas, configurable, con "skip" interno).
  - Publicacion en background por batches (Publicar seleccionados) para evitar timeouts.
- CFDI / Mexico (best-effort):
  - Mapea `sat_key` de SYSCOM a UNSPSC (si el campo/modelo existe).
  - Mapea `clave_unidad_sat` a UoM (si el campo de localizacion existe en `uom.uom`).

## Requisitos

- Odoo 19
- Dependencias (ver `__manifest__.py`):
  - `base`, `product`, `stock`, `sale`, `website_sale`
- Acceso a Internet desde el servidor para consumir la API de SYSCOM y descargar imagenes/recursos.

## Instalacion / Actualizacion

1) Copiar el modulo al path de addons (ej. `/var/lib/odoo/.local/share/Odoo/addons/19.0/sync_syscom`).
2) Actualizar lista de apps y/o instalar:
   - Apps -> activar modo desarrollador (si aplica) -> actualizar apps -> instalar "Sync Syscom".

### Actualizacion por git (ejemplo)

```bash
cd /var/lib/odoo/.local/share/Odoo/addons/19.0/sync_syscom
sudo -u odoo git pull
sudo systemctl stop odoo
sudo -u odoo /usr/bin/odoo -c /etc/odoo/odoo.conf -d <TU_DB> -u sync_syscom --stop-after-init
sudo systemctl start odoo
```

> Tip: si `git pull` se queja de cambios locales, usa `git status` y `git stash` antes de jalar.

## Configuracion (Ajustes)

La app agrega una seccion en Ajustes:

- Ajustes -> SyncSyscom -> Configurar:
  - Token API SYSCOM
  - Base URL (default `https://developers.syscom.mx/api/v1`)
  - Timeout
  - Moneda de origen de precios (`usd` recomendado)
  - Listas de precios destino (Lista/Especial/Descuento)
  - Stock minimo para publicar (default 1)
  - % descuento sobre precio especial para calcular costo (default 4)
  - Refresco en background (activar/desactivar)
  - Refresco cada N horas (default 4)

Parametros tecnicos (`ir.config_parameter`) relevantes:

- `sync_syscom.syscom_api_token`
- `sync_syscom.syscom_base_url`
- `sync_syscom.syscom_timeout`
- `sync_syscom.price_currency`
- `sync_syscom.min_stock`
- `sync_syscom.cost_discount_pct`
- `sync_syscom.stock_refresh_enabled`
- `sync_syscom.stock_refresh_hours`
- `sync_syscom.stock_refresh_last_run` (lo actualiza el cron)
- `sync_syscom.publish_batch_size` (default 10)

## Manual de usuario (breve)

### 1) Sincronizar catalogos (background)

Menu: SyncSyscom -> Sincronizar -> `Categorias/Marcas/Modelos`

Que hace:
- Programa un pipeline de crons que sincroniza:
  1) Categorias
  2) Marcas
  3) Modelos/productos al staging

Esto corre en segundo plano; puedes monitorear avances en:
- SyncSyscom -> Logs

### 2) Seleccionar que modelos traer / publicar

Opcion A (recomendada cuando no quieres todo):
1) SyncSyscom -> Catalogos -> Categorias
2) Marca `Sel` en las categorias que te interesan.
3) SyncSyscom -> Sincronizar -> `Sync Modelos Sel.`

Opcion B (traer todo):
- Usa `Categorias/Marcas/Modelos` y luego filtra en el staging.

### 3) Publicar productos seleccionados (background, por lotes)

1) SyncSyscom -> Catalogos -> Modelos
2) Marca `Sel` en los productos/modelos que quieres publicar.
3) SyncSyscom -> Sincronizar -> `Publicar seleccionados`

Que pasa:
- No publica en ese instante (evita timeouts).
- Marca los seleccionados como `Pendiente` y un cron los procesa en batches (default 10/min).
- Estados en staging:
  - `Pendiente` -> `Procesando` -> `Publicado` o `Error`

### 4) Ver disponibilidad en eCommerce

En la PDP (pagina de producto) se muestra:
- "Disponible para envio: <N>"
- "(Actualizado: <fecha>)"

El numero mostrado corresponde a `existencia.nuevo` de SYSCOM.

### 5) Checkout / Confirmacion (politica dropship)

Durante checkout:
- Se revalida stock contra SYSCOM.
- Si SYSCOM falla: se bloquea el checkout y se muestra un mensaje en carrito.

Al confirmar pedido (Sales Order):
- Se vuelve a validar stock.
- Si qty > stock: no confirma y deja el pedido como cotizacion.

## Jobs / Crons

Definidos en `data/cron.xml`:

- Sync Syscom: categorias (background) -> `cron_sync_syscom_categories`
- Sync Syscom: marcas (background) -> `cron_sync_syscom_brands_full`
- Sync Syscom: productos de marcas (background) -> `cron_sync_syscom_brand_products`
- Sync Syscom: tipo de cambio semanal -> `cron_sync_syscom_exchange_rate`
- Sync Syscom: stock/precios (background) -> `cron_sync_syscom_stock_daily`
- Sync Syscom: publicar seleccionados (background) -> `cron_sync_syscom_publish_selected`

Notas:
- El refresco stock/precios corre con frecuencia corta, pero se "salta" si aun no toca segun `stock_refresh_hours`.
- La publicacion background corre cada minuto, pero si no hay pendientes no hace nada.

## Datos que se guardan en Odoo

### Staging: `sync.syscom.product`
- `selected`: marca para publicar
- precios USD/MXN, tipo de cambio aplicado, stock `nuevo`, payload/imagenes/recursos, y logs de error
- `publish_state`/timestamps para la publicacion en background

### Producto Odoo: `product.template`
- `syscom_is_product`, `syscom_product_id`
- `syscom_stock_new`, `syscom_stock_synced_at`, `syscom_api_ok`
- `syscom_uom_sat`, `syscom_cost_margin_pct`
- `website_description` (descripcion para eCommerce)
- `product.document` (URLs de recursos) y `product.image` (galeria)

## Seguridad / Accesos

La app usa un grupo (ver `security/security.xml`):
- `sync_syscom.group_sync_syscom_manager`

Si un usuario no tiene ese grupo, no vera el menu principal del modulo ni acciones.

## Troubleshooting (comun)

### No aparecen cambios en website
- Asegurate de actualizar el modulo con `-u sync_syscom`.
- Limpia cache del navegador / prueba con `?debug=assets`.

### Errores al actualizar vistas QWeb
Si un `xpath` no encuentra el nodo, Odoo falla al cargar registry. Se debe ajustar el `xpath` para tu version/theme.

### Documentos no publicados
Los recursos se crean como `product.document` (type=url). Si aun aparecen privados, revisa:
- si hay Automated Actions/Studio que reescriben flags (`public`, `shown_on_product_page`).

## Roadmap sugerido

- Integracion de ordenes a SYSCOM (API) al confirmar pago (dropship real).
- Mejoras en mapeo CFDI (impuestos, unidad SAT adicional, etc.) segun reglas fiscales del cliente.
- Colas robustas (queue_job) si se requiere alto volumen de publicacion.
