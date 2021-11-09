from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from . import pg_advisory_lock
import threading

class Task(models.Model):
    _name = 'cicd.task'
    _order = 'date desc'

    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    name = fields.Char("Name")
    date = fields.Datetime("Date", default=lambda self: fields.Datetime.now())
    state = fields.Selection([
        ('new', 'New'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ])
    log = fields.Text("Log")
    lockbit = fields.Datetime()

    def perform(self):
        self.ensure_one()
        pg_advisory_lock(self.env.cr, f"performat_task_{self.id}")

    def _cron_run(self):
        for task in self.search([
            ('state', '=', 'new')
        ]):
            task.perform()

    def _make_cron(self, uuid, object, active):
        key = f"{uuid}_{object._name}_{object.id}"
        crons = self.env['ir.cron'].with_context(active_test=False).search([('name', '=', key)], limit=1)
        if not crons:
            crons = crons.create({
                'name': key,
                'model_id': self.env['ir.model'].search([('model', '=', object._name)]).id,
                'interval_number': 1,
                'numbercall': -1,
                'interval_type': 'minutes',
            })
        if crons.active != active:
            crons.active = active