#! /usr/bin/env python

# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os

from setuptools import (
    find_packages,
    setup,
)

here = os.path.abspath(os.path.dirname(__file__))

with open(os.path.join(here, 'README')) as f:
    README = f.read()
with open(os.path.join(here, 'NEWS')) as f:
    README += "\n\n" + f.read()

requires = [
    'contextlib2',
    'cornice',
    'lazr.sshserver>=0.1.7',
    'Paste',
    'pygit2>=0.27.2,<0.28.0',
    'python-openid',
    'PyYAML',
    'Twisted[conch]',
    'waitress',
    'zope.interface',
    ]
test_requires = [
    'fixtures',
    'flake8',
    'testtools',
    'webtest',
    ]
deploy_requires = [
    'envdir',
    'gunicorn',
    ]

setup(
    name='turnip',
    version='0.1.1',
    packages=[
        'turnip.%s' % package for package in
        find_packages('turnip', exclude=['*.tests', 'tests'])],
    include_package_data=True,
    zip_safe=False,
    maintainer='LAZR Developers',
    maintainer_email='lazr-developers@lists.launchpad.net',
    description='turnip',
    long_description=README,
    license='AGPL v3',
    url='https://launchpad.net/turnip',
    download_url='https://launchpad.net/turnip/+download',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        ],
    install_requires=requires,
    tests_require=test_requires,
    extras_require=dict(
        test=test_requires,
        deploy=deploy_requires),
    test_suite='turnip',
    entry_points="""\
    [paste.app_factory]
    main = turnip.api:main
    """,
    paster_plugins=['pyramid'],
    )
