# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path
import re
import subprocess
import uuid
from unittest import mock

import pygit2
import yaml
from fixtures import EnvironmentVariable, MonkeyPatch, TempDir
from pygit2 import Signature
from testtools import TestCase
from twisted.internet import reactor as default_reactor
from twisted.web import server

from turnip.api import store
from turnip.api.tests.test_helpers import RepoFactory, open_repo
from turnip.config import config
from turnip.pack.tests.fake_servers import FakeVirtInfoService
from turnip.tests.tasks import CeleryWorkerFixture


class InitTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))

    def assertAllLinkCounts(self, link_count, path):
        count = 0
        for dirpath, _, filenames in os.walk(path):
            for filename in filenames:
                count += 1
                self.assertEqual(
                    link_count,
                    os.stat(os.path.join(dirpath, filename)).st_nlink,
                )
        return count

    def assertAdvertisedRefs(self, present, absent, repo_path):
        out, err = subprocess.Popen(
            ["git", "receive-pack", "--advertise-refs", repo_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).communicate()
        self.assertEqual(b"", err)
        for ref, hex in present:
            self.assertIn(
                hex.encode("ascii") + b" " + ref.encode("utf-8"), out
            )
        for ref in absent:
            self.assertNotIn(absent, out)

    def assertPackedRefs(self, refs, repo_path):
        """Assert the exact format of a packed-refs file.

        We're writing this out directly, so make sure it's as we expect.

        :param refs: A mapping from ref names to (oid, peeled_oid) tuples,
            where peeled_oid may be None if the ref points directly to a
            commit object.
        :param repo_path: The path to the .git directory to check.
        """
        expected_packed_refs = [
            b"# pack-refs with: peeled fully-peeled sorted "
        ]
        for ref_name, (oid, peeled_oid) in sorted(refs.items()):
            if not isinstance(ref_name, bytes):
                ref_name = ref_name.encode("utf-8")
            expected_packed_refs.append(
                b"%s %s" % (oid.encode("ascii"), ref_name)
            )
            if peeled_oid is not None:
                expected_packed_refs.append(
                    b"^%s" % (peeled_oid.encode("ascii"),)
                )
        with open(os.path.join(repo_path, "packed-refs"), "rb") as packed_refs:
            self.assertEqual(
                b"\n".join(expected_packed_refs) + b"\n", packed_refs.read()
            )

    def assertAlternates(self, expected_paths, repo_path):
        alt_path = store.alternates_path(repo_path)
        if not os.path.exists(os.path.dirname(alt_path)):
            raise Exception("No repo at %s." % repo_path)
        actual_paths = []
        if os.path.exists(alt_path):
            with open(alt_path) as altf:
                actual_paths = [
                    re.sub("/objects\n$", "", line) for line in altf
                ]
        self.assertEqual(
            {path.rstrip("/") for path in expected_paths},
            set(actual_paths),
        )

    def makeOrig(self):
        self.orig_path = os.path.join(self.repo_store, "orig/")
        self.orig_factory = RepoFactory(
            self.orig_path, num_branches=3, num_commits=2, num_tags=2
        )
        orig = self.orig_factory.build()
        self.orig_refs = {}
        for ref in orig.references.objects:
            obj = orig[ref.target]
            self.orig_refs[ref.name] = (
                obj.hex,
                ref.peel().hex if obj.type != pygit2.GIT_OBJ_COMMIT else None,
            )
        self.master_oid = orig.references["refs/heads/master"].target
        self.orig_objs = os.path.join(self.orig_path, ".git/objects")

    def test_from_scratch(self):
        path = os.path.join(self.repo_store, "repo/")
        store.init_repo(path)
        r = pygit2.Repository(path)
        self.assertEqual([], r.listall_references())

    def test_repo_config(self):
        """Assert repository is initialised with correct config defaults."""
        repo_path = os.path.join(self.repo_store, "repo")
        store.init_repo(repo_path)
        repo_config = pygit2.Repository(repo_path).config
        with open("git.config.yaml") as f:
            yaml_config = yaml.safe_load(f)

        self.assertEqual(
            bool(yaml_config["core.logallrefupdates"]),
            bool(repo_config["core.logallrefupdates"]),
        )
        self.assertEqual(
            str(yaml_config["pack.depth"]), repo_config["pack.depth"]
        )

    def test_is_repository_available(self):
        repo_path = os.path.join(self.repo_store, "repo/")

        # Fail to set status if repository directory doesn't exist.
        self.assertRaises(
            ValueError, store.set_repository_creating, repo_path, False
        )

        store.init_repository(repo_path, True)
        store.set_repository_creating(repo_path, True)
        self.assertFalse(store.is_repository_available(repo_path))

        store.set_repository_creating(repo_path, False)
        self.assertTrue(store.is_repository_available(repo_path))

        # Duplicate call to set_repository_creating(False) should ignore
        # eventually missing ".turnip-creating" file.
        store.set_repository_creating(repo_path, False)
        self.assertTrue(store.is_repository_available(repo_path))

    def test_open_ephemeral_repo(self):
        """Opening a repo where a repo name contains ':' should return
        a new ephemeral repo.
        """
        # Create repos A and B with distinct commits, and C which has no
        # objects of its own but has a clone of B as its
        # turnip-subordinate.
        repos = {}
        for name in ["A", "B"]:
            factory = RepoFactory(os.path.join(self.repo_store, name))
            factory.generate_branches(2, 2)
            repos[name] = factory.repo
        repo_path_c = os.path.join(self.repo_store, "C")
        store.init_repo(
            repo_path_c, clone_from=os.path.join(self.repo_store, "B")
        )
        repos["C"] = pygit2.Repository(repo_path_c)

        # Opening the union of one and three includes the objects from
        # two, as they're in three's turnip-subordinate.
        repo_name = "A:C"

        with store.open_repo(self.repo_store, repo_name) as ephemeral_repo:
            self.assertAlternates(
                [
                    repos["A"].path,
                    repos["C"].path,
                    os.path.join(repos["A"].path, "turnip-subordinate"),
                    os.path.join(repos["C"].path, "turnip-subordinate"),
                ],
                ephemeral_repo.path,
            )
            self.assertIn(repos["A"].head.target, ephemeral_repo)
            self.assertIn(repos["B"].head.target, ephemeral_repo)

    def test_open_ephemeral_repo_already_exists(self):
        """If an ephemeral repo already exists, open_repo fails correctly."""
        repos = {}
        for name in ("A", "B"):
            repos[name] = RepoFactory(os.path.join(self.repo_store, name)).repo
        ephemeral_uuid = uuid.uuid4()
        self.useFixture(MonkeyPatch("uuid.uuid4", lambda: ephemeral_uuid))
        ephemeral_path = os.path.join(
            self.repo_store, "ephemeral-" + ephemeral_uuid.hex
        )
        os.mkdir(ephemeral_path)

        def open_test_repo():
            with store.open_repo(self.repo_store, "A:B"):
                pass

        e = self.assertRaises(pygit2.GitError, open_test_repo)
        self.assertEqual(
            str(e), "Repository '%s' already exists" % ephemeral_path
        )
        self.assertEqual(
            {"A", "B", os.path.basename(ephemeral_path)},
            set(os.listdir(self.repo_store)),
        )

    def test_open_ephemeral_repo_init_exception(self):
        """If init_repo fails, open_repo cleans up but preserves the error."""

        class InitException(Exception):
            pass

        def mock_write_alternates(*args, **kwargs):
            raise InitException()

        self.useFixture(
            MonkeyPatch(
                "turnip.api.store.write_alternates", mock_write_alternates
            )
        )
        repos = {}
        for name in ("A", "B"):
            repos[name] = RepoFactory(os.path.join(self.repo_store, name)).repo

        def open_test_repo():
            with store.open_repo(self.repo_store, "A:B"):
                pass

        self.assertRaises(InitException, open_test_repo)
        self.assertEqual({"A", "B"}, set(os.listdir(self.repo_store)))

    def test_repo_with_alternates(self):
        """Ensure objects path is defined correctly in repo alternates."""
        factory = RepoFactory(os.path.join(self.repo_store, uuid.uuid1().hex))
        repo_path_with_alt = os.path.join(self.repo_store, uuid.uuid1().hex)
        store.init_repo(
            repo_path_with_alt, alternate_repo_paths=[factory.repo.path]
        )
        self.assertAlternates([factory.repo_path], repo_path_with_alt)

    def test_repo_alternates_objects_shared(self):
        """Ensure objects are shared from alternate repo."""
        factory = RepoFactory(os.path.join(self.repo_store, uuid.uuid1().hex))
        commit_oid = factory.add_commit("foo", "foobar.txt")
        repo_path_with_alt = os.path.join(self.repo_store, uuid.uuid4().hex)
        store.init_repo(
            repo_path_with_alt, alternate_repo_paths=[factory.repo.path]
        )
        repo_with_alt = open_repo(repo_path_with_alt)
        self.assertEqual(commit_oid.hex, repo_with_alt.get(commit_oid).hex)

    def test_clone_with_refs(self):
        self.makeOrig()
        self.assertAllLinkCounts(1, self.orig_objs)

        # init_repo with clone_from=orig and clone_refs=True creates a
        # repo with the same set of refs. And the objects are copied
        # too.
        to_path = os.path.join(self.repo_store, "to/")
        self.assertFalse(store.is_repository_available(to_path))
        store.init_repo(to_path, clone_from=self.orig_path, clone_refs=True)
        self.assertTrue(store.is_repository_available(to_path))

        to = pygit2.Repository(to_path)
        self.assertIsNot(None, to[self.master_oid])
        self.assertEqual(
            sorted(self.orig_refs), sorted(to.listall_references())
        )
        self.assertPackedRefs(self.orig_refs, to_path)

        # Advance master and remove branch-2, so that the commit referenced
        # by the original repository's master isn't referenced by any of the
        # cloned repository's refs; otherwise git >= 2.13 deduplicates the
        # ref in the alternate object store which makes it hard to test that
        # it's set up properly.
        RepoFactory(to_path, num_commits=2).build()
        to.references.delete("refs/heads/branch-2")
        to_master_oid = to.references["refs/heads/master"].target

        # Internally, the packs are hardlinked into a subordinate
        # alternate repo, so minimal space is used by the clone.
        self.assertAlternates(["../turnip-subordinate"], to_path)
        to_sub_path = os.path.join(to_path, "turnip-subordinate")
        self.assertTrue(os.path.exists(to_sub_path))
        self.assertAllLinkCounts(2, self.orig_objs)
        self.assertPackedRefs(self.orig_refs, to_sub_path)

        self.assertAdvertisedRefs(
            [
                (".have", self.master_oid.hex),
                ("refs/heads/master", to_master_oid.hex),
            ],
            [],
            to_path,
        )

    def test_clone_without_refs(self):
        self.makeOrig()
        self.assertAllLinkCounts(1, self.orig_objs)

        # init_repo with clone_from=orig and clone_refs=False creates a
        # repo without any refs, but the objects are copied.
        to_path = os.path.join(self.repo_store, "to/")
        self.assertFalse(store.is_repository_available(to_path))
        store.init_repo(to_path, clone_from=self.orig_path, clone_refs=False)
        self.assertTrue(store.is_repository_available(to_path))

        to = pygit2.Repository(to_path)
        self.assertIsNot(None, to[self.master_oid])
        self.assertEqual([], to.listall_references())
        self.assertFalse(os.path.exists(os.path.join(to_path, "packed-refs")))

        # Internally, the packs are hardlinked into a subordinate
        # alternate repo, so minimal space is used by the clone.
        self.assertAlternates(["../turnip-subordinate"], to_path)
        to_sub_path = os.path.join(to_path, "turnip-subordinate")
        self.assertTrue(os.path.exists(to_sub_path))
        self.assertAllLinkCounts(2, self.orig_objs)
        self.assertPackedRefs(self.orig_refs, to_sub_path)

        # No refs exist, but receive-pack advertises the clone_from's
        # refs as extra haves.
        self.assertAdvertisedRefs(
            [(".have", self.master_oid.hex)], ["refs/"], to_path
        )

    def test_clone_of_clone(self):
        self.makeOrig()
        orig = pygit2.Repository(self.orig_path)
        orig_blob = orig.create_blob(b"orig")

        self.assertAllLinkCounts(1, self.orig_objs)
        to_path = os.path.join(self.repo_store, "to/")
        self.assertFalse(store.is_repository_available(to_path))
        store.init_repo(to_path, clone_from=self.orig_path)
        self.assertTrue(store.is_repository_available(to_path))

        self.assertAllLinkCounts(2, self.orig_objs)
        to = pygit2.Repository(to_path)
        to_blob = to.create_blob(b"to")
        to.create_branch(
            "branch-0",
            orig[orig.references["refs/heads/branch-1"].target.hex],
            True,
        )
        to.create_branch(
            "new-branch",
            orig[orig.references["refs/heads/branch-1"].target.hex],
        )
        packed_refs = dict(self.orig_refs)
        packed_refs["refs/heads/branch-0"] = (
            orig.references["refs/heads/branch-1"].target.hex,
            None,
        )
        packed_refs["refs/heads/new-branch"] = (
            orig.references["refs/heads/branch-1"].target.hex,
            None,
        )

        too_path = os.path.join(self.repo_store, "too/")
        store.init_repo(too_path, clone_from=to_path)
        self.assertAllLinkCounts(3, self.orig_objs)
        too_blob = pygit2.Repository(too_path).create_blob(b"too")

        # Each clone has just its subordinate as an alternate, and the
        # subordinate has no alternates of its own.
        for path in (to_path, too_path):
            self.assertAlternates(["../turnip-subordinate"], path)
            self.assertAlternates([], os.path.join(path, "turnip-subordinate"))
            self.assertIn(self.master_oid.hex, pygit2.Repository(path))
            self.assertAdvertisedRefs(
                [(".have", self.master_oid.hex)], [], path
            )

        # Objects from all three repos are in the third.
        too = pygit2.Repository(too_path)
        self.assertIn(orig_blob, too)
        self.assertIn(to_blob, too)
        self.assertIn(too_blob, too)

        # Each clone has refs from its (transitive) parents in its
        # subordinate.
        self.assertPackedRefs(
            self.orig_refs, os.path.join(to_path, "turnip-subordinate")
        )
        self.assertPackedRefs(
            packed_refs, os.path.join(too_path, "turnip-subordinate")
        )

    def test_create_single_ref(self):
        repo_path = os.path.join(self.repo_store, uuid.uuid1().hex)
        factory = RepoFactory(repo_path)
        commit_sha1 = factory.add_commit("foo", "foobar.txt").hex
        tag_name = "refs/tags/1701"
        branch_name = "refs/heads/new-feature"

        for ref in [tag_name, branch_name]:
            refs_to_create = [{"ref": ref, "commit_sha1": commit_sha1}]
            created, _ = store.create_references(
                self.repo_store, repo_path, refs_to_create
            )
            assert created[ref] == commit_sha1
            self.assertAdvertisedRefs([(ref, commit_sha1)], [], repo_path)

    def test_create_multiple_mixed_success_and_errors(self):
        repo_path = os.path.join(self.repo_store, uuid.uuid1().hex)
        factory = RepoFactory(repo_path)

        expected_created = []
        refs_to_create = []
        # Expected successful cases: 5 commits with a tag and a branch ref
        # created against each commit.
        for i in range(5):
            commit = factory.add_commit(f"foo-{i}", f"foobar-{i}.txt").hex

            tag = f"refs/tags/{i}"
            refs_to_create.append({"ref": tag, "commit_sha1": commit})
            expected_created.append((tag, commit))

            branch = f"refs/heads/new-feature-{i}"
            refs_to_create.append({"ref": branch, "commit_sha1": commit})
            expected_created.append((branch, commit))

        expected_errors = []
        # Invalid ref name
        invalid_name = "refs/tags/?*"
        refs_to_create.append(
            {
                "ref": invalid_name,
                "commit_sha1": refs_to_create[0]["commit_sha1"],
            }
        )
        expected_errors.append(
            (invalid_name, refs_to_create[0]["commit_sha1"])
        )

        # Commit does not exist
        nonexistent_commit = factory.nonexistent_oid()
        nonexistent_commit_ref = "refs/tags/nonexistent-commit"
        refs_to_create.append(
            {
                "ref": nonexistent_commit_ref,
                "commit_sha1": nonexistent_commit,
            }
        )
        expected_errors.append((nonexistent_commit_ref, nonexistent_commit))

        # Ref already exists and force is not passed
        existing_ref = refs_to_create[0]["ref"]
        refs_to_create.append(
            {
                "ref": existing_ref,
                "commit_sha1": refs_to_create[0]["commit_sha1"],
            }
        )
        expected_errors.append(
            (existing_ref, refs_to_create[0]["commit_sha1"])
        )

        created, errors = store.create_references(
            self.repo_store, repo_path, refs_to_create
        )

        for entry in expected_created:
            # Assert {ref:commit} key value pair for successful cases
            self.assertEqual(entry[1], created[entry[0]])

        self.assertEqual(
            f"Invalid ref name '{invalid_name}'", errors[invalid_name]
        )
        self.assertEqual(
            f"Commit '{nonexistent_commit}' not found",
            errors[nonexistent_commit_ref],
        )
        self.assertEqual(
            (
                f"Ref '{existing_ref}' already exists; "
                "retry with force to overwrite"
            ),
            errors[existing_ref],
        )

        # Verify successful cases are created and errorneous ones are not
        # on the git level
        self.assertAdvertisedRefs(expected_created, expected_errors, repo_path)

    def test_force_overwrite_ref(self):
        repo_path = os.path.join(self.repo_store, uuid.uuid1().hex)
        factory = RepoFactory(repo_path)
        first_commit_sha1 = factory.add_commit("foo", "foobar.txt").hex
        tag_name = "refs/tags/1701"
        branch_name = "refs/heads/new-feature"

        for ref in [tag_name, branch_name]:
            refs_to_create = [
                {"ref": ref, "commit_sha1": first_commit_sha1, "force": False}
            ]
            store.create_references(self.repo_store, repo_path, refs_to_create)
            second_commit_oid = factory.add_commit("bar", "barbaz.txt")
            second_commit_sha1 = second_commit_oid.hex
            refs_to_create = [
                {"ref": ref, "commit_sha1": second_commit_sha1, "force": True}
            ]
            created, _ = store.create_references(
                self.repo_store, repo_path, refs_to_create
            )

            assert created[ref] == second_commit_sha1
            self.assertAdvertisedRefs(
                [(ref, second_commit_sha1)],  # in refs
                [(ref, first_commit_sha1)],  # not in refs
                repo_path,
            )

    def test_fetch_refs(self):
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.makeOrig()
        # Creates a new branch in the orig repository.
        orig_path = self.orig_path
        orig = self.orig_factory.repo
        master_tip = orig.references[b"refs/heads/master"].target.hex

        orig_branch_name = "new-branch"
        orig_ref_name = "refs/heads/new-branch"
        orig.create_branch(orig_branch_name, orig[master_tip])
        orig_commit_oid = self.orig_factory.add_commit(
            b"foobar file content",
            "foobar.txt",
            parents=[master_tip],
            ref=orig_ref_name,
        )
        orig_blob_id = orig[orig_commit_oid].tree[0].id

        dest_path = os.path.join(self.repo_store, "to/")
        store.init_repo(dest_path, clone_from=self.orig_path)

        dest = pygit2.Repository(dest_path)
        self.assertEqual([], dest.references.objects)

        dest_ref_name = "refs/merge/123"
        store.fetch_refs.apply_async(
            args=(
                [(orig_path, orig_commit_oid.hex, dest_path, dest_ref_name)],
            )
        )
        celery_fixture.waitUntil(10, lambda: len(dest.references.objects) == 1)

        self.assertEqual(1, len(dest.references.objects))
        copied_ref = dest.references.objects[0]
        self.assertEqual(dest_ref_name, copied_ref.name)
        self.assertEqual(
            orig.references[orig_ref_name].target,
            dest.references[dest_ref_name].target,
        )
        self.assertEqual(b"foobar file content", dest[orig_blob_id].data)

        # Updating and copying again should work too, and it should be
        # compatible with using the ref name instead of the commit ID too.
        orig_commit_oid = self.orig_factory.add_commit(
            b"changed foobar content",
            "foobar.txt",
            parents=[orig_commit_oid],
            ref=orig_ref_name,
        )
        orig_blob_id = orig[orig_commit_oid].tree[0].id

        store.fetch_refs.apply_async(
            args=([(orig_path, orig_ref_name, dest_path, dest_ref_name)],)
        )

        def waitForNewCommit():
            try:
                return dest[orig_blob_id].data == b"changed foobar content"
            except KeyError:
                return False

        celery_fixture.waitUntil(10, waitForNewCommit)

        self.assertEqual(1, len(dest.references.objects))
        copied_ref = dest.references.objects[0]
        self.assertEqual(dest_ref_name, copied_ref.name)
        self.assertEqual(
            orig.references[orig_ref_name].target,
            dest.references[dest_ref_name].target,
        )
        self.assertEqual(b"changed foobar content", dest[orig_blob_id].data)

    def test_delete_ref(self):
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.makeOrig()
        orig_path = self.orig_path
        orig = self.orig_factory.repo

        master_tip = orig.references[b"refs/heads/master"].target.hex
        new_branch_name = "new-branch"
        new_ref_name = "refs/heads/new-branch"
        orig.create_branch(new_branch_name, orig[master_tip])
        self.orig_factory.add_commit(
            b"foobar file content",
            "foobar.txt",
            parents=[master_tip],
            ref=new_ref_name,
        )

        before_refs_len = len(orig.references.objects)
        operations = [(orig_path, new_ref_name)]
        store.delete_refs.apply_async((operations,))
        celery_fixture.waitUntil(
            10, lambda: len(orig.references.objects) < before_refs_len
        )

        self.assertEqual(before_refs_len - 1, len(orig.references.objects))
        self.assertNotIn(
            new_branch_name, [i.name for i in orig.references.objects]
        )

    def hasZeroLooseObjects(self, path):
        curdir = os.getcwd()
        os.chdir(path)
        objects = subprocess.check_output(["git", "count-objects"], text=True)
        if int(objects[0 : (objects.find(" objects"))]) == 0:
            os.chdir(curdir)
            return True
        else:
            os.chdir(curdir)
            return False

    def test_repack(self):
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.makeOrig()
        orig_path = self.orig_path

        # First assert we have loose objects for this repo
        self.assertFalse(self.hasZeroLooseObjects(orig_path))

        # Trigger the repack job
        store.repack.apply_async(
            queue="repacks", kwargs={"repo_path": orig_path}
        )

        # Assert we have 0 loose objects after repack job ran
        celery_fixture.waitUntil(
            10, lambda: self.hasZeroLooseObjects(orig_path)
        )

    def test_gc(self):
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.makeOrig()
        orig_path = self.orig_path

        # First assert we have loose objects for this repo
        self.assertFalse(self.hasZeroLooseObjects(orig_path))

        # Trigger the GC job
        store.gc.apply_async((orig_path,))

        # Assert we have 0 loose objects after a gc job ran
        celery_fixture.waitUntil(
            10, lambda: self.hasZeroLooseObjects(orig_path)
        )


class MergeTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))
        self.repo_path = os.path.join(self.repo_store, "repo")
        self.factory = RepoFactory(self.repo_path)
        self.repo = self.factory.build()

        self.initial_commit = self.factory.add_commit("initial", "file.txt")
        self.repo.create_branch("main", self.repo.get(self.initial_commit))
        self.repo.set_head("refs/heads/main")

        self.feature_commit = self.factory.add_commit(
            "feature", "file.txt", parents=[self.initial_commit]
        )
        self.repo.create_branch("feature", self.repo.get(self.feature_commit))

    def test_merge_successful(self):
        """Test a successful merge between two branches."""
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        self.assertIsNotNone(result["merge_commit"])
        self.assertEqual(
            self.repo.references["refs/heads/main"].target.hex,
            result["merge_commit"],
        )
        self.assertFalse(result["previously_merged"])

        merge_commit = self.repo.get(result["merge_commit"])
        self.assertEqual(merge_commit.parents[0].hex, self.initial_commit.hex)
        self.assertEqual(merge_commit.parents[1].hex, self.feature_commit.hex)
        self.assertEqual(merge_commit.committer.name, "Test User")
        self.assertEqual(merge_commit.committer.email, "test@example.com")

    def test_merge_already_included(self):
        """Test merge when source is already included in target."""
        initial_result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        # Try to merge again
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertEqual(
            initial_result["merge_commit"],
            result["merge_commit"],
        )
        self.assertTrue(result["previously_merged"])

    def test_merge_already_included_old_remerge(self):
        """Test merge when source is already included in target in the odd case
        where someone tries to re-merge a commit that has been merged many
        commits ago."""

        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        merge_commit = self.repo.get(result["merge_commit"])
        self.factory.generate_commits(1001, parents=[merge_commit.oid])
        latest_commit = self.factory.commits[-1]
        self.repo.references["refs/heads/main"].set_target(latest_commit)

        # Try to merge again
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertTrue(result["previously_merged"])
        self.assertIsNone(result["merge_commit"])

    def test_merge_already_included_old_commit(self):
        """Test merge when source is already included in target in the case
        of an old merge proposal whose target already moved on many commits."""

        self.factory.generate_commits(1001, parents=[self.initial_commit])
        latest_commit = self.factory.commits[-1]
        self.repo.references["refs/heads/main"].set_target(latest_commit)

        initial_result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        # Try to merge again
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertTrue(result["previously_merged"])
        self.assertEqual(
            initial_result["merge_commit"],
            result["merge_commit"],
        )

    def test_merge_conflicts(self):
        """Test merge with conflicts."""
        main_commit = self.factory.add_commit(
            "main content", "file.txt", parents=[self.initial_commit]
        )
        self.repo.references["refs/heads/main"].set_target(main_commit)

        feature_commit = self.factory.add_commit(
            "feature content", "file.txt", parents=[self.initial_commit]
        )
        self.repo.references["refs/heads/feature"].set_target(feature_commit)

        self.assertRaises(
            store.MergeConflicts,
            store.merge,
            self.repo_store,
            "repo",
            "main",
            main_commit.hex,
            "feature",
            feature_commit.hex,
            "Test User",
            "test@example.com",
        )

    def test_merge_custom_message(self):
        """Test merge with custom commit message."""
        custom_message = "Custom merge message"
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
            commit_message=custom_message,
        )

        merge_commit = self.repo.get(result["merge_commit"])
        self.assertEqual(merge_commit.message, custom_message)

    def test_merge_with_invalid_branch_names(self):
        """Test error handling for invalid branch names."""
        self.assertRaises(
            store.RefNotFoundError,
            store.merge,
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "invalid/branch/name",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

    def test_merge_with_nonexistent_branches(self):
        """Test error handling when branches don't exist."""
        self.assertRaises(
            store.RefNotFoundError,
            store.merge,
            self.repo_store,
            "repo",
            "nonexistent",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

    def test_merge_source_branch_moved_on(self):
        """Test error handling when source branch tip doesn't match the
        expected source_commit_sha1."""

        # Add another commit to feature branch after the merge was requested
        new_feature_commit = self.factory.add_commit(
            "new feature", "file.txt", parents=[self.feature_commit]
        )
        self.repo.references["refs/heads/feature"].set_target(
            new_feature_commit
        )

        # Try to merge using the old feature commit SHA1
        self.assertRaises(
            pygit2.GitError,
            store.merge,
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

    def test_merge_target_branch_moved_on(self):
        """Test merge is successful if target_commit_sha1 refers to a
        descendant of the current target branch tip."""

        new_main_commit = self.factory.add_commit(
            "main update", "non-conlict.txt", parents=[self.initial_commit]
        )
        self.repo.references["refs/heads/main"].set_target(new_main_commit)

        # Try to merge using the initial commit SHA1 (which is an ancestor of
        # current main)
        result = store.merge(
            self.repo_store,
            "repo",
            "main",
            new_main_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        # Verify merge was successful
        self.assertIsNotNone(result["merge_commit"])
        merge_commit = self.repo.get(result["merge_commit"])
        self.assertEqual(merge_commit.parents[0].hex, new_main_commit.hex)
        self.assertEqual(merge_commit.parents[1].hex, self.feature_commit.hex)

    def test_merge_target_commit_sha1_not_found(self):
        """Test error handling when target_commit_sha1 is no longer part of the
        target branch."""

        # Create a new branch and force update main to point to it
        new_branch_commit = self.factory.add_commit(
            "new branch", "file.txt", parents=[]
        )
        self.repo.create_branch("new_branch", self.repo.get(new_branch_commit))
        self.repo.references["refs/heads/main"].set_target(new_branch_commit)

        # Try to merge using the initial commit SHA1 which is no longer in
        # main's history
        self.assertRaises(
            pygit2.GitError,
            store.merge,
            self.repo_store,
            "repo",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )


class GetBranchTipTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))
        self.repo_path = os.path.join(self.repo_store, "repo")
        self.factory = RepoFactory(self.repo_path)
        self.repo = self.factory.build()

    def test_get_branch_tip_success(self):
        """Test getting the tip of an existing branch."""

        signature = Signature("Test", "test@example.com")
        commit_oid = self.repo.create_commit(
            "refs/heads/test_branch",
            signature,
            signature,
            "Initial commit",
            self.repo.TreeBuilder().write(),
            [],
        )

        # Get the branch tip
        tip_oid = store.get_branch_tip(self.repo, "test_branch")

        # Verify the tip matches our commit
        self.assertEqual(tip_oid, commit_oid)

    def test_get_branch_tip_nonexistent(self):
        """Test getting the tip of a non-existent branch."""
        e = self.assertRaises(
            store.RefNotFoundError,
            store.get_branch_tip,
            self.repo,
            "nonexistent_branch",
        )

        self.assertIn(
            "Branch 'refs/heads/nonexistent_branch' not found", str(e)
        )


class OpenRemoteTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))

        # Create source and target repos
        self.source_path = os.path.join(self.repo_store, "source")
        self.target_path = os.path.join(self.repo_store, "target")
        self.source_factory = RepoFactory(self.source_path)
        self.target_factory = RepoFactory(self.target_path)
        self.source_repo = self.source_factory.build()
        self.target_repo = self.target_factory.build()

    def test_open_remote_creates_and_removes_remote(self):
        """Test that open_remote creates a remote and removes it after use."""
        remote_name = "test-remote"

        with store.open_remote(
            self.source_repo, self.target_path, remote_name
        ):
            # Verify remote was created
            remote = self.source_repo.remotes[remote_name]
            self.assertEqual(remote.url, f"file://{self.target_path}")

        # Verify remote was removed
        self.assertNotIn(remote_name, self.source_repo.remotes)

    def test_open_remote_existing_remote(self):
        """Test that if a remote already exists (due to a previous clean up
        issue), open_remote will still recreate and remove the remote."""
        remote_name = "test-remote"

        self.source_repo.remotes.create(remote_name, "file://old_target_path")

        with store.open_remote(
            self.source_repo, self.target_path, remote_name
        ):
            remote = self.source_repo.remotes[remote_name]
            self.assertEqual(remote.url, f"file://{self.target_path}")

        self.assertNotIn(remote_name, self.source_repo.remotes)

    def test_open_remote_handles_errors(self):
        """Test that open_remote handles errors and still cleans up."""
        remote_name = "test-remote"

        def _open_remote_error():
            with store.open_remote(
                self.source_repo, self.target_path, remote_name
            ):
                raise Exception("Test error")

        self.assertRaises(Exception, _open_remote_error)

        # Verify remote was still removed despite error
        self.assertNotIn(remote_name, self.source_repo.remotes)


class PushTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))

        # Create source and target repos
        self.source_path = os.path.join(self.repo_store, "source")
        self.target_path = os.path.join(self.repo_store, "target")
        self.source_factory = RepoFactory(self.source_path)
        self.target_factory = RepoFactory(self.target_path)
        self.source_repo = self.source_factory.build()
        self.target_repo = self.target_factory.build()

        # Create a branch in source repo
        self.branch_name = "feature"
        self.initial_commit = self.source_factory.add_commit(
            "initial", "file.txt"
        )
        self.source_repo.create_branch(
            self.branch_name, self.source_repo.get(self.initial_commit)
        )

    def test_push_creates_internal_ref(self):
        """Test that push creates an internal ref in target repo."""
        remote_ref = store.push(
            self.repo_store, "source", self.target_repo, self.branch_name
        )

        # Verify ref was created in target repo
        self.assertIn(remote_ref, self.target_repo.references)
        target_ref = self.target_repo.references[remote_ref]
        source_ref = self.source_repo.references[
            f"refs/heads/{self.branch_name}"
        ]
        self.assertEqual(target_ref.target, source_ref.target)

    def test_push_updates_existing_ref(self):
        """Test that push updates an existing ref."""
        # First push
        remote_ref = store.push(
            self.repo_store, "source", self.target_repo, self.branch_name
        )

        # Add new commit to source branch
        new_commit = self.source_factory.add_commit(
            "new commit", "file.txt", parents=[self.initial_commit]
        )
        self.source_repo.references[
            f"refs/heads/{self.branch_name}"
        ].set_target(new_commit)

        # Push again
        store.push(
            self.repo_store, "source", self.target_repo, self.branch_name
        )

        # Verify ref was updated
        target_ref = self.target_repo.references[remote_ref]
        self.assertEqual(target_ref.target, new_commit)

    def test_push_ref_does_not_exist(self):
        """Test that push raises a RefNotFoundError is ref does not exist."""
        # First push
        self.assertRaises(
            store.RefNotFoundError,
            store.push,
            self.repo_store,
            "source",
            self.target_repo,
            "nonexisting",
        )
        remote_ref = store.push(
            self.repo_store, "source", self.target_repo, self.branch_name
        )

        # Add new commit to source branch
        new_commit = self.source_factory.add_commit(
            "new commit", "file.txt", parents=[self.initial_commit]
        )
        self.source_repo.references[
            f"refs/heads/{self.branch_name}"
        ].set_target(new_commit)

        # Push again
        store.push(
            self.repo_store, "source", self.target_repo, self.branch_name
        )

        # Verify ref was updated
        target_ref = self.target_repo.references[remote_ref]
        self.assertEqual(target_ref.target, new_commit)


class CrossRepoMergeTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))

        # Create target repo
        self.target_path = os.path.join(self.repo_store, "target")
        self.target_factory = RepoFactory(self.target_path)
        self.target_repo = self.target_factory.build()

        self.target_initial = self.target_factory.add_commit(
            "target initial", "file.txt"
        )
        self.target_branch = self.target_repo.create_branch(
            "main", self.target_repo.get(self.target_initial)
        )
        self.target_repo.set_head("refs/heads/main")

        # Create source repo (forked from target)
        self.source_path = os.path.join(self.repo_store, "source")
        self.source_factory = RepoFactory(
            self.source_path, clone_from=self.target_factory
        )
        self.source_repo = self.source_factory.build()
        self.source_initial = self.target_initial
        self.source_branch = self.source_repo.create_branch(
            "feature", self.source_repo.get(self.source_initial)
        )

    def test_cross_repo_merge_successful(self):
        """Test successful merge from source repo to target repo."""
        # Add commits to both branches
        source_commit = self.source_factory.add_commit(
            "source change",
            "file.txt",
            parents=[self.source_initial],
        )
        self.source_repo.lookup_reference(self.source_branch.name).set_target(
            source_commit
        )

        result = store.merge(
            self.repo_store,
            "target:source",  # target:source format for cross-repo
            "main",
            self.target_initial.hex,
            "feature",
            source_commit.hex,
            "Test User",
            "test@example.com",
        )

        # Verify merge was successful
        self.assertIsNotNone(result["merge_commit"])
        merge_commit = self.target_repo.get(result["merge_commit"])
        self.assertEqual(merge_commit.parents[0].hex, self.target_initial.hex)
        self.assertEqual(merge_commit.parents[1].hex, source_commit.hex)
        self.assertEqual(
            merge_commit.message, "Merge branch 'feature' into main"
        )

        # Verify temporary ref was cleaned up
        self.assertIsNone(
            self.target_repo.references.get("refs/internal/source-feature")
        )

    def test_cross_repo_merge_commit_message(self):
        """Test if a commit message is sent, it's used fot the merge commit."""
        source_commit = self.source_factory.add_commit(
            "source change",
            "file.txt",
            parents=[self.source_initial],
        )
        self.source_repo.lookup_reference(self.source_branch.name).set_target(
            source_commit
        )

        result = store.merge(
            self.repo_store,
            "target:source",
            "main",
            self.target_initial.hex,
            "feature",
            source_commit.hex,
            "Test User",
            "test@example.com",
            "A test commit message",
        )

        self.assertIsNotNone(result["merge_commit"])
        merge_commit = self.target_repo.get(result["merge_commit"])
        self.assertEqual(merge_commit.message, "A test commit message")

    def test_cross_repo_merge_conflicts(self):
        """Test merge conflicts when merging from source repo."""
        # Create conflicting changes in both repos
        source_commit = self.source_factory.add_commit(
            "source change", "file.txt", parents=[self.source_initial]
        )
        self.source_repo.lookup_reference(self.source_branch.name).set_target(
            source_commit
        )
        target_commit = self.target_factory.add_commit(
            "target change", "file.txt", parents=[self.target_initial]
        )
        self.target_repo.lookup_reference(self.target_branch.name).set_target(
            target_commit
        )

        # Try to merge
        e = self.assertRaises(
            store.MergeConflicts,
            store.merge,
            self.repo_store,
            "target:source",
            "main",
            target_commit.hex,
            "feature",
            source_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertEqual(
            f"Merge conflicts detected between {target_commit.hex} "
            f"(main) and {source_commit.hex} (feature)",
            str(e),
        )

        # Verify temporary ref was cleaned up
        self.assertIsNone(
            self.target_repo.references.get("refs/internal/source-feature")
        )

    def test_cross_repo_merge_source_branch_moved(self):
        """Test error when source branch tip doesn't match expected commit."""
        # Add commit to source branch after merge was requested
        new_source_commit = self.source_factory.add_commit(
            "new source", "file.txt", parents=[self.source_initial]
        )
        self.source_repo.references[self.source_branch.name].set_target(
            new_source_commit
        )

        # Try to merge using old source commit
        e = self.assertRaises(
            pygit2.GitError,
            store.merge,
            self.repo_store,
            "target:source",
            "main",
            self.target_initial.hex,
            "feature",
            self.source_initial.hex,
            "Test User",
            "test@example.com",
        )
        self.assertEqual("The tip of the source branch has changed", str(e))


class RequestMergeTestCase(TestCase):
    def setUp(self):
        super().setUp()

        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))

        self.target_repo_path = os.path.join(self.repo_store, "target")
        self.target_factory = RepoFactory(self.target_repo_path)
        self.target_repo = self.target_factory.build()

        self.initial_commit = self.target_factory.add_commit(
            "initial", "file.txt"
        )
        self.target_repo.create_branch(
            "main", self.target_repo.get(self.initial_commit)
        )
        self.target_repo.set_head("refs/heads/main")
        self.feature_commit = self.target_factory.add_commit(
            "feature", "file.txt", parents=[self.initial_commit]
        )
        self.target_repo.create_branch(
            "feature", self.target_repo.get(self.feature_commit)
        )

        self.source_repo_path = os.path.join(self.repo_store, "source")
        self.source_factory = RepoFactory(
            self.source_repo_path, clone_from=self.target_factory
        )
        self.source_repo = self.source_factory.build()

    def test_request_merge_successful(self):
        """Test successful request merge."""
        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            result = store.request_merge(
                self.repo_store,
                "target",
                "main",
                self.initial_commit.hex,
                "feature",
                self.feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            self.assertTrue(result["queued"])
            self.assertFalse(result["already_merged"])
            mock_apply_async.assert_called_once_with(
                kwargs={
                    "repo_store": self.repo_store,
                    "repo_name": "target",
                    "source_repo_name": None,
                    "target_branch": "main",
                    "target_commit_sha1": self.initial_commit.hex,
                    "source_branch": "feature",
                    "source_commit_sha1": self.feature_commit.hex,
                    "committer_name": "Test User",
                    "committer_email": "test@example.com",
                    "commit_message": None,
                }
            )

    def test_request_merge_successful_source_same_as_target(self):
        """Test successful request merge."""
        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            result = store.request_merge(
                self.repo_store,
                "target:target",
                "main",
                self.initial_commit.hex,
                "feature",
                self.feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            self.assertTrue(result["queued"])
            self.assertFalse(result["already_merged"])
            mock_apply_async.assert_called_once_with(
                kwargs={
                    "repo_store": self.repo_store,
                    "repo_name": "target",
                    "source_repo_name": None,
                    "target_branch": "main",
                    "target_commit_sha1": self.initial_commit.hex,
                    "source_branch": "feature",
                    "source_commit_sha1": self.feature_commit.hex,
                    "committer_name": "Test User",
                    "committer_email": "test@example.com",
                    "commit_message": None,
                }
            )

    def test_request_merge_already_merged(self):
        """Test request merge with already merged branches."""

        self.target_repo.references["refs/heads/main"].set_target(
            self.feature_commit
        )
        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            result = store.request_merge(
                self.repo_store,
                "target",
                "main",
                self.initial_commit.hex,
                "feature",
                self.feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            self.assertFalse(result["queued"])
            self.assertTrue(result["already_merged"])
            mock_apply_async.assert_not_called()

    def test_cross_repo_request_merge_successful(self):
        """Test successful cross-repo request merge."""

        feature_commit = self.source_factory.add_commit(
            "feature", "file.txt", parents=[self.initial_commit]
        )
        self.source_repo.create_branch(
            "feature", self.source_repo.get(feature_commit)
        )

        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            result = store.request_merge(
                self.repo_store,
                "target:source",
                "main",
                self.initial_commit.hex,
                "feature",
                feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            self.assertTrue(result["queued"])
            self.assertFalse(result["already_merged"])
            mock_apply_async.assert_called_once_with(
                kwargs={
                    "repo_store": self.repo_store,
                    "repo_name": "target",
                    "source_repo_name": "source",
                    "target_branch": "main",
                    "target_commit_sha1": self.initial_commit.hex,
                    "source_branch": "feature",
                    "source_commit_sha1": feature_commit.hex,
                    "committer_name": "Test User",
                    "committer_email": "test@example.com",
                    "commit_message": None,
                }
            )

    def test_request_merge_source_branch_not_found(self):
        """Test request merge with a non-existent source branch."""
        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            self.assertRaises(
                store.RefNotFoundError,
                store.request_merge,
                self.repo_store,
                "target",
                "main",
                self.initial_commit.hex,
                "nonexistent_branch",
                self.feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            mock_apply_async.assert_not_called()

    def test_request_merge_cross_repo_source_branch_not_found(self):
        """Test request merge cross-repo  with a non-existent source branch."""

        with mock.patch(
            "turnip.api.store.merge_async.apply_async"
        ) as mock_apply_async:
            self.assertRaises(
                store.RefNotFoundError,
                store.request_merge,
                self.repo_store,
                "target:source",
                "main",
                self.initial_commit.hex,
                "nonexistent_branch",
                self.feature_commit.hex,
                "Test User",
                "test@example.com",
            )
            mock_apply_async.assert_not_called()

    def test_merge_source_branch_moved(self):
        """Test error when source branch tip doesn't match expected commit."""

        new_feature_commit = self.target_factory.add_commit(
            "new source", "file.txt", parents=[self.feature_commit]
        )
        self.target_repo.references["refs/heads/feature"].set_target(
            new_feature_commit
        )

        # Try to merge using old source commit
        e = self.assertRaises(
            pygit2.GitError,
            store.request_merge,
            self.repo_store,
            "target",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertEqual("The tip of the source branch has changed", str(e))

    def _setup_XML_RPC(self):
        """Set up test XML-RPC server"""
        self.virtinfo = FakeVirtInfoService(allowNone=True)
        self.virtinfo_listener = default_reactor.listenTCP(
            0, server.Site(self.virtinfo)
        )
        self.virtinfo_port = self.virtinfo_listener.getHost().port
        self.virtinfo_url = b"http://localhost:%d/" % self.virtinfo_port
        self.addCleanup(self.virtinfo_listener.stopListening)
        config.defaults["virtinfo_endpoint"] = self.virtinfo_url

    def test_request_merge_successful_async(self):
        """Test a successful request merge using celery."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)
        self._setup_XML_RPC()

        # Start merge
        result = store.request_merge(
            self.repo_store,
            "target",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertTrue(result["queued"])
        self.assertFalse(result["already_merged"])

        # Wait for the merge commit to appear on main
        def merge_done():
            ref = self.target_repo.references["refs/heads/main"]
            commit = self.target_repo.get(ref.target)
            return len(commit.parent_ids) == 2 and (
                self.initial_commit.hex in [p.hex for p in commit.parents]
                and self.feature_commit.hex in [p.hex for p in commit.parents]
            )

        celery_fixture.waitUntil(10, merge_done)

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)
        self.assertEqual(len(commit.parent_ids), 2)
        self.assertIn(self.initial_commit.hex, [p.hex for p in commit.parents])
        self.assertIn(self.feature_commit.hex, [p.hex for p in commit.parents])
        self.assertEqual(commit.committer.name, "Test User")
        self.assertEqual(commit.committer.email, "test@example.com")

    def test_cross_repo_request_merge_successful_async(self):
        """Test a successful cross-repo request merge using celery."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        # Add a new commit to source feature branch
        feature_commit = self.source_factory.add_commit(
            "feature", "file.txt", parents=[self.initial_commit]
        )
        self.source_repo.create_branch(
            "feature", self.source_repo.get(feature_commit)
        )

        result = store.request_merge(
            self.repo_store,
            "target:source",
            "main",
            self.initial_commit.hex,
            "feature",
            feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertTrue(result["queued"])
        self.assertFalse(result["already_merged"])

        # Wait for the merge commit to appear on main in the target repo
        def merge_done():
            ref = self.target_repo.references["refs/heads/main"]
            commit = self.target_repo.get(ref.target)
            return len(commit.parent_ids) == 2 and (
                self.initial_commit.hex in [p.hex for p in commit.parents]
                and feature_commit.hex in [p.hex for p in commit.parents]
            )

        celery_fixture.waitUntil(10, merge_done)

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)
        self.assertEqual(len(commit.parent_ids), 2)
        self.assertIn(self.initial_commit.hex, [p.hex for p in commit.parents])
        self.assertIn(feature_commit.hex, [p.hex for p in commit.parents])
        self.assertEqual(commit.committer.name, "Test User")
        self.assertEqual(commit.committer.email, "test@example.com")
        # Temporary ref should be cleaned up
        self.assertIsNone(
            self.target_repo.references.get("refs/internal/source-feature")
        )

    def test_request_merge_conflicts_async(self):
        """Test request merge with conflicts using celery."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        # Create conflicting changes in both branches
        main_commit = self.target_factory.add_commit(
            "main content", "file.txt", parents=[self.initial_commit]
        )
        self.target_repo.references["refs/heads/main"].set_target(main_commit)

        feature_commit = self.target_factory.add_commit(
            "feature content", "file.txt", parents=[self.initial_commit]
        )
        self.target_repo.references["refs/heads/feature"].set_target(
            feature_commit
        )

        # Start merge
        result = store.request_merge(
            self.repo_store,
            "target",
            "main",
            main_commit.hex,
            "feature",
            feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertTrue(result["queued"])
        self.assertFalse(result["already_merged"])

        # Wait for a short time and check that the main branch tip did not
        # change (no merge commit)
        def merge_failed():
            ref = self.target_repo.references["refs/heads/main"]
            commit = self.target_repo.get(ref.target)
            # Should still be the main_commit and not a merge commit
            return (
                commit.hex == main_commit.hex and len(commit.parent_ids) == 1
            )

        celery_fixture.waitUntil(5, merge_failed)

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)
        self.assertEqual(commit.hex, main_commit.hex)
        self.assertEqual(len(commit.parent_ids), 1)

    def test_request_merge_already_merged_async(self):
        """Test request merge when source is already merged into target
        (async)."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        # Simulate already merged: set main to feature commit
        self.target_repo.references["refs/heads/main"].set_target(
            self.feature_commit
        )

        result = store.request_merge(
            self.repo_store,
            "target",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )
        self.assertFalse(result["queued"])
        self.assertTrue(result["already_merged"])
        # No new merge commit should appear
        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)
        self.assertEqual(commit.hex, self.feature_commit.hex)
        self.assertEqual(len(commit.parent_ids), 1)

    def test_request_merge_source_branch_moved_async(self):
        """Test request merge source branch tip moved forward (async)"""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        new_feature_commit = self.target_factory.add_commit(
            "new feature", "file.txt", parents=[self.feature_commit]
        )
        self.target_repo.references["refs/heads/feature"].set_target(
            new_feature_commit
        )

        self.assertRaises(
            pygit2.GitError,
            store.request_merge,
            self.repo_store,
            "target",
            "main",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,  # old tip
            "Test User",
            "test@example.com",
        )

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)

        # No merge happended
        self.assertEqual(commit.hex, self.initial_commit.hex)

    def test_request_merge_target_branch_not_found_async(self):
        """Test request merge when target branch does not exist (async)."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.assertRaises(
            store.RefNotFoundError,
            store.request_merge,
            self.repo_store,
            "target",
            "nonexistent",
            self.initial_commit.hex,
            "feature",
            self.feature_commit.hex,
            "Test User",
            "test@example.com",
        )

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)

        # No merge happended
        self.assertEqual(commit.hex, self.initial_commit.hex)

    def test_request_merge_source_sha1_not_found_async(self):
        """Test request merge when source sha1 does not exist (async)."""
        self._setup_XML_RPC()
        celery_fixture = CeleryWorkerFixture()
        self.useFixture(celery_fixture)

        self.assertRaises(
            store.GitError,
            store.request_merge,
            self.repo_store,
            "target",
            "main",
            self.initial_commit.hex,
            "feature",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # non-existent sha1
            "Test User",
            "test@example.com",
        )

        ref = self.target_repo.references["refs/heads/main"]
        commit = self.target_repo.get(ref.target)
        # No merge happended
        self.assertEqual(commit.hex, self.initial_commit.hex)


class DiffStatsStoreTestCase(TestCase):
    """Test cases for the get_diff_stats function in the store module."""

    def setUp(self):
        super().setUp()
        self.repo_store = self.useFixture(TempDir()).path
        self.useFixture(EnvironmentVariable("REPO_STORE", self.repo_store))
        self.repo_path = os.path.join(self.repo_store, uuid.uuid1().hex)
        self.factory = RepoFactory(self.repo_path)

        # Create base commits
        c1 = self.factory.add_commit("d", "to_delete.txt")
        c2 = self.factory.add_commit("a", "file.txt", parents=[c1])
        self.base = self.factory.add_commit("r", "to_rename.txt", parents=[c2])

    def test_get_diff_stats_non_existing_sha1_from(self):
        """Test file modifications are correctly identified in diff stats"""
        c1 = self.factory.add_commit("b", "file.txt", parents=[self.base])
        self.assertRaises(
            ValueError,
            store.get_diff_stats,
            self.repo_store,
            self.repo_path,
            "unkonwn",
            c1.hex,
            "..",
        )

    def test_get_diff_stats_non_existing_sha1_to(self):
        """Test file modifications are correctly identified in diff stats"""
        self.assertRaises(
            ValueError,
            store.get_diff_stats,
            self.repo_store,
            self.repo_path,
            self.base,
            "unkonwn",
            "..",
        )

    def test_get_diff_stats_basic_modified(self):
        """Test file modifications are correctly identified in diff stats"""
        c1 = self.factory.add_commit("b", "file.txt", parents=[self.base])
        stats_mod = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, c1.hex, ".."
        )
        self.assertEqual([], stats_mod["added"])
        self.assertIn("file.txt", stats_mod["modified"])
        self.assertEqual([], stats_mod["deleted"])
        self.assertEqual([], stats_mod["renamed"])

    def test_get_diff_stats_basic_added(self):
        """Test file additions are correctly identified in diff stats"""
        c1 = self.factory.add_commit("n", "new.txt", parents=[self.base])
        stats_add = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, c1.hex, ".."
        )
        self.assertIn("new.txt", stats_add["added"])
        self.assertEqual([], stats_add["modified"])
        self.assertEqual([], stats_add["deleted"])
        self.assertEqual([], stats_add["renamed"])

    def test_get_diff_stats_basic_deleted(self):
        """Test file deletions are correctly identified in diff stats"""
        # Delete file.txt in next commit (by removing then committing a change)
        self.factory.repo.index.remove("file.txt")
        c1 = self.factory.add_commit("y", "another.txt", parents=[self.base])
        stats_del = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, c1.hex, ".."
        )
        self.assertIn("another.txt", stats_del["added"])
        self.assertEqual([], stats_del["modified"])
        self.assertIn("file.txt", stats_del["deleted"])
        self.assertEqual([], stats_del["renamed"])

    def test_get_diff_stats_renamed(self):
        """Test file renames are correctly identified in diff stats"""
        self.factory.repo.index.remove("to_rename.txt")
        c1 = self.factory.add_commit("r", "renamed.txt", parents=[self.base])

        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, c1.hex, ".."
        )
        self.assertEqual([], stats["added"])
        self.assertEqual([], stats["deleted"])
        self.assertEqual([], stats["modified"])
        self.assertEqual(1, len(stats["renamed"]))
        self.assertEqual(
            {"old": "to_rename.txt", "new": "renamed.txt"}, stats["renamed"][0]
        )

    def test_get_diff_stats_multiple_added_modified_deleted(self):
        """Test diff stats with multiple changes across several commits"""
        # Create commits to compare against
        c1 = self.factory.add_commit("f", "file.txt", parents=[self.base])
        c2 = self.factory.add_commit("n", "new.txt", parents=[c1])
        self.factory.repo.index.remove("to_delete.txt")
        c3 = self.factory.add_commit("a", "another.txt", parents=[c2])

        self.factory.repo.index.remove("to_rename.txt")
        c4 = self.factory.add_commit("r", "renamed.txt", parents=[c3])

        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, c4.hex, ".."
        )
        self.assertIn("new.txt", stats["added"])
        self.assertIn("another.txt", stats["added"])
        self.assertIn("file.txt", stats["modified"])
        self.assertIn("to_delete.txt", stats["deleted"])
        self.assertIn(
            {"old": "to_rename.txt", "new": "renamed.txt"}, stats["renamed"]
        )

    def test_get_diff_stats_no_from_sha(self):
        """Test diff stats when comparing against no source commit"""
        c1 = self.factory.add_commit("b", "file.txt", parents=[self.base])
        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, None, c1.hex, "..."
        )
        self.assertIn("file.txt", stats["added"])
        self.assertEqual([], stats["modified"])
        self.assertEqual([], stats["deleted"])
        self.assertEqual([], stats["renamed"])

        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, "", c1.hex, "..."
        )
        self.assertIn("file.txt", stats["added"])
        self.assertEqual([], stats["modified"])
        self.assertEqual([], stats["deleted"])
        self.assertEqual([], stats["renamed"])

    def test_get_diff_stats_triple_dot_uses_merge_base(self):
        """Test that triple-dot diff notation correctly uses common base"""
        left = self.factory.add_commit("left", "left.txt", parents=[self.base])
        self.factory.repo.index.remove("left.txt")
        right = self.factory.add_commit(
            "right", "right.txt", parents=[self.base]
        )

        # Compare left...right should use merge-base (base) vs right
        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, left.hex, right.hex, "..."
        )
        self.assertIn("right.txt", stats["added"])
        self.assertNotIn("left.txt", stats["added"])

    def test_get_diff_stats_empty_diff(self):
        """Test diff stats when comparing identical commits"""
        stats = store.get_diff_stats(
            self.repo_store, self.repo_path, self.base.hex, self.base.hex, ".."
        )
        self.assertEqual([], stats["added"])
        self.assertEqual([], stats["modified"])
        self.assertEqual([], stats["deleted"])
        self.assertEqual([], stats["renamed"])

    def test_cross_repo_basic_diff_stats(self):
        """Compare commits across repos using ephemeral alternates."""
        # Create target repo
        target_path = os.path.join(self.repo_store, "target")
        target_factory = RepoFactory(target_path)
        target_repo = target_factory.build()
        shared_base = target_factory.add_commit("base", "base.txt")
        target_repo.create_branch("main", target_repo.get(shared_base))
        target_repo.set_head("refs/heads/main")

        # Create source repo as a clone of target (shares history up to base)
        source_path = os.path.join(self.repo_store, "source")
        source_factory = RepoFactory(source_path, clone_from=target_factory)
        source_change = source_factory.add_commit(
            "source change", "right.txt", parents=[shared_base]
        )

        stats = store.get_diff_stats(
            self.repo_store,
            "target:source",
            shared_base.hex,
            source_change.hex,
            "..",
        )
        self.assertIn("right.txt", stats["added"])
        self.assertEqual([], stats["modified"])

    def test_cross_repo_empty_from_diff_stats(self):
        """Empty 'from' should diff against empty tree across repos."""
        # Create target repo
        target_path = os.path.join(self.repo_store, "target")
        target_factory = RepoFactory(target_path)
        target_repo = target_factory.build()
        shared_base = target_factory.add_commit("base", "base.txt")
        target_repo.create_branch("main", target_repo.get(shared_base))
        target_repo.set_head("refs/heads/main")

        # Create source repo as a clone of target (shares history up to base)
        source_path = os.path.join(self.repo_store, "source")
        source_factory = RepoFactory(source_path, clone_from=target_factory)
        source_change = source_factory.add_commit(
            "source change", "right.txt", parents=[shared_base]
        )

        stats = store.get_diff_stats(
            self.repo_store,
            "target:source",
            None,
            source_change.hex,
            "..",
        )
        self.assertIn("base.txt", stats["added"])  # from initial commit
        self.assertIn("right.txt", stats["added"])  # from source commit
