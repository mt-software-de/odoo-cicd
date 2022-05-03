from curses import wrapper
import arrow
from contextlib import contextmanager, closing
import base64
import datetime
from . import pg_advisory_lock
import traceback
import time
from odoo import _, api, fields, models
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import ValidationError
import logging
from pathlib import Path
from contextlib import contextmanager

MAX_ERROR_SIZE = 100 * 1024 * 1024 * 1024

SETTINGS = (
    "RUN_POSTGRES=1\n"
    "DB_HOST=postgres\n"
    "DB_PORT=5432\n"
    "DB_USER=odoo\n"
    "DB_PWD=odoo\n"
    "ODOO_DEMO=1\n"
    "ODOO_QUEUEJOBS_CRON_IN_ONE_CONTAINER=1\n"
    "RUN_ODOO_QUEUEJOBS=0\n"
    "RUN_ODOO_CRONJOBS=0\n"
    "ODOO_LOG_LEVEL=warn\n"
)

logger = logging.getLogger(__name__)


class AbortException(Exception):
    pass


class WrongShaException(Exception):
    pass


class TestFailedAtInitError(Exception):
    pass


class CicdTestRun(models.Model):
    _log_access = False
    _inherit = ['mail.thread', 'cicd.open.window.mixin']
    _name = 'cicd.test.run'
    _order = 'id desc'

    name = fields.Char(compute="_compute_name")
    do_abort = fields.Boolean("Abort when possible", tracking=True)
    create_date = fields.Datetime(
        default=lambda self: fields.Datetime.now(), required=True,
        readonly=True)
    date = fields.Datetime(
        "Date Started", default=lambda self: fields.Datetime.now(),
        required=True, tracking=True)
    commit_id = fields.Many2one("cicd.git.commit", "Commit", required=True)
    commit_id_short = fields.Char(related="commit_id.short", store=True)
    branch_id = fields.Many2one(
        'cicd.git.branch', string="Initiating branch", required=True)
    branch_id_name = fields.Char(related='branch_id.name', store=False)
    branch_ids = fields.Many2many(
        'cicd.git.branch', related="commit_id.branch_ids", string="Branches")
    repo_short = fields.Char(related="branch_ids.repo_id.short")
    state = fields.Selection([
        ('open', 'Ready To Test'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('omitted', 'Omitted'),
        ('failed', 'Failed'),
    ], string="Result", required=True, default='open', tracking=True)
    success_rate = fields.Integer("Success Rate [%]", tracking=True)
    line_ids = fields.One2many('cicd.test.run.line', 'run_id', string="Lines")
    duration = fields.Integer("Duration [s]", tracking=True)
    queuejob_log = fields.Binary("Queuejob Log")
    queuejob_log_filename = fields.Char(compute="_queuejob_log_filename")

    def _queuejob_log_filename(self):
        for rec in self:
            rec.queuejob_log_filename = 'queuejobs.xlsx'

    def abort(self):
        for qj in self._get_queuejobs('all'):
            self.env.cr.execute((
                "update queue_job set state = 'failed' "
                "where id=%s "
            ), (qj['id'],))
        self.do_abort = False
        self.state = 'failed'

    def _wait_for_postgres(self, shell, timeout=300):
        started = arrow.get()
        deadline = started.shift(seconds=timeout)

        while True:
            try:
                shell.odoo(
                    "psql",
                    "--non-interactive",
                    "--sql",
                    "select * from information_schema.tables limit 1;",
                    timeout=timeout)
            except Exception:
                diff = arrow.get() - started
                msg = f"Waiting for postgres {diff.total_seconds()}..."
                logger.info(msg)
                if arrow.get() < deadline:
                    time.sleep(0.5)
                else:
                    raise
            else:
                break

    def _reload(self, shell, settings, root):
        def reload():
            try:
                self.branch_id._reload(
                    shell, None, shell.logsio, project_name=shell.project_name,
                    settings=settings, commit=self.commit_id.name)
            except Exception as ex:
                logger.error(ex)
                self._report("Exception at reload", exception=ex)
                raise
            else:
                self._report("Reloaded")
        self._report("Reloading for test run")
        try:
            try:
                reload()
            except RetryableJobError:
                self._report("Retryable error occurred at reloading")
                raise
            except Exception as ex:
                self._report("Reloading: Exception stage 1 hit")
                self._report(str(ex))
                try:
                    if shell.cwd != root:
                        shell.rm(shell.cwd)
                    reload()
                except RetryableJobError:
                    self._report(
                        "Retryable error occurred at reloading stage 2")
                    raise
                except Exception as ex:
                    if 'reference is not a tree' in str(ex):
                        raise RetryableJobError((
                            "Missing commit not arrived "
                            "- retrying later.")) from ex
                    self._report("Error occurred", exception=ex)
                    raise

        except RetryableJobError as ex:
            self._report("Retrying", exception=ex)
            raise

        except AbortException as ex:
            pass

        except Exception as ex:
            self._report("Error", exception=ex)
            raise

    def _abort_if_required(self):
        if self.do_abort:
            raise AbortException("User aborted")

    def _prepare_run(self):
        self = self._with_context()
        self._report('prepare run started')
        self._switch_to_running_state()

        started = arrow.utcnow()
        try:
            with self._shell() as shell:
                self._checkout_source_code(shell)
                self._abort_if_required()
                self._report('building')
                shell.odoo('build')
                self._report('killing any existing')
                shell.odoo('kill', allow_error=True)
                shell.odoo('rm', allow_error=True)
                self._report('starting postgres')
                shell.odoo('up', '-d', 'postgres')

                self._abort_if_required()

                self._wait_for_postgres(shell)
                self._report('db reset started')
                shell.odoo('-f', 'db', 'reset')
                shell.odoo('pghba-conf-wide-open', "--no-scram")
                shell.odoo('update', 'base', '--no-dangling-check')

                self._abort_if_required()
                self._report('db reset done')

                self._abort_if_required()

                self._report("Storing snapshot")
                shell.odoo('snap', 'save', shell.project_name, force=True)
                self._wait_for_postgres(shell)
                self._report("Storing snapshot done")
                self._report(
                    'preparation done',
                    duration=arrow.utcnow() - started
                )

            self._abort_if_required()

        except TestFailedAtInitError as ex:
            self.state = 'failed'
            self._report("Failed at preparation", state='failed', exception=ex)
        else:
            self.as_job("_preparedone_run_tests", False)._run_test()

    def _cleanup_testruns(self):
        self = self._with_context()
        with self._logsio(None) as logsio:
            self._report("Cleanup Testing started...")
            with self._shell() as shell:
                if not shell.exists(shell.cwd):
                    return
                shell.odoo('kill', allow_error=True)
                shell.odoo('rm', force=True, allow_error=True)
                shell.odoo('snap', 'clear')
                shell.odoo('down', "-v", force=True, allow_error=True)
                project_dir = shell.cwd
                with shell.clone(cwd=shell.cwd.parent) as shell:
                    try:
                        shell.rm(project_dir)
                    except Exception:
                        msg = f"Failed to remove directory {project_dir}"
                        if logsio:
                            logsio.error(msg)
                        logger.error(msg)
            self._report("Cleanup Testing done.")

    def _report(
        self, msg, state='success',
        exception=None, duration=None, ttype='log'
    ):
        # if not hasattr(report, 'last_report_time'):
        #     report.last_report_time = arrow.get()
        # if duration is None:
        #     duration = (arrow.get() - report.last_report_time)\
        #         .total_seconds()
        # elif isinstance(duration, datetime.timedelta):
        #     duration = duration.total_seconds()
        if duration and isinstance(duration, datetime.timedelta):
            duration = duration.total_seconds()

        ttype = ttype or 'log'
        data = {
            'state': state,
            'name': msg,
            'ttype': ttype,
            'duration': duration
        }
        if exception:
            state = 'failed'
            msg = (msg or '') + '\n' + str(exception)
            data['exc_info'] = str(exception)
        else:
            state = state or 'success'

        self.line_ids = [[0, 0, data]]
        self.env.cr.commit()

        with self._logsio(None) as logsio:
            if state == 'success':
                logsio.info(msg)
            else:
                logsio.error(msg)

    def _checkout_source_code(self, shell):
        self._report("Checking out source code...")
        try:
            self._reload(
                shell, SETTINGS,
                str(Path(shell.cwd).parent)
                )
        except Exception as ex:
            # could be: module not found, error in manifest or so:
            raise TestFailedAtInitError() from ex

        self._report("Checking commit")
        sha = shell.X(["git", "log", "-n1", "--format=%H"])[
            'stdout'].strip()
        if sha != self.commit_id.name:
            raise WrongShaException((
                f"checked-out SHA {sha} "
                f"not matching test sha {self.commit_id.name}"
                ))
        self._report("Commit matches")
        self._report(f"Checked out source code at {shell.cwd}")

    def prepare_run(self):
        self = self._with_context()
        self._switch_to_running_state()

        self._report("Prepare run...")
        self.date = fields.Datetime.now()
        self.as_job('prepare', False)._prepare_run()

    def execute_now(self):
        self.with_context(
            test_queue_job_no_delay=True,
            DEBUG_TESTRUN=True,
            FORCE_TEST_RUN=True).execute()
        return True

    def _get_qj_marker(self, suffix, afterrun):
        runtype = '__after_run__' if afterrun else '__run__'
        return (
            f"testrun-{self.id}-{runtype}"
            f"{suffix}"
        )

    def as_job(self, suffix, afterrun, eta=None):
        marker = self._get_qj_marker(suffix, afterrun=afterrun)
        eta = arrow.utcnow().shift(minutes=eta or 0).strftime(DTF)
        return self.with_delay(
            channel="testruns",
            identity_key=marker,
            eta=eta
            )

    def _get_queuejobs(self, ttype):
        assert ttype in ['active', 'all']
        self.ensure_one()
        if ttype == 'active':
            domain = " state not in ('done') "
        else:
            domain = " 1 = 1 "

        # TODO safe
        marker = self._get_qj_marker("", False)
        domain += f" AND identity_key ilike '%{marker}%'"
        self.env.cr.execute((
            "select id, state, exc_info, identity_key "
            "from queue_job "
            "where " + domain
        ))
        queuejobs = self.env.cr.dictfetchall()

        def retryable(job):
            if job['state'] != 'failed':
                return True
            if 'could not serialize' in (job['exc_info'] or '').lower():
                return True
            return False

        if ttype == 'active':
            queuejobs = [x for x in queuejobs if retryable(x)]

        queuejobs = [
            x for x in queuejobs
            if 'wait_for_finish' not in (x['identity_key'] or '')]
        return queuejobs

    @contextmanager
    def _logsio(self, logsio=None):
        if logsio:
            yield logsio
        else:
            with self.branch_id.with_context(
                testrun="")._get_new_logsio_instance(
                    'test-run-execute') as logsio:
                yield logsio

    def _trigger_wait_for_finish(self):
        self.as_job(
            "wait_for_finish", False, eta=1)._wait_for_finish()

    def _wait_for_finish(self, task=None):
        self.ensure_one()
        if not self.exists():
            return
        if self.env.context.get('test_queue_job_no_delay'):
            return

        qj = self._get_queuejobs('active')
        if qj:
            raise RetryableJobError(
                "Waiting for test finish", seconds=30,
                ignore_retry=True)

        with self._logsio(None) as logsio:
            logsio.info(f"Duration was {self.duration}")

            qj = sorted(qj, key=lambda x: x['date_created'])
            if qj:
                self.duration = \
                        (arrow.utcnow() - arrow.get(qj[0]['date_created']))\
                    .total_seconds()
            else:
                self.duration = 0

        self.as_job("cleanup", True)._cleanup_testruns()

        self.as_job("compute_success_rate", True)._compute_success_rate(
            task=task)
        self.as_job('inform_developer', True)._inform_developer()

    @contextmanager
    def _shell(self, logsio=None):
        with self._logsio(logsio) as logsio:
            self = self._with_context()
            machine = self.branch_ids.machine_id
            root = machine._get_volume('source')
            project_name = self.branch_id.project_name
            with machine._shell(
                cwd=root / project_name, project_name=project_name,
                logsio=logsio,
            ) as shell:
                yield shell

    # ----------------------------------------------
    # Entrypoint
    # ----------------------------------------------
    # env['cicd.test.run'].with_context(DEBUG_TESTRUN=True, FORCE_TEST_RUN=True).browse(nr).execute()
    def execute(self, task=None):
        self.ensure_one()

        self._switch_to_running_state()
        self.do_abort = False
        self.as_job('starting_games', False)._let_the_games_begin()

    def _switch_to_running_state(self):
        """
        Should be called by methods that to work on test runs.
        If a queuejob is revived then the state of the test run should
        represent this.
        """
        if self.state != 'running':
            self.state = 'running'

        self._trigger_wait_for_finish()

    def _with_context(self):
        testrun_context = f"_testrun_{self.id}"
        self = self.with_context(
            testrun=testrun_context,
            prefetch_fields=False
            )

        # lock test run
        self.env.cr.execute((
            "select id "
            "from cicd_test_run "
            "where id = %s "
            "for update nowait "
        ), (self.id,))

        return self

    def _let_the_games_begin(self):
        self = self._with_context()
        self._switch_to_running_state()

        with self._logsio(None) as logsio:
            b = self.branch_id

            if not b.any_testing:
                logsio.info("No testing - so done")
                self.success_rate = 100
                self.state = 'success'
                return

            self.line_ids = [[6, 0, []]]
            self._report("Started")
            self.do_abort = False
            self.state = 'running'

            self.as_job('prepare-run', False).prepare_run()

    def _run_test(self):
        self.ensure_one()
        self = self._with_context()
        self._report("Starting Tests")
        self._switch_to_running_state()

        b = self.branch_id

        with self._shell() as shell:
            logsio = shell.logsio

            if b.run_unittests:
                self._execute(
                    shell, logsio, self._run_unit_tests,
                    'test-units')
            if b.run_robottests:
                self._execute(
                    shell, logsio, self._run_robot_tests,
                    'test-robot')
            if b.simulate_install_id:
                self._execute(
                    shell, logsio, self._run_update_db,
                    'test-migration')

    def _execute(self, shell, logsio, run, appendix):
        try:
            logsio.info("Running " + appendix)
            run(shell, logsio)
        except Exception as ex:
            logger.error(ex, exc_info=True)
            msg = traceback.format_exc()
            self._report(msg, exception=ex)

    def _compute_success_rate(self, task=None):
        self.ensure_one()
        lines = self.mapped('line_ids').filtered(
            lambda x: x.ttype != 'log')
        success_lines = len(lines.filtered(
            lambda x: x.state == 'success' or x.force_success or x.reused))
        if not lines:
            # perhaps in debugging and quickly testing releasing
            # or turning off tests
            self.state = 'success'
            self.success_rate = 100

        else:
            if all(
                x.state == 'success' or
                x.force_success or x.reused for x in lines
            ):
                self.state = 'success'
            else:
                self.state = 'failed'

            if not success_lines:
                self.success_rate = 0
            else:
                self.success_rate = int(
                    100 / float(len(lines)) * float(success_lines))
        self.branch_id._compute_state()
        if task:
            if self.state == 'failed':
                task.state = 'failed'
            elif self.state == 'success':
                task.state = 'success'

    def _compute_name(self):
        for rec in self:
            date = rec.create_date.strftime("%Y-%m-%d %H:%M:%S")[:10]
            rec.name = f"{date} - {rec.branch_id.name}"

    @api.model
    def _get_ttypes(self, filtered):
        for x in self._fields['ttype'].selection:
            if filtered:
                if x[0] not in filtered:
                    continue
            yield x[0]

    def rerun(self):
        if self.branch_id.state not in ['testable', 'tested', 'dev']:
            raise ValidationError(
                _("State of branch does not allow a repeated test run"))
        self = self.sudo()
        self.line_ids.unlink()
        self.state = 'open'
        self.success_rate = 0

    # def _run_create_empty_db(self, shell, task, logsio):

    #     def _emptydb(item):
    #         self.branch_id._create_empty_db(shell, task, logsio)

    #     self._generic_run(
    #         shell, logsio, [None],
    #         'emptydb', _emptydb,
    #     )

    def _run_update_db(self, shell, logsio, **kwargs):

        def _update(item):
            logsio.info(f"Restoring {self.branch_id.dump_id.name}")

            shell.odoo('-f', 'restore', 'odoo-db', self.branch_id.dump_id.name)
            self._wait_for_postgres(shell)
            shell.odoo('update', timeout=self.timeout_migration)
            self._wait_for_postgres(shell)

        self._generic_run(
            shell, logsio, [None],
            'migration', _update,
        )

    def _get_generic_run_name(
        self, item, name_callback
    ):
        name = item or ''
        if name_callback:
            try:
                name = name_callback(item)
            except:
                pass
        return name

    def _generic_run(
        self, shell, logsio, todo, ttype, execute_run,
        try_count=1, name_callback=None, name_prefix='',
        unique_name=False, hash=False,
    ):
        """
        Timeout in seconds.

        """
        success = True
        len_todo = len(todo)
        for i, item in enumerate(todo):
            trycounter = 0

            name = self._get_generic_run_name(item, name_callback)
            if hash and self.env['cicd.test.run.line']._check_if_test_already_succeeded(
                self, name, hash,
            ):
                continue

            position = name_prefix or ''
            if len_todo > 0:
                position += f"({i + 1} / {len_todo})"

            while trycounter < try_count:
                if self.do_abort:
                    raise AbortException("Aborted by user")
                trycounter += 1
                logsio.info(f"Try #{trycounter}")

                started = arrow.get()
                data = {
                    'position': position,
                    'name': name,
                    'ttype': ttype,
                    'run_id': self.id,
                    'started': started.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    'try_count': trycounter,
                    'hash': hash,
                }
                try:
                    logsio.info(f"Running {name}")
                    result = execute_run(item)
                    if result:
                        data.update(result)

                except Exception:
                    msg = traceback.format_exc()
                    logsio.error(f"Error happened: {msg}")
                    data['state'] = 'failed'
                    data['exc_info'] = msg
                    success = False
                else:
                    # e.g. robottests return state from run
                    if 'state' not in data:
                        data['state'] = 'success'
                end = arrow.get()
                data['duration'] = (end - started).total_seconds()
                if data['state'] == 'success':
                    break
            self.line_ids = [[0, 0, data]]
            self.env.cr.commit()
        return success

    def _inform_developer(self):
        for rec in self:
            partners = (
                rec.commit_id.author_user_id.mapped('partner_id')
                | rec.commit_id.branch_ids.mapped('assignee_id.partner_id')
                | rec.mapped('message_follower_ids.partner_id')
                | rec.branch_id.mapped('message_follower_ids.partner_id')
            )

            rec.message_post_with_view(
                "cicd.mail_testrun_result",
                subtype_id=self.env.ref('mail.mt_note').id,
                partner_ids=partners.ids,
                values={
                    "obj": rec,
                },
            )
