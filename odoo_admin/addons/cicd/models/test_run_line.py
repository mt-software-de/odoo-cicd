import re
from functools import partial
import arrow
from contextlib import contextmanager
from . import pg_advisory_lock
import traceback
from odoo import _, api, fields, models
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import ValidationError
from pathlib import Path
from contextlib import contextmanager
from .test_run import AbortException


class CicdTestRunLine(models.AbstractModel):
    _inherit = 'cicd.open.window.mixin'
    _name = 'cicd.test.run.line'
    _order = 'started desc'

    run_id = fields.Many2one('cicd.test.run', string="Run")
    exc_info = fields.Text("Exception Info")
    queuejob_id = fields.Many2one("queue.job", string="Queuejob")
    machine_id = fields.Many2one(
        'cicd.machine', string="Machine", required=True)
    duration = fields.Integer("Duration")
    state = fields.Selection([
        ('open', 'Open'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], default='open', required=True)
    force_success = fields.Boolean("Force Success")
    try_count = fields.Integer("Try Count")
    name = fields.Char("Name")
    name_short = fields.Char(compute="_compute_name_short")
    test_setting_id = fields.Reference([
        ("cicd.test.settings.unittest", "Unit Test"),
        ("cicd.test.settings.robottest", "Robot Test"),
        ("cicd.test.settings.migration", "Migration Test"),
    ], string="Initiating Testsetting")
    hash = fields.Char("Hash", help="For using")
    reused = fields.Boolean("Reused", readonly=True)
    started = fields.Datetime(
        "Started", default=lambda self: fields.Datetime.now())
    project_name = fields.Char("Project Name Used (for cleaning)")
    effective_machine = fields.Many2one(
        "cicd.machine", compute="_compute_machine")
    logfile_path = fields.Char("Logfilepath", compute="_compute_logfilepath")
    log = fields.Text("Log")

    def _reset_logfile(self):
        Path(self.logfile_path).write_text(
            "")

    def _compute_logfilepath(self):
        for rec in self:
            rec.logfile_path = \
                "/opt/out_dir/testrunline_logs/testrunline_log_{rec.id}"
            Path(rec.logfile_path).parent.mkdir(exists_ok=True, parents=True)

    def _compute_machine(self):
        for rec in self:
            rec.effective_machine_id = \
                rec.machine_id or rec.run_id.branch_id.repo_id.machine_id

    @contextmanager
    def _shell(self, quick=False):
        assert self.env.context.get('testrun')
        with self.effective_machine_id._shell(
            cwd=self._get_source_path(),
            project_name=self.branch_id.project_name,
        ) as shell:
            if not quick:
                self._ensure_source_and_machines(shell)
            yield shell

    def open_queuejob(self):
        return {
            'view_type': 'form',
            'res_model': self.queuejob_id._name,
            'res_id': self.queuejob_id.id,
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'target': 'current',
        }

    def _compute_name_short(self):
        for rec in self:
            MAX = 80
            if len(rec.name or '') > MAX:
                rec.name_short = f"{rec.name[:MAX]}..."
            else:
                rec.name_short = rec.name

    def toggle_force_success(self):
        self.sudo().force_success = not self.sudo().force_success

    @api.recordchange('force_success')
    def _onchange_force(self):
        for rec in self:
            if rec.run_id.state not in ['running']:
                rec.run_id._compute_success_rate()

    def check_if_test_already_succeeded(self):
        """
        Compares the hash of the module with an existing
        previous run with same hash.
        """
        import pudb;pudb.set_trace()
        res = self.search([
            ('run_id.branch_ids.repo_id',
                '=', self.run_id.branch_ids.repo_id.id),
            ('name', '=', self.name),
            ('hash', '=', hash),
            ('state', '=', 'success'),
        ], limit=1, order='id desc')
        return bool(res)

    @api.constrains("ttype")
    def _be_graceful(self):
        for rec in self:
            if rec.ttype != 'error':
                continue

            exc_info = rec.exc_info or ''
            name = rec.name or ''

            grace = [
                '.git/index.lock',
                'the database system is starting up',

            ]
            for vol in self.env['cicd.machine.volume'].search([]):
                grace += [f"{vol.name}.*No such file or directory"]
                grace += [f"No such file or directory.*{vol.name}"]
            for grace in grace:
                for line in (exc_info + name).splitlines():
                    if re.findall(grace, line):
                        rec.ttype = 'log'
                        break

    @api.model
    def create(self, vals):
        res = super().create(vals)
        res._be_graceful()
        return res

    def ok(self):
        return {'type': 'ir.actions.act_window_close'}

    def execute(self):
        breakpoint()
        self.run_id._switch_to_running_state()
        if not self.run_id.no_reuse and self.check_if_test_already_succeeded(self):
            self.state = 'success'
            self.reused = True
            return

        logfile = Path(self.logfile_path)

        try:

            trycounter = 0
            while trycounter < self.try_count:
                self.log = False
                if self.run_id.do_abort:
                    raise AbortException("Aborted by user")
                trycounter += 1

                with self.run_id._logsio() as logsio:
                    logsio.info(f"Try #{trycounter}")

                    self.started = arrow.get()
                    self.env.cr.commit()
                    try:
                        logsio.info(f"Running {self.name}")
                        self._execute()

                    except Exception:  # pylint: disable=broad-except
                        msg = traceback.format_exc()
                        logsio.error(f"Error happened: {msg}")
                        self.state = 'failed'
                        self.exc_info = msg
                    else:
                        # e.g. robottests return state from run
                        self.state = 'success'
                        self.exc_info = False
                end = arrow.get()
                self.duration = (end - self.started).total_seconds()
                if self.state == 'success':
                    break

                self.log = False
                if logfile.exists():
                    self.log = logfile.read_text()
                    logfile.unlink()

        except Exception as ex:
            if logfile.exists():
                logfile.unlink()

        self.env.cr.commit()

    def _report(self, msg, exception=None):
        if exception:
            if isinstance(exception, RetryableJobError):
                return

        if exception:
            msg = (msg or '') + '\n' + str(exception)

        if not msg:
            return

        with open(self.logfile_path, 'a') as file:
            file.write(msg)
            file.write("\n")

        with self._logsio(None) as logsio:
            logsio.info(msg)

    def _log(self, func, comment="", allow_error=False):
        breakpoint()
        started = arrow.utcnow()
        if comment:
            self._report(comment)
        params = {}
        do_log = True
        try:
            func(self)
        except Exception as ex:  # pylint: disable=broad-except
            do_log = True
            if allow_error:
                params['name'] = (
                    f"{params['name'] or ''}"
                    " "
                    f"{ex}"
                )
            else:
                params['exception'] = ex

        finally:
            params['duration'] = (arrow.utcnow() - started).total_seconds()
        if do_log:
            self._report(comment, **params)
        self._abort_if_required()

    def _lo(self, shell, *params, comment=None, **kwparams):
        breakpoint()
        comment = comment or ' '.join(map(str, params))
        self._log(
            lambda self: shell.odoo(*params, **kwparams),
            comment=comment,
            allow_error=kwparams.get('allow_error'),
        )

    def _ensure_source_and_machines(
        self, shell, start_postgres=False, settings=""
    ):
        self._log(
            lambda self: self._checkout_source_code(shell.machine),
            'checkout source'
        )
        lo = partial(self._lo, shell)
        # lo = lambda *args, **kwargs: self._lo(shell, *args, **kwargs)
        self._reload(
            shell, settings, shell.cwd)
        lo('regpull', allow_error=True)
        lo('build')
        lo('kill', allow_error=True)
        lo('rm', allow_error=True)
        if start_postgres:
            lo('up', '-d', 'postgres')
            shell.wait_for_postgres()

    def _get_source_path(self):
        path = Path(self.machine_id._get_volume('source'))
        # one source directory for all tests; to have common .dirhashes
        # and save disk space
        project_name = self.branch_id.with_context(testrun="").project_name
        path = path / f"{project_name}_testrun_{self.id}"
        return path

    def _checkout_source_code(self, machine):
        assert machine._name == 'cicd.machine'

        with pg_advisory_lock(self.env.cr, f'testrun_{self.id}'):
            path = self._get_source_path()

            with machine._shell(cwd=path.parent) as shell:
                if not shell.exists(path / '.git'):
                    shell.remove(path)
                    self._report("Checking out source code...")
                    self.branch_id.repo_id._get_main_repo(
                        destination_folder=path,
                        logsio=shell.logsio,
                        machine=machine,
                        limit_branch=self.branch_id.name,
                        depth=1,
                    )
            with shell.clone(cwd=path) as shell:
                shell.X(["git-cicd", "checkout", "-f", self.commit_id.name])

                self._report("Checking commit")
                sha = shell.X(["git-cicd", "log", "-n1", "--format=%H"])[
                    'stdout'].strip()
                if sha != self.commit_id.name:
                    raise WrongShaException((
                        f"checked-out SHA {sha} "
                        f"not matching test sha {self.commit_id.name}"
                        ))
                self._report("Commit matches")
                self._report(f"Checked out source code at {shell.cwd}")
