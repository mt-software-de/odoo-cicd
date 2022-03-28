{   'application': False,
    'assets': {'web.assets_backend': ['/cicd/static/styles.less']},
    'auto_install': True,
    'data': [   'security/groups.xml',
                'data/cronjobs.xml',
                'data/cronjobs_clear.xml',
                'data/cronjobs_release.xml',
                'data/cronjobs_tasks.xml',
                'data/data.xml',
                'data/mail_templates.xml',
                'data/queuejob_functions.xml',
                'views/backup_dump_form.xml',
                'views/commit_form.xml',
                'views/compressor_tree.xml',
                'views/dump_form.xml',
                'views/dump_tree.xml',
                'views/git_branch_form.xml',
                'views/git_branch_kanban.xml',
                'views/git_branch_search.xml',
                'views/git_branch_tree.xml',
                'views/machine_form.xml',
                'views/new_branch_form.xml',
                'views/postgres_form.xml',
                'views/postgres_tree.xml',
                'views/queue_job_tree.xml',
                'views/queuejob_form.xml',
                'views/queuejob_search.xml',
                'views/registry.xml',
                'views/release_action_form.xml',
                'views/release_form.xml',
                'views/release_item_form.xml',
                'views/release_search.xml',
                'views/release_tree.xml',
                'views/repository.xml',
                'views/task_form.xml',
                'views/task_search.xml',
                'views/task_tree.xml',
                'views/test_run_form.xml',
                'views/test_run_kanban.xml',
                'views/test_run_line_form.xml',
                'views/test_run_line_tree.xml',
                'views/test_run_search.xml',
                'views/test_run_tree.xml',
                'views/ticket_system_form.xml',
                'views/ticket_system_tree.xml',
                'views/user_form.xml',
                'views/volume_form.xml',
                'views/wiz_new_branch.xml',
                'views/menu.xml',
                'security/ir.model.access.csv'],
    'demo': [],
    'depends': ['mail', 'queue_job', 'visualize_queries', 'field_onchange'],
    'external_dependencies': {
        'python': [
                'pudb',
                'sarge',
                'bson',
                'humanize',
                'paramiko',
                'arrow',
                'croniter',
        ]
    },
    'name': 'cicd',
    'qweb': [],
    'test': [],
    'version': '15.0.1.2'}
