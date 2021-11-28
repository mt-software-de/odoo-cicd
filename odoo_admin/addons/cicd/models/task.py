import arrow
import traceback
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo import registry
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from . import pg_advisory_lock
from contextlib import contextmanager

class Task(models.Model):
    _name = 'cicd.task'
    _order = 'date desc'

    model = fields.Char("Model")
    res_id = fields.Integer("ID")
    display_name = fields.Char(compute="_compute_display_name")
    machine_id = fields.Many2one('cicd.machine', string="Machine", readonly=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    name = fields.Char("Name")
    date = fields.Datetime("Date", default=lambda self: fields.Datetime.now(), readonly=True)
    is_done = fields.Boolean(compute="_compute_is_done", store=True)

    state = fields.Selection(related='queue_job_id.state', string="State")
    log = fields.Text("Log", readonly=True)
    error = fields.Text("Exception", readonly=True)
    dump_used = fields.Char("Dump used", readonly=True)
    duration = fields.Integer("Duration [s]", readonly=True)
    commit_id = fields.Many2one("cicd.git.commit", string="Commit", readonly=True)
    queue_job_id = fields.Many2one('queue.job', string="Queuejob")

    @api.depends('state')
    def _compute_is_done(self):
        for rec in self:
            rec.is_done = rec.state in ['done', 'failed']


    def _compute_display_name(self):
        for rec in self:
            name = rec.name
            name = name.replace("obj.", "")
            if name.startswith("_"):
                name = name[1:]
            name = name.split("(")[0]
            rec.display_name = name

    @contextmanager
    def _get_env(self, new_one):
        if not new_one:
            yield self
            return
        
        db_registry = registry(self.env.cr.dbname)
        with api.Environment.manage(), db_registry.cursor() as cr:
            env = api.Environment(cr, self.env.user.id, {})
            self = self.with_env(env).sudo()
            yield self

    def perform(self, now=False):
        self.ensure_one()
        self2 = self.sudo()

        if not now:
            self = self.with_delay()
            
        self._exec(now)

    def _exec(self, now):
        started = arrow.get()
        # try nicht unbedingt notwendig; bei __exit__ wird ein close aufgerufen
        with self._get_env(new_one=not now) as self:
            pg_advisory_lock(self.env.cr, f"performat_task_{self.branch_id.id}")

            try:
                name = self.name or ''
                if name.startswith("_"):
                    name = name[1:]
                logsio = self.branch_id._get_new_logsio_instance(name)
                logsio.start_keepalive()

                dest_folder = self.machine_id._get_volume('source') / self.branch_id.project_name
                with self.machine_id._shellexec(dest_folder, logsio=logsio, project_name=self.branch_id.project_name) as shell:
                    self.branch_id.repo_id._get_main_repo(
                        destination_folder=dest_folder
                        )
                    obj = self.env[self.model].sudo().browse(self.res_id)
                    sha = shell.X(["git", "log", "-n1", "--format=%H"]).output.strip()
                    commit = self.branch_id.commit_ids.filtered(lambda x: x.name == sha)
                    if not commit:
                        raise ValidationError(f"Commit {sha} not found in branch.")
                    self.commit_id = commit
                    # get current commit
                    args = {
                        'task': self,
                        'logsio': logsio,
                        'shell': shell,
                        }
                    cmd = 'obj.' + self.name + "(**args)", {'obj': obj, 'args': args}
                    
                    exec()

                self.log = '\n'.join(logsio.lines)

            except Exception as ex:
                msg = traceback.format_exc()
                self.state = 'failed'
                self.error = msg
                logsio.error(msg)

            else:
                self.state = 'done'
            logsio.stop_keepalive()

            duration = (arrow.get() - started).total_seconds()
            self.duration = duration
            logsio.info(f"Finished after {duration} seconds!")

    def _cron_run(self):
        for task in self.search([
            ('state', '=', 'new')
        ]):
            task.perform()

    def _make_cron(self, uuid, object, method, active):
        object.ensure_one()
        key = f"{uuid}_{object._name}_{object.id}"
        crons = self.env['ir.cron'].with_context(active_test=False).search([('name', '=', key)], limit=1)
        if not crons:
            crons = crons.create({
                'name': key,
                'model_id': self.env['ir.model'].search([('model', '=', object._name)]).id,
                'interval_number': 1,
                'numbercall': -1,
                'interval_type': 'minutes',
                'code': f"env['{object._name}'].browse({object.id}).{method}()"
            })
        if crons.active != active:
            crons.active = active