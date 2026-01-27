from odoo import _
from odoo.exceptions import UserError
import requests


class SyscomClient:
    DEFAULT_TEST_ENDPOINT = "/categorias"

    def __init__(self, base_url, token, timeout=30):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout or 30

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

    def get_brand_detail(self, brand_id):
        return self._request("GET", f"/marcas/{brand_id}")

    def _request(self, method, endpoint):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise UserError(_("Timeout al conectar con SYSCOM")) from exc
        except requests.exceptions.ConnectionError as exc:
            raise UserError(_("Error de conexión con SYSCOM: %s") % exc) from exc
        except requests.exceptions.RequestException as exc:
            raise UserError(_("Error HTTP al conectar con SYSCOM: %s") % exc) from exc

        if response.status_code >= 400:
            raise UserError(self._format_error(response))

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise UserError(_("Respuesta inválida de SYSCOM.")) from exc

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
