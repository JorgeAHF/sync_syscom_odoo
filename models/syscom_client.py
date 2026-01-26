import logging

from odoo import _

try:
    import requests
except Exception:  # pragma: no cover - fallback when requests isn't available
    requests = None

try:
    import urllib3
except Exception:  # pragma: no cover - urllib3 should be available in Odoo
    urllib3 = None

_logger = logging.getLogger(__name__)


class SyscomClientError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class SyscomClient:
    DEFAULT_TEST_ENDPOINT = "/api/v1/categories"

    def __init__(self, base_url, token, timeout=30):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout or 30

    def test_connection(self):
        endpoint = self._get_test_endpoint()
        url = f"{self.base_url}{endpoint}"
        masked_token = self._mask_token(self.token)
        header_variants = [
            {"Authorization": f"Bearer {self.token}"},
            {"X-API-KEY": self.token},
        ]

        last_error = None
        for headers in header_variants:
            try:
                status_code = self._request(url, headers)
                _logger.info(
                    "Syscom test connection status %s using token %s",
                    status_code,
                    masked_token,
                )
                return status_code
            except SyscomClientError as exc:
                _logger.warning(
                    "Syscom test connection failed using token %s: %s",
                    masked_token,
                    exc,
                )
                if exc.status_code == 401 and headers is not header_variants[-1]:
                    last_error = exc
                    continue
                raise

        if last_error:
            raise last_error

        raise SyscomClientError(_("No fue posible validar el token"))

    def _get_test_endpoint(self):
        return self.DEFAULT_TEST_ENDPOINT

    def _request(self, url, headers):
        if requests:
            return self._request_requests(url, headers)
        return self._request_urllib3(url, headers)

    def _request_requests(self, url, headers):
        try:
            response = requests.get(
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise SyscomClientError(_("Timeout al conectar con SYSCOM")) from exc
        except requests.exceptions.ConnectionError as exc:
            raise SyscomClientError(_("Error de conexión con SYSCOM: %s") % exc) from exc
        except requests.exceptions.RequestException as exc:
            raise SyscomClientError(_("Error HTTP al conectar con SYSCOM: %s") % exc) from exc

        if response.status_code >= 400:
            raise SyscomClientError(
                _("HTTP %s: %s") % (response.status_code, response.text),
                status_code=response.status_code,
            )
        return response.status_code

    def _request_urllib3(self, url, headers):
        if urllib3 is None:
            raise SyscomClientError(_("No hay librería HTTP disponible"))

        pool = urllib3.PoolManager()
        timeout = urllib3.Timeout(total=self.timeout)
        try:
            response = pool.request(
                "GET",
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=timeout,
            )
        except urllib3.exceptions.ConnectTimeoutError as exc:
            raise SyscomClientError(_("Timeout al conectar con SYSCOM")) from exc
        except urllib3.exceptions.ReadTimeoutError as exc:
            raise SyscomClientError(_("Timeout al leer respuesta de SYSCOM")) from exc
        except urllib3.exceptions.MaxRetryError as exc:
            raise SyscomClientError(_("Error de conexión con SYSCOM: %s") % exc) from exc
        except urllib3.exceptions.HTTPError as exc:
            raise SyscomClientError(_("Error HTTP al conectar con SYSCOM: %s") % exc) from exc

        if response.status >= 400:
            body = (response.data or b"").decode("utf-8", errors="ignore")
            raise SyscomClientError(
                _("HTTP %s: %s") % (response.status, body),
                status_code=response.status,
            )
        return response.status

    @staticmethod
    def _mask_token(token):
        if not token:
            return "(vacío)"
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}***{token[-4:]}"
