# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from __future__ import absolute_import, print_function, unicode_literals

import os.path
from multiprocessing import cpu_count

from charmhelpers.core import (
    hookenv,
    host,
    templating,
    )

from charms.turnip.base import (
    code_dir,
    data_dir,
    data_mount_unit,
    get_rabbitmq_url,
    logs_dir,
    reload_systemd,
    venv_dir,
    )


def configure_wsgi():
    config = hookenv.config()
    context = dict(config)
    context.update({
        'code_dir': code_dir(),
        'config_file': os.path.join(code_dir(), 'gunicorn-turnip-api.py'),
        'data_dir': data_dir(),
        'data_mount_unit': data_mount_unit(),
        'logs_dir': logs_dir(),
        'venv_dir': venv_dir(),
        'celery_broker': get_rabbitmq_url(),
        })
    if context['wsgi_workers'] == 0:
        context['wsgi_workers'] = cpu_count() * 2 + 1
    templating.render(
        'gunicorn-turnip-api.py.j2', context['config_file'], context,
        perms=0o644)
    templating.render(
        'turnip-api.service.j2', '/lib/systemd/system/turnip-api.service',
        context, perms=0o644)
    reload_systemd()
    if host.service_running('turnip-api'):
        host.service_stop('turnip-api')
    if not host.service_resume('turnip-api'):
        raise RuntimeError('Failed to start turnip-api')
