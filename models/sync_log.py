from datetime import timedelta

from odoo import _, api, fields, models

from .constants import DEFAULT_LOG_RETENTION_DAYS


class SyncSyscomLog(models.Model):
    _name = "sync.syscom.log"
    _description = "Log de sincronización SYSCOM"
    _order = "create_date desc"

    name = fields.Char(string="Nombre", required=True)
    kind = fields.Selection(
        [
            ("info", "Info"),
            ("warn", "Advertencia"),
            ("error", "Error"),
        ],
        string="Tipo",
        default="info",
        required=True,
    )
    message = fields.Text(string="Mensaje")

    @api.model
    def notify_admin_on_critical_error(self, subject, message):
        """Envía una actividad/nota interna a los usuarios del grupo sync_syscom_manager.

        Se llama desde _mark_error() de los jobs para alertar al equipo cuando
        un proceso crítico falla (token vencido, API caída, error de BD, etc.).
        """
        try:
            group = self.env.ref("sync_syscom.group_sync_syscom_manager", raise_if_not_found=False)
            if not group:
                return
            users = group.users
            if not users:
                return

            # Creamos una actividad "To-Do" en el log más reciente del tipo error.
            # Si no hay modelo de actividades (raro), caemos en silencio.
            if "mail.activity" not in self.env.registry.models:
                return

            ActivityType = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
            if not ActivityType:
                return

            # Usamos el propio log como anclaje de la actividad.
            log = self.sudo().create({
                "name": subject,
                "kind": "error",
                "message": message,
            })

            for user in users:
                self.env["mail.activity"].sudo().create({
                    "res_model_id": self.env["ir.model"]._get_id(self._name),
                    "res_id": log.id,
                    "activity_type_id": ActivityType.id,
                    "summary": subject,
                    "note": "<p>%s</p>" % message,
                    "user_id": user.id,
                })
        except Exception:
            # Las notificaciones nunca deben interrumpir el job principal.
            pass

    @api.model
    def cron_purge_old_logs(self):
        """Elimina logs más antiguos que el periodo de retención configurado.

        El periodo se configura en 'sync_syscom.log_retention_days' (días).
        Por defecto: 90 días. Nunca elimina registros de tipo 'error'.
        """
        params = self.env["ir.config_parameter"].sudo()
        try:
            retention_days = int(
                params.get_param("sync_syscom.log_retention_days") or DEFAULT_LOG_RETENTION_DAYS
            )
        except (TypeError, ValueError):
            retention_days = DEFAULT_LOG_RETENTION_DAYS
        retention_days = max(retention_days, 7)  # mínimo 7 días por seguridad

        cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        old_logs = self.search([
            ("create_date", "<", fields.Datetime.to_string(cutoff)),
            ("kind", "!=", "error"),  # conservar errores para auditoría
        ])
        count = len(old_logs)
        if old_logs:
            old_logs.unlink()

        self.sudo().create({
            "name": _("Purga de logs SYSCOM"),
            "kind": "info",
            "message": _(
                "Purga completada. Eliminados: %(count)s registros anteriores a %(cutoff)s (retención: %(days)s días)."
            ) % {"count": count, "cutoff": cutoff.date(), "days": retention_days},
        })
