from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
class Release(models.Model):
    _name = 'cicd.release'

    name = fields.Char("Name")
    machine_ids = fields.Many2many('cicd.machine', string="Machines")
    repo_id = fields.Many2one(related="branch_id.repo_id", string="Repo", store=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    candidate_branch = fields.Many2one('cicd.git.branch', string="Candidate", default="pre_master", required=True)
    item_ids = fields.One2many('cicd.release.item', 'release_id', string="Release")
    auto_release = fields.Boolean("Auto Release")
    auto_release_cronjob_id = fields.Many2one('ir.cron', string="Scheduled Release")
    sequence_id = fields.Many2one('ir.sequence', string="Version Sequence", required=True)

    @api.recordchange('auto_release')
    def _onchange_autorelease(self):
        for rec in self:
            if not rec.auto_release and rec.auto_release_cronjob_id:
                rec.auto_releae_cronjob_id.sudo().unlink()
            elif rec.auto_release and not rec.auto_release_cronjob_id:
                rec._make_cronjob()

    def _make_cronjob(self):
        self.auto_release_cronjob_id = self.env['ir.cron'].create({
            'name': self.name + " scheduled release"
        })


class ReleaseItem(models.Model):
    _name = 'cicd.release.item'
    _order = 'id desc'

    name = fields.Char("Version")
    release_id = fields.Many2one('cicd.release', string="Release")
    planned_date = fields.Datetime("Planned Deploy Date", default=lambda self: fields.Datetime.now())
    done_date = fields.Datetime("Done")
    final_curtain = fields.Datetime("Final Curtains")
    target_branch = fields.Many2one('cicd.git.branch', "Target Branch", default="master", required=True)

    diff_commit_ids = fields.Many2many('cicd.git.commit', string="New Commits", compute="_compute_diff_commits")
    state = fields.Selection([
        ("new", "New"),
        ("ready", "Ready"),
    ], string="State")
    computed_summary = fields.Text("Computed Summary", compute="_compute_summary")
    commit_ids = fields.Many2many('cicd.git.commit', string="Commits")
    branch_ids = fields.Many2one('cicd.git.branch', string="Consolidated Branches")

    release_type = fields.Selection([
        ('standard', 'Standard'),
        ('hotfix', 'Hotfix'),
    ], default="standard", required=True)

    @api.model
    def create(self, vals):
        release = self.env['cicd.release'].browse(vals['release_id'])
        vals['name'] = release.sequence_id.next_by_id()
        res = super().create(vals)
        return res

    def _compute_summary(self):
        for rec in self:
            summary = []
            for branch in rec.branch_ids.sorted(lambda x: x.date):
                summary.append(f"* {branch.enduser_summary}")
            rec.computed_summary = '\n'.join(summary)

    def _compute_diff_commits(self):
        for rec in self:
            previous_release = self.release_id.item_ids.filtered(lambda x: x.id < rec.id).sorted(lambda x: x.id, reverse=True)
            if not previous_release:
                rec.diff_commit_ids = [[6, 0, []]]
            else:
                rec.diff_commit_ids = [[6, 0, (rec.commit_ids - previous_release[0].commit_ids).ids]]
