from odoo import _
import requests


class SyscomClient:
    DEFAULT_TEST_ENDPOINT = "/api/v1/categories"

    def __init__(self, base_url, token, timeout=30):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout or 30

    def ping(self):
        url = f"{self.base_url}{self.DEFAULT_TEST_ENDPOINT}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            return False, _("Timeout al conectar con SYSCOM")
        except requests.exceptions.ConnectionError as exc:
            return False, _("Error de conexión con SYSCOM: %s") % exc
        except requests.exceptions.RequestException as exc:
            return False, _("Error HTTP al conectar con SYSCOM: %s") % exc

        if response.status_code == 404 and self._is_route_not_found(response):
            return True, _("Conexión establecida, la API respondió 404 en la ruta de prueba.")
        if response.status_code >= 400:
            return False, _("HTTP %s: %s") % (response.status_code, response.text)

        return True, _("Conexión exitosa con SYSCOM.")

    @staticmethod
    def _is_route_not_found(response):
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("code") == 4002:
            return True
        detail = payload.get("detail") or ""
        return "No existe esa ruta" in detail
