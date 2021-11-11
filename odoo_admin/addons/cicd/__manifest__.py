{   'application': False,
    'auto_install': True,
    'css': [],
    'data': [   'data/cronjobs.xml',
                'data/data.xml',
                'views/dump_form.xml',
                'views/git_branch_form.xml',
                'views/git_branch_kanban.xml',
                'views/git_branch_tree.xml',
                'views/machine_form.xml',
                'views/repository.xml',
                'views/task_form.xml',
                'views/volume_form.xml',
                'views/menu.xml',
                'security/ir.model.access.csv'],
    'demo': [],
    'depends': ['base'],
    'external_dependencies': {   'python': [   'pudb',
                                               'spur',
                                               'spurplus',
                                               'bson',
                                               'humanize',
                                               'paramiko']},
    'name': 'cicd',
    'qweb': [],
    'test': [],
    'version': '14.0.1.0'}