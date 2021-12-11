from odoo import _, api, fields, models
import logging
logger = logging.getLogger(__name__)

class Dump(models.Model):
    _inherit = ['cicd.mixin.size']
    _name = 'cicd.dump'
    _order = 'date_modified desc'

    active = fields.Boolean("Active", default=True)
    name = fields.Char("Name", required=True, readonly=True)
    machine_id = fields.Many2one("cicd.machine", string="Machine", required=True, readonly=True)
    date_modified = fields.Datetime("Date Modified", readonly=True)

    def download(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            #'url': 'http://www.mut.de',
            'url': f'/download/dump/{self.id}',
            'target': 'new'
        }
        
    def unlink(self):
        for rec in self:
            with self.machine_id._shell() as shell:
                if shell.exists(rec.name):
                    shell.unlink(rec.name)

        return super().unlink()

    @api.model
    def _cron_update(self):
        for machine in self.env['cicd.machine'].sudo().search([]):
            self._update_dumps(machine)

    @api.constrains("name")
    def _check_name(self):
        for rec in self:
            while rec.name.endswith("/"):
                rec.name = rec.name[:-1]

    def _update_dumps(self, machine):
        with machine._shell() as shell:
            for volume in machine.volume_ids.filtered(lambda x: x.ttype in ['dumps', 'dumps_in']):
                files = machine._execute_shell([
                    "ls", volume.name + "/"
                ]).output.strip().split("\n")

                for file in files:
                    if not file:
                        continue
                    path = volume.name + "/" + file
                    dumps = self.sudo().with_context(active_test=False).search([('name', '=', path), ('machine_id', '=', machine.id)])
                    if not dumps:
                        dumps = dumps.sudo().create({
                            'name': path,
                            'machine_id': machine.id,
                        })
                    try:
                        machine._execute_shell(['/usr/bin/test', '-f', path])
                    except Exception as ex:
                        logger.error(ex)
                    else:
                        dumps.size = int(machine._execute_shell([
                            'stat', '-c', '%s', path
                        ]).output.strip())
                        dumps.date_modified = machine._execute_shell([
                            'date', '-r', path, '+%Y-%m-%d %H:%M:%S', '-u',
                        ]).output.strip()