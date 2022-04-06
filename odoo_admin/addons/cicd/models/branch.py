import traceback
import time
import json
from contextlib import contextmanager
import arrow
import os
import requests
from odoo import registry
from pathlib import Path
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError, UserError
import humanize
from ..tools.logsio_writer import LogsIOWriter
from contextlib import contextmanager
import logging
from .consts import STATES
from odoo.addons.queue_job.exception import RetryableJobError
from itertools import groupby

logger = logging.getLogger(__name__)


class GitBranch(models.Model):
    _inherit = ['mail.thread']
    _name = 'cicd.git.branch'

    force_prepare_dump = fields.Boolean("Force prepare Dump")
    is_release_branch = fields.Boolean(
        compute="_compute_is_release_branch",
        search="_search_release_branch")
    project_name = fields.Char(
        compute="_compute_project_name", store=False,
        search="_search_project_name")
    database_project_name = fields.Char(
        compute="_compute_project_name", store=False)
    approver_ids = fields.Many2many(
        "res.users", "cicd_git_branch_approver_rel",
        "branch_id", "user_id", string="Approver")
    machine_id = fields.Many2one(related='repo_id.machine_id')
    backup_machine_id = fields.Many2one(
        'cicd.machine', string="Machine for backup/restore")
    backup_filename = fields.Char("Backup Filename")
    last_access = fields.Datetime(
        "Last Access", readonly=True,
        compute='_compute_last_access',
        inverse='_set_last_access')
    cycle_down_after_seconds = fields.Integer(
        "Cycle Down After Seconds", default=3600)
    name = fields.Char("Git Branch", required=True)
    technical_branch_name = fields.Char("Technical Instance Name", help="Docker container limit their name.")
    date_registered = fields.Datetime("Date registered")
    date = fields.Datetime("Date")
    repo_id = fields.Many2one(
        'cicd.git.repo', string="Repository", required=True)
    repo_short = fields.Char(related="repo_id.short")
    active = fields.Boolean("Active", default=True, tracking=True)
    commit_ids = fields.Many2many('cicd.git.commit', string="Commits")
    commit_ids_ui = fields.Many2many(
        'cicd.git.commit', string="Commits", compute="_compute_commit_ids")
    current_task = fields.Char(compute="_compute_current_task")
    database_ids = fields.Many2many(
        'cicd.database', string="Databases", compute="_compute_databases")
    database_size = fields.Float("Database Size", compute="_compute_databases")
    database_size_human = fields.Char(
        "Database Size", compute="_compute_databases")
    ticket_system_url = fields.Char(
        compute="_compute_ticket_system_url", compute_sudo=True)
    ticket_system_ref = fields.Char(
        "Ticketsystem Ref",
        help=(
            "If branch name differs from ticketsystem "
            "then add the name in the ticketsystem here."
            ))
    task_ids = fields.One2many('cicd.task', 'branch_id', string="Tasks")
    task_ids_filtered = fields.Many2many('cicd.task', compute="_compute_tasks")
    state = fields.Selection(
        STATES, string="State", default="new",
        tracking=True, compute="_compute_state", store=True,
        group_expand="_expand_states")
    dump_id = fields.Many2one("cicd.dump", string="Dump")
    last_restore_dump_name = fields.Char("Restored Dump Name", readonly=True)
    last_restore_dump_date = fields.Char("Restored Dump Date", readonly=True)
    remove_web_assets_after_restore = fields.Boolean(
        "Remove Webassets", default=True)
    reload_config = fields.Text("Reload Config", tracking=True)
    autobackup = fields.Boolean("Autobackup", tracking=True)
    enduser_summary = fields.Text("Enduser Summary", tracking=True)
    target_release_ids = fields.Many2many(
        "cicd.release",
        "branch_target_release",
        "branch_id", "release_id",
        string="Target Releases",
        tracking=True,
    )
    release_ids = fields.One2many(
        "cicd.release", "branch_id", string="Releases")

    release_branch_ids = fields.Many2one(
        'cicd.release.item.branch', 'branch_id')
    # Update Fehler: psycopg2.errors.UndefinedColumn: column "id" referenced in foreign key constraint does not exist
    # release_item_ids = fields.Many2many(
    #     'cicd.release.item', 'cicd_release_item_branch', 'branch_id',
    #     'item_id', 'Releases')
    computed_release_item_ids = fields.Many2many(
        'cicd.release.item', "Releases", compute="_compute_releases",
        search='_search_release_items')

    any_testing = fields.Boolean(compute="_compute_any_testing")
    run_unittests = fields.Boolean(
        "Run Unittests",default=True, testrun_field=True)
    run_robottests = fields.Boolean(
        "Run Robot-Tests", default=True, testrun_field=True)
    simulate_install_id = fields.Many2one(
        "cicd.dump", string="Simulate Install", testrun_field=True)
    unittest_all = fields.Boolean("All Unittests")
    retry_unit_tests = fields.Integer("Retry Unittests", default=3)
    timeout_tests = fields.Integer("Timeout Tests [s]", default=600)
    timeout_migration = fields.Integer("Timeout Migration [s]", default=1800)

    test_run_ids = fields.One2many(
        'cicd.test.run', string="Test Runs", compute="_compute_test_runs")
    block_release = fields.Boolean("Block Release", tracking=True)
    block_updates_until = fields.Datetime("Block updates until", tracking=True)

    allowed_backup_machine_ids = fields.Many2many(
        'cicd.machine',
        string="Allowed Backup Machines", compute="_compute_allowed_machines")
    latest_commit_id = fields.Many2one('cicd.git.commit')

    approval_state = fields.Selection(
        related="latest_commit_id.approval_state", tracking=True)
    link_to_instance = fields.Char(
        compute="_compute_link", string="Link To Instance")

    containers = fields.Text(compute="compute_containers_text", store=False)

    _sql_constraints = [
        (
            'name_repo_id_unique',
            "unique(name, repo_id)",
            _("Only one unique entry allowed.")),
    ]

    def compute_containers_text(self):
        for rec in self:
            with rec._extra_env() as x_rec:
                containers_json = x_rec.repo_id.machine_id._get_containers()
                project_name = x_rec.project_name
            containers = []
            for k, v in containers_json.items():
                if project_name + '_' in k:
                    containers.append((
                        f"{k}:{v.lower()}"
                    ))

            def sortorder(x):
                x = x or ''
                state = x.split(":")[1]
                states = {
                    'running': 1,
                    'down': 2,
                    'exited': 2,
                }
                return states.get(state) or ''

            if containers:
                rec.containers = '\n'.join(sorted(containers, key=sortorder))
            else:
                rec.containers = ""

    def _get_last_access_file(self):
        self.ensure_one()
        path = Path((
            "/opt/out_dir/last_access/"
            f"last_access_{self.id}"
        ))
        path.parent.mkdir(exist_ok=True)
        return path

    def _compute_last_access(self):
        for rec in self:
            file = rec._get_last_access_file()
            if not file.exists():
                rec.last_access = False
            else:
                date = arrow.get(file.read_text())
                rec.last_access = date.strftime("%Y-%m-%d %H:%M:%S")

    def _set_last_access(self):
        for rec in self:
            rec._get_last_access_file().write_text(
                arrow.get().strftime("%Y-%m-%d %H:%M:%S")
            )

    def _compute_link(self):
        for rec in self:
            url = self.env['ir.config_parameter'].sudo().get_param(
                key="web.base.url", default=False)
            url += '/start/' + rec.project_name
            rec.link_to_instance = url

    def _expand_states(self, states, domain, order):
        # return all possible states, in order
        return [key for key, val in type(self).state.selection]

    def _compute_latest_commit(self, shell):
        for rec in self:
            shell.checkout_branch(rec.name)

            latest_commit = shell.X([
                "git", "log", "-n1", '--pretty=%H'])[
                    'stdout'].strip().split('\n')[0]

            commit = rec.commit_ids.filtered(lambda x: x.name == latest_commit)
            if not commit:
                raise RetryableJobError(
                    f"Could not find {latest_commit}",
                    ignore_retry=True, seconds=120)
            commit.ensure_one()
            if rec.latest_commit_id != commit:
                rec.latest_commit_id = commit

    def _compute_any_testing(self):
        for rec in self:
            _fields = [
                k
                for k, v in rec._fields.items()
                if getattr(v, 'testrun_field', False)
                ]
            rec.any_testing = any(rec[f] for f in _fields)

    @api.model
    def create(self, vals):
        res = super().create(vals)
        if not res.simulate_install_id:
            res.simulate_install_id = \
                res.repo_id.default_simulate_install_id_dump_id

        if 'remove_web_assets_after_restore' not in vals:
            res.remove_web_assets_after_restore = \
                res.repo_id.remove_web_assets_after_restore
        return res

    def _search_release_items(self, operator, value):
        if operator == 'in':
            branch = self.env['cicd.release.item.branch'].search([
                ('item_id', 'in', value)]).branch_id
            return [('id', 'in', branch.ids)]
        else:
            raise NotImplementedError()

    def _compute_releases(self):
        """
        On item branch or master branch restrict releases to show
        """
        for rec in self:
            releases = self.env['cicd.release'].search([
                ('repo_id', '=', rec.repo_id.id)])

            release_items = self.env['cicd.release.item'].search([
                ('branch_ids.branch_id', '=', rec.id)]).with_context(
                    prefetch_fields=False)
            item_branches = release_items.item_branch_id

            if rec in releases.branch_id:
                release_items = releases.filtered(
                    lambda x: x.branch_id == rec
                ).item_ids
            elif rec in item_branches:
                release_items = releases.item_ids.filtered(
                    lambda x: x.item_branch_id == rec)

            rec.computed_release_item_ids = release_items

    def approve(self):
        self.approver_ids = [[0, 0, {
            'user_id': self.env.user.id,
            'commit_id': self.commit_ids[0].id,
            'state': 'ok',
        }]]
        self.state = 'approved'

    def decline(self):
        self.approver_ids = [[0, 0, {
            'user_id': self.env.user.id,
            'commit_id': self.commit_ids[0].id,
            'state': 'not ok',
        }]]
        self.state = 'rework'

    def set_state(self, state, raise_exception=False):
        self.ensure_one()
        self.state = state

    @api.depends(
        # "block_release", # not needed here - done in _onchange_state_event
        "task_ids",
        "task_ids.state",
        "latest_commit_id",
        "latest_commit_id.approval_state",
        "latest_commit_id.test_state",
        "latest_commit_id.force_approved",
        "latest_commit_id.test_run_ids",
        "latest_commit_id.test_run_ids.state",
        "computed_release_item_ids.state",
        "computed_release_item_ids",
        "release_branch_ids.state",
        "release_branch_ids",
        "any_testing",
        "block_release",
    )
    def _compute_state(self):
        for rec in self:
            tasks = rec.task_ids.with_context(prefetch_fields=False)
            task_names = set(tasks.mapped('name'))
            building_tasks = any(
                x in y for x in ['update', 'reset', 'restore']
                for y in task_names)

            if not rec.commit_ids and not building_tasks:
                if rec.state != 'new':
                    rec.state = 'new'
                continue
            if not building_tasks:
                state = 'new'
            else:
                state = 'dev'

            commit = rec.latest_commit_id

            if commit.approval_state == 'check':
                state = 'approve'

            elif commit.approval_state == 'approved' and \
                commit.test_state in [False, 'open', 'running'] and \
                    rec.any_testing and not commit.force_approved:

                state = 'testable'

            elif commit.test_state == 'failed' or \
                    commit.approval_state == 'declined':
                state = 'dev'

            elif rec.block_release:
                state = 'blocked'

            elif (
                    commit.test_state == 'success'
                    or not rec.any_testing
                    or commit.force_approved
                    ) and commit.approval_state == 'approved':

                release_items = rec.computed_release_item_ids

                # Determine suitable state state
                state = 'tested'
                for release in release_items.release_id:
                    last_item = (release.next_to_finish_item_id | \
                        release.last_item_id).filtered(
                            lambda x: rec in x.branch_ids.branch_id).filtered(
                                lambda x: x.state != 'done')
                    last_done_item = release.with_context(
                        prefetch_fields=False).item_ids.filtered(
                            lambda x: x.is_done and
                            rec.latest_commit_id in x.branch_ids.commit_id)

                    merge_conflict = 'conflict' in last_item.branch_ids.filtered(
                        lambda x: x.commit_id == rec.latest_commit_id).mapped(
                        'state')

                    # merge conflicts beats all
                    # candidate wins over test
                    # done looses again candidate
                    if merge_conflict:
                        # always wins
                        state = 'merge_conflict'
                    elif last_done_item:
                        if state in ['tested']:
                            state = 'done'
                    elif last_item:
                        if state in ['tested', 'done']:
                            state = 'candidate'

            if state != rec.state:
                rec.state = state
                rec.with_delay(
                    identity_key=f"report_ticket_system branch:{rec.name}:"
                )._report_new_state_to_ticketsystem()

    @api.depends("name", "ticket_system_ref")
    def _compute_ticket_system_url(self):
        for rec in self:
            url = False
            if rec.repo_id.ticketsystem_id:
                url = rec.repo_id.ticketsystem_id._compute_url(rec)
            rec.ticket_system_url = url

    def _compute_test_runs(self):
        for rec in self:
            rec.test_run_ids = rec.mapped('commit_ids.test_run_ids')

    def _make_task(
        self, execute, now=False, machine=None, silent=False,
        identity_key=None, reuse=False, testrun_id=None,
        ignore_previous_tasks=False,
        **kwargs
    ):
        for rec in self:
            identity_key = identity_key or \
                f"{rec.repo_id.short}-{rec.name}-{execute}"
            tasks = rec.task_ids.with_context(prefetch_fields=False)

            if reuse and tasks and tasks[0].name == execute and \
                    tasks[0].state == 'failed':
                if now:
                    tasks[0].perform(
                        now=now, ignore_previous_tasks=ignore_previous_tasks)
                elif tasks[0].queue_job_id and \
                        tasks[0].queue_job_id.state in ['failed']:
                    tasks[0].queue_job_id.state = 'pending'
                    return

            if not now and tasks.filtered(
                lambda x: x.state in [
                    False, 'pending', 'enqueued', 'started'] and
                    x.identity_key == identity_key):
                if silent:
                    return
                raise ValidationError((
                    "Task already exists. Not triggered again."
                    f"Idkey: {identity_key}"))

            task = rec.env['cicd.task'].sudo().create({
                'model': self._name,
                'res_id': rec.id,
                'name': execute,
                'branch_id': rec.id,
                'machine_id': (machine and machine.id) or rec.machine_id.id,
                'identity_key': identity_key if identity_key else False,
                'kwargs': json.dumps(kwargs),
                'testrun_id': testrun_id or False,
            })
            task.perform(now=now)

    def _cron_execute_task(self):
        self.ensure_one()
        tasks = self.task_ids.with_context(prefetch_fields=False).filtered(
            lambda x: x.state == 'new')
        if not tasks:
            return
        tasks = tasks[-1]
        tasks.perform()

    def _get_instance_folder(self, machine):
        project_name = self._unblocked('project_name')
        if not project_name:
            raise ValidationError("Project name not determined.")
        return machine._get_volume('source') / project_name

    def make_instance_ready_to_login(self):
        machine = self.machine_id
        timeout = machine.test_timeout_web_login

        def test_request():
            response = requests.get((
                "http://"
                f"{self._get_odoo_proxy_container_name()}"
                "/web/login"
                ),
                timeout=timeout)
            return response.status_code == 200

        deadline = arrow.utcnow().shift(seconds=120)
        virgin = True
        while True:
            try:
                test_request()
            except Exception as ex:
                if arrow.utcnow() > deadline:
                    raise ValidationError((
                        "Timeout: could start instance within a "
                        "certain amount of time - please check logs "
                        "if there is bug in the source code of the "
                        "instance or contact your developer"
                        )) from ex

                try:
                    if virgin:
                        self._make_task(
                            "_reload_and_restart", now=True, reuse=True)
                        virgin = False
                    else:
                        self._make_task(
                            "_simple_docker_up", now=True, reuse=True)
                except RetryableJobError:
                    time.sleep(1)
            else:
                break

        if test_request():
            return

        if self.task_ids.filtered(lambda x: not x.is_done and x.state):
            raise ValidationError((
                "Instance did not respond. Undone task exists. "
                "Please retry later!"
                ))

        raise ValidationError((
            "Instance did not respond. It was tried to start the"
            "application but this did not succeed. Please check task logs."
            ))

    def _get_odoo_proxy_container_name(self):
        project_name = self._unblocked('project_name')
        return f"{project_name}_proxy"

    @api.depends_context('testrun')
    @api.depends("repo_id", "repo_id.short", "name")
    def _compute_project_name(self):
        for rec in self:
            project_name = "_".join([
                os.environ['CICD_PROJECT_NAME'],
                rec.repo_id.short,
                rec.technical_branch_name or rec.name,
            ])

            dbname = project_name.lower().replace("-", "_")
            if any(dbname.startswith(x) for x in "0123456789"):
                dbname = 'db' + dbname
            if self.env.context.get('testrun'):
                project_name += self.env.context['testrun']
            # incompatibility to capital letters in
            # btrfs; constraining to lowercase
            project_name = project_name.lower()
            rec.project_name = project_name
            rec.database_project_name = dbname

    @contextmanager
    def _get_new_logsio_instance(self, source):
        self.ensure_one()
        project_name = self._unblocked('project_name')

        with LogsIOWriter.GET(f"{project_name}", source) as logs:
            yield logs

    @api.constrains("backup_filename")
    def _check_backup_filename(self):
        for rec in self:
            if not rec.backup_filename:
                continue
            if '/' in rec.backup_filename:
                raise ValidationError("No slashes in backup filename allowed!")

    @api.constrains('active')
    def _on_active_change(self):
        for rec in self:
            if not rec.active:
                rec._make_task("_destroy_instance")

    def _destroy_instance(self, shell, task, logsio, **kwargs):
        with self._get_new_logsio_instance('set_inactive') as logsio:
            for machine in self.env['cicd.machine'].search([
                    ('ttype', '=', 'dev')]):
                try:
                    path = machine._get_volume('source')
                except Exception:
                    continue
                self.env['base'].flush()
                self.env.cr.commit()

                # delete instance folder
                with machine._shell(cwd=path, logsio=logsio) as shell:
                    project_path = self.project_path
                    if shell.exists(project_path):
                        try:
                            shell.odoo("kill")
                        except Exception as ex:
                            logsio.error(str(ex))

                        try:
                            shell.odoo("rm")
                        except Exception as ex:
                            logsio.error(str(ex))

                    self.with_delay().purge_instance_folder()

                # delete db
                self.with_delay().delete_db()

    def purge_instance_folder(self):
        for rec in self:
            with rec.repo_id.machine_id._shell() as shell:
                folder = rec._get_instance_folder(shell.machine)
                shell.remove(folder)

    def delete_db(self):
        for rec in self:
            machine = rec.repo_id.machine_id
            if machine.postgres_server_id.ttype != 'dev':
                continue
            project_name = self._unblocked('project_name')
            dbs = machine.postgres_server_id.database_ids.filtered(
                lambda x: x.name == project_name)
            dbs.delete_db()

    def toggle_active(self):
        for rec in self:
            rec.active = not rec.active

    def toggle_block_release(self):
        for rec in self:
            rec.block_release = not rec.block_release

    # env['cicd.git.branch']._cron_make_test_runs()
    @api.model
    def _cron_make_test_runs(self):
        for branch in self.search([('state', '=', 'testable')]):
            if not branch.test_run_ids.filtered(
                lambda x: x.state in [False, 'running', 'open'] and
                    x.commit_id == branch.latest_commit_id):
                branch.with_delay(
                    identity_key=f"{branch.latest_commit_id.name}-run-tests"
                    )._run_tests()

        def kf(x):
            return x.branch_id

        open_tests = self.env['cicd.test.run'].search([
            ('state', '=', 'open')], order='id desc')

        for branch, tests in groupby(open_tests.sorted(kf), kf):
            tests = self.env['cicd.test.run'].union(*list(tests))
            if not tests:
                continue
            branch.with_delay(
                identity_key=f"{branch.latest_commit_id.name}-run-tests")\
                ._run_tests(testrun_id=tests[0].id)
            tests[1:].write({'state': 'omitted'})

    def _trigger_rebuild_after_fetch(self):
        """
        After new source is fetched then the instance is rebuilt.
        """
        for rec in self:
            if (not rec.database_size and rec.repo_id.initialize_new_branches) or \
                        rec.force_prepare_dump:
                rec._make_task("_prepare_a_new_instance", silent=True)
                rec.force_prepare_dump = False
            elif rec.database_size:
                rec._make_task("_update_odoo", silent=True)

    def contains_commit(self, commit):
        return commit in self.mapped('commit_ids')

    def _compute_tasks(self):
        for rec in self:
            tasks = rec.task_ids.with_context(prefetch_fields=False)
            # TODO understand prefetch attribute
            # removed from mt again because it is slow - in feature system of
            # rs # tasks = rec.task_ids # added prefetch=False to fields so all
            # rec.task_ids everywhere should be optimized

            def _filter(x):
                if x.state in ['failed']:
                    return True
                return True

            rec.task_ids_filtered = [[6, 0, tasks.filtered(_filter).ids]]

    @api.depends('commit_ids')
    def _compute_commit_ids(self):
        for rec in self:
            rec.commit_ids_ui = rec.commit_ids.sorted(
                lambda x: x.date, reverse=True)

    def _compute_databases(self):
        for rec in self:
            rec.database_ids = self.env['cicd.database'].sudo().search([
                ('name', '=', rec.database_project_name)])
            rec.database_size = sum(rec.database_ids.mapped('size'))
            rec.database_size_human = humanize.naturalsize(rec.database_size)

    @api.depends("task_ids", "task_ids.state")
    def _compute_current_task(self):
        for rec in self:
            rec.current_task = ', '.join(rec.task_ids.filtered(
                lambda x: x.state in [
                    'pending', 'started', 'enqueued']).mapped('name'))

    def _search_project_name(self, operator, value):
        assert operator == '='

        if not value:
            return [('id', '=', 0)]

        ids = self.search([]).filtered(
            lambda x: x.project_name.lower() == value.lower()).ids
        return [('id', 'in', ids)]

    @api.model
    def _cron_check_blocking_done(self):
        dt = arrow.get().now().strftime("%Y-%m-%d %H:%M:%S")
        with self._extra_env() as x_self:
            branch_ids = x_self.search([('block_updates_until', '<', dt)]).ids
        for branch_id in branch_ids:
            with self._extra_env() as x_self:
                branch = x_self.browse().branch_id
                branch.block_updates_until = False
                branch.update_all_modules()
                branch.env.cr.commit()

    def _compute_allowed_machines(self):
        for rec in self:
            rec.allowed_backup_machine_ids = self.env['cicd.machine'].search([
                ('postgres_server_id.ttype', '=', 'dev')])

    def set_to_check(self):
        self.latest_commit_id.approval_state = 'check'
        self.latest_commit_id.code_review_state = 'check'

    def set_approved(self):
        if self.latest_commit_id.approval_state not in ['approved', 'declined']:
            self.latest_commit_id.approval_state = 'approved'
        else:
            self.latest_commit_id.code_review_state = 'approved'

    def set_declined(self):
        if self.latest_commit_id.approval_state not in ['approved', 'declined']:
            self.latest_commit_id.approval_state = 'declined'
        else:
            self.latest_commit_id.code_review_state = 'declined'

    def ticketsystem_set_state(self, state):
        assert state in ['done', 'in progress']
        # override / implement!

    def _report_new_state_to_ticketsystem(self):
        self.ensure_one()
        if not self.ticket_system_url:
            return

    def _report_comment_to_ticketsystem(self, comment):
        self.ensure_one()
        if not self.ticket_system_url:
            return

    def show_queuejobs(self):
        jobs = self.env['queue.job'].search([
            ('identity_key', 'ilike', f":{self.repo_id.short}-{self.name}:")
        ])

        return {
            'name': "Jobs",
            'view_type': 'form',
            'res_model': jobs._name,
            'context': {
                'search_default_group_by_state': 1,
            },
            'domain': [('id', 'in', jobs.ids)],
            'views': [(False, 'tree'), (False, 'form')],
            'type': 'ir.actions.act_window',
            'target': 'current',
        }

    @property
    def project_path(self):
        with self._extra_env() as self:
            res = self.machine_id._get_volume('source') / self.project_name
        return res

    @contextmanager
    def shell(self, logs_title, prepare=True):
        project_name = self._unblocked('project_name')
        with self._get_new_logsio_instance(logs_title) as logsio:
            with self.machine_id._shell(
                cwd=self.project_path,
                logsio=logsio,
                project_name=project_name
            ) as shell:

                try:
                    if prepare and shell.cwd and shell.exists(shell.cwd) and \
                            shell.remove(shell.cwd / "/.git"):
                        shell.checkout_branch(self.name)

                    yield shell

                except Exception as ex:
                    msg = traceback.format_exc()
                    logsio.error(ex)
                    logsio.error(msg)
                    raise

    def _compute_is_release_branch(self):
        for rec in self:
            rec.is_release_branch = self.env[
                'cicd.release.item'].search_count([
                    ('item_branch_name', '=', rec.name),
                    ('release_id.repo_id', '=', rec.repo_id.id)
                ])

    def _search_release_branch(self, operator, value):
        if operator == '=' and not value:
            names = self.env['cicd.release.item'].sudo().search([]).mapped(
                'item_branch_name')
            return [('name', 'not in', names)]

        else:
            raise NotImplementedError(operator)

    def unlink(self):
        for rec in self:
            rec.active = False
        return True

    def export_excel(self):
        wiz = self.env['cicd.export.excel'].create({
            'branch_id': self.id,
        })
        return {
            'view_type': 'form',
            'res_model': wiz._name,
            'res_id': wiz.id,
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'target': 'current',
        }