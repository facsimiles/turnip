# Copyright 2015 Canonical Ltd.  All rights reserved.

import itertools
import os
import shutil

from pygit2 import (
    GitError,
    GIT_OBJ_BLOB,
    GIT_OBJ_COMMIT,
    GIT_OBJ_TREE,
    GIT_OBJ_TAG,
    init_repository,
    Repository,
    )


REF_TYPE_NAME = {
    GIT_OBJ_COMMIT: 'commit',
    GIT_OBJ_TREE: 'tree',
    GIT_OBJ_BLOB: 'blob',
    GIT_OBJ_TAG: 'tag'
    }


def format_ref(ref, git_object):
    return {
        ref: {
            "object": {
                'sha1': git_object.oid.hex,
                'type': REF_TYPE_NAME[git_object.type]
                }
            }
        }


def format_commit(git_object):
    parents = []
    for parent in git_object.parent_ids:
        parents.append(str(parent))
    return {
        'sha1': git_object.oid.hex,
        'message': git_object.message,
        'author': {
            'name': git_object.author.name,
            'email': git_object.author.email,
            'time': git_object.author.time
            },
        'committer': {
            'name': git_object.committer.name,
            'email': git_object.committer.email,
            'time': git_object.committer.time
            },
        'parents': parents,
        'tree': git_object.tree.hex
        }


def init_repo(repo, is_bare=True):
    """Initialise a git repository."""
    if os.path.exists(repo):
        raise Exception("Repository '%s' already exists" % repo)
    repo_path = init_repository(repo, is_bare)
    return repo_path


def open_repo(repo_path):
    """Open an existing git repository."""
    repo = Repository(repo_path)
    return repo


def delete_repo(repo):
    """Permanently delete a git repository from repo store."""
    shutil.rmtree(repo)


def get_refs(repo_path):
    """Return all refs for a git repository."""
    repo = open_repo(repo_path)
    refs = {}
    for ref in repo.listall_references():
        git_object = repo.lookup_reference(ref).peel()
        # Filter non-unicode refs, as refs are treated as unicode
        # given json is unable to represent arbitrary byte strings.
        try:
            ref.decode('utf-8')
        except UnicodeDecodeError:
            pass
        else:
            refs.update(format_ref(ref, git_object))
    return refs


def get_ref(repo_path, ref):
    """Return a specific ref for a git repository."""
    repo = open_repo(repo_path)
    git_object = repo.lookup_reference(ref).peel()
    ref_obj = format_ref(ref, git_object)
    return ref_obj


def get_log(repo_path, start=None, limit=None, stop=None):
    """Return a commit collection from HEAD or optionally a start oid.

    :param start: sha1 or branch to start listing commits from.
    :param limit: limit number of commits to return.
    :param stop: ignore a commit (and its ancestors).
    """
    repo = open_repo(repo_path)
    if not start:
        start = repo.head.target  # walk from HEAD
    walker = repo.walk(start)
    if stop:
        walker.hide(stop)  # filter stop sha1 and its ancestors
    if limit:
        walker = itertools.islice(walker, int(limit))
    commits = []
    for commit in walker:
        commits.append(format_commit(commit))
    return commits


def get_commit(repo_path, commit_oid):
    """Return a single commit object from an oid."""
    repo = open_repo(repo_path)
    git_object = repo.get(commit_oid)
    if git_object.type != GIT_OBJ_COMMIT:
        raise GitError
    commit = format_commit(git_object)
    return commit


def get_commits(repo_path, commit_oids):
    """Return a collection of commit objects from a list of oids."""
    commit_objects = []
    for commit in commit_oids:
        commit_objects.append(get_commit(repo_path, commit))
    return commit_objects
