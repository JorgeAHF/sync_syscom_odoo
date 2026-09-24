import email.utils
import logging
from datetime import datetime, timezone

from odoo import _
from odoo.exceptions import UserError
import requests

from .constants import SYSCOM_DEFAULT_TIMEOUT

_logger = logging.getLogger(__name__)


class SyscomRateLimitError(UserError):
    """HTTP 429 de SYSCOM, con la espera que pide la respuesta.

    Hereda de UserError a proposito: los `except UserError` que ya existen en el
    modulo la siguen atrapando igual, y el mensaje conserva el formato
    "HTTP 429: ..." del que depende `_es_error_definitivo`. Quien quiera tratarla
    distinto la captura por su tipo, antes que a UserError.

    :param retry_after: segundos que pide esperar la cabecera Retry-After, o None
        si la respuesta no la trae (ver DEFAULT_RATE_LIMIT_BACKOFF).
    """

    def __init__(self, message, retry_after=None, status_code=429):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class SyscomClient:
    DEFAULT_TEST_ENDPOINT = "/categorias"

    def __init__(self, base_url, token, timeout=SYSCOM_DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout or SYSCOM_DEFAULT_TIMEOUT

    def ping(self):
        try:
            self._request("GET", self.DEFAULT_TEST_ENDPOINT)
        except UserError as exc:
            return False, str(exc)
        return True, _("Conexión exitosa con SYSCOM.")

    def get_categories(self):
        return self._request("GET", "/categorias")

    def get_category_detail(self, category_id):
        return self._request("GET", f"/categorias/{category_id}")

    def get_brands(self):
        return self._request("GET", "/marcas")

    def get_brand_detail(self, brand_id, timeout=None):
        return self._request("GET", f"/marcas/{brand_id}", timeout_override=timeout)

    def get_brand_products(self, brand_id, page=1, stock=None):
        params = []
        if stock is not None:
            params.append(f"stock={int(bool(stock))}")
        if page and page > 1:
            params.append(f"pagina={page}")
        query = f"?{'&'.join(params)}" if params else ""
        return self._request("GET", f"/marcas/{brand_id}/productos{query}")

    def get_product_detail(self, product_id):
        return self._request("GET", f"/productos/{product_id}")

    def get_exchange_rate(self):
        """Obtiene tipo de cambio de SYSCOM (/tipocambio)."""
        return self._request("GET", "/tipocambio")

    def _request(self, method, endpoint, timeout_override=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        timeout_value = timeout_override or self.timeout or SYSCOM_DEFAULT_TIMEOUT
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=(timeout_value, timeout_value),
            )
        except requests.exceptions.Timeout as exc:
            raise UserError(_("Timeout al conectar con SYSCOM")) from exc
        except requests.exceptions.ConnectionError as exc:
            raise UserError(_("Error de conexión con SYSCOM: %s") % exc) from exc
        except requests.exceptions.RequestException as exc:
            raise UserError(_("Error HTTP al conectar con SYSCOM: %s") % exc) from exc

        if response.status_code == 429:
            raise SyscomRateLimitError(
                self._format_error(response),
                retry_after=self._segundos_de_espera(response),
            )

        if response.status_code >= 400:
            raise UserError(self._format_error(response))

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise UserError(_("Respuesta inválida de SYSCOM.")) from exc

    @staticmethod
    def _segundos_de_espera(response):
        """Lee la cabecera Retry-After. Devuelve segundos (>=0) o None si no viene.

        RFC 9110 admite dos formatos y hay que soportar los dos: un entero de
        segundos ("Retry-After: 120") o una fecha HTTP
        ("Retry-After: Wed, 21 Oct 2026 07:28:00 GMT").

        Se registra siempre el valor crudo. Al 20/08/2026 no sabemos si SYSCOM
        manda esta cabecera --se recogieron 41 respuestas 429 sin capturarla-- y
        esta linea de log es la que va a contestarlo, sin gastar una sola llamada
        extra: basta con esperar al proximo 429 natural.
        """
        crudo = (response.headers or {}).get("Retry-After")
        if not crudo:
            _logger.info("SYSCOM 429 sin cabecera Retry-After; se usara el respaldo configurado")
            return None

        crudo = str(crudo).strip()
        _logger.info("SYSCOM 429 con Retry-After: %r", crudo)

        try:
            return max(int(crudo), 0)
        except (TypeError, ValueError):
            pass

        try:
            cuando = email.utils.parsedate_to_datetime(crudo)
        except (TypeError, ValueError):
            _logger.warning("Retry-After de SYSCOM ilegible: %r", crudo)
            return None
        if cuando is None:
            return None
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=timezone.utc)
        return max(int((cuando - datetime.now(timezone.utc)).total_seconds()), 0)

    @staticmethod
    def _format_error(response):
        try:
            payload = response.json()
        except ValueError:
            payload = None

        detail = None
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("title")

        if not detail:
            detail = response.text or _("Respuesta sin detalle")

        return _("HTTP %s: %s") % (response.status_code, detail)
