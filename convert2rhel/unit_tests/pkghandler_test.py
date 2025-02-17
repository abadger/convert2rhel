# -*- coding: utf-8 -*-
#
# Copyright(C) 2016 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
__metaclass__ = type


import glob
import os
import re

from collections import namedtuple

import pytest
import rpm
import six

from convert2rhel import backup, pkghandler, pkgmanager, unit_tests, utils
from convert2rhel.pkghandler import (
    PackageInformation,
    PackageNevra,
    _get_packages_to_update_dnf,
    _get_packages_to_update_yum,
    get_total_packages_to_update,
)
from convert2rhel.systeminfo import Version, system_info
from convert2rhel.toolopts import tool_opts
from convert2rhel.unit_tests import (
    CallYumCmdMocked,
    DownloadPkgMocked,
    RemovePkgsMocked,
    RunSubprocessMocked,
    SysExitCallableObject,
    TestPkgObj,
    create_pkg_information,
    create_pkg_obj,
    is_rpm_based_os,
    mock_decorator,
)
from convert2rhel.unit_tests.conftest import all_systems, centos7, centos8


six.add_move(six.MovedModule("mock", "mock", "unittest.mock"))
from six.moves import mock


YUM_KERNEL_LIST_OLDER_AVAILABLE = """Installed Packages
kernel.x86_64    4.7.4-200.fc24   @updates
Available Packages
kernel.x86_64    4.5.5-300.fc24   fedora
kernel.x86_64    4.7.2-201.fc24   @updates
kernel.x86_64    4.7.4-200.fc24   @updates"""

YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE = """Installed Packages
kernel.x86_64    4.7.4-200.fc24   @updates
Available Packages
kernel.x86_64    4.7.4-200.fc24   @updates"""

YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE_MULTIPLE_INSTALLED = """Installed Packages
kernel.x86_64    4.7.2-201.fc24   @updates
kernel.x86_64    4.7.4-200.fc24   @updates
Available Packages
kernel.x86_64    4.7.4-200.fc24   @updates"""


class GetInstalledPkgsWDifferentFingerprintMocked(unit_tests.MockFunction):
    def __init__(self):
        self.is_only_rhel_kernel_installed = False
        self.called = 0

    def __call__(self, *args, **kwargs):
        self.called += 1
        if self.is_only_rhel_kernel_installed:
            return []  # No third-party kernel
        else:
            return [
                create_pkg_information(
                    name="kernel",
                    version="3.10.0",
                    release="1127.19.1.el7",
                    arch="x86_64",
                    packager="Oracle",
                ),
                create_pkg_information(
                    name="kernel-uek",
                    version="0.1",
                    release="1",
                    arch="x86_64",
                    packager="Oracle",
                ),
                create_pkg_information(
                    name="kernel-headers",
                    version="0.1",
                    release="1",
                    arch="x86_64",
                    packager="Oracle",
                ),
                create_pkg_information(
                    name="kernel-uek-headers",
                    version="0.1",
                    release="1",
                    arch="x86_64",
                    packager="Oracle",
                ),
                create_pkg_information(
                    name="kernel-firmware",
                    version="0.1",
                    release="1",
                    arch="x86_64",
                    packager="Oracle",
                ),
                create_pkg_information(
                    name="kernel-uek-firmware",
                    version="0.1",
                    release="1",
                    arch="x86_64",
                    packager="Oracle",
                ),
            ]


class PrintPkgInfoMocked(unit_tests.MockFunction):
    def __init__(self):
        self.called = 0
        self.pkgs = []

    def __call__(self, pkgs):
        self.called += 1
        self.pkgs = pkgs


class QueryMocked(unit_tests.MockFunction):
    def __init__(self):
        self.filter_called = 0
        self.installed_called = 0
        self.stop_iteration = False
        self.pkg_obj = None

    def __call__(self, *args):
        self._setup_pkg()
        return self

    def __iter__(self):  # pylint: disable=non-iterator-returned
        return self

    def __next__(self):
        if self.stop_iteration or not self.pkg_obj:
            self.stop_iteration = False
            raise StopIteration
        self.stop_iteration = True
        return self.pkg_obj

    def _setup_pkg(self):
        self.pkg_obj = TestPkgObj()
        self.pkg_obj.name = "installed_pkg"

    def filterm(self, empty):
        # Called internally in DNF when calling fill_sack - ignore, not needed
        pass

    def installed(self):
        self.installed_called += 1
        return self

    def filter(self, name__glob, **kwargs):
        self.filter_called += 1
        if name__glob and name__glob == "installed_pkg":
            self._setup_pkg()
        elif name__glob:
            self.pkg_obj = None
        return self


class ReturnPackagesMocked(unit_tests.MockFunction):
    def __call__(self, patterns=None):
        if patterns:
            if "non_existing" in patterns:
                return []

        pkg_obj = TestPkgObj()
        pkg_obj.name = "installed_pkg"
        return [pkg_obj]


class DownloadPkgsMocked(unit_tests.MockFunction):
    def __init__(self, destdir=None):
        self.called = 0
        self.to_return = ["/path/to.rpm"]
        self.destdir = destdir

    def __call__(self, pkgs, dest, reposdir=None):
        self.called += 1
        self.pkgs = pkgs
        self.dest = dest
        self.reposdir = reposdir
        if self.destdir and not os.path.exists(self.destdir):
            os.mkdir(self.destdir, 0o700)
        return self.to_return


class StoreContentMocked(unit_tests.MockFunction):
    def __init__(self):
        self.called = 0
        self.filename = None
        self.content = None

    def __call__(self, filename, content):
        self.called += 1
        self.filename = filename
        self.content = content
        return True


class TransactionSetMocked(unit_tests.MockFunction):
    def __call__(self):
        return self

    def dbMatch(self, key="name", value=""):
        db = [
            {
                rpm.RPMTAG_NAME: "pkg1",
                rpm.RPMTAG_VERSION: "1",
                rpm.RPMTAG_RELEASE: "2",
                rpm.RPMTAG_EVR: "1-2",
            },
            {
                rpm.RPMTAG_NAME: "pkg2",
                rpm.RPMTAG_VERSION: "2",
                rpm.RPMTAG_RELEASE: "3",
                rpm.RPMTAG_EVR: "2-3",
            },
        ]
        if key != "name":  # everything other than 'name' is unsupported ATM :)
            return []
        if not value:
            return db
        else:
            return [db_entry for db_entry in db if db_entry[rpm.RPMTAG_NAME] == value]


class TestClearVersionlock:
    def test_clear_versionlock_plugin_not_enabled(self, caplog, monkeypatch):
        monkeypatch.setattr(os.path, "isfile", mock.Mock(return_value=False))
        monkeypatch.setattr(os.path, "getsize", mock.Mock(return_value=0))

        pkghandler.clear_versionlock()

        assert len(caplog.records) == 1
        assert caplog.records[-1].message == "Usage of YUM/DNF versionlock plugin not detected."

    def test_clear_versionlock_user_says_yes(self, monkeypatch):
        monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
        monkeypatch.setattr(os.path, "isfile", mock.Mock(return_value=True))
        monkeypatch.setattr(os.path, "getsize", mock.Mock(return_value=1))
        monkeypatch.setattr(pkghandler, "call_yum_cmd", CallYumCmdMocked())
        monkeypatch.setattr(backup.RestorableFile, "backup", mock.Mock())
        monkeypatch.setattr(backup.RestorableFile, "restore", mock.Mock())

        pkghandler.clear_versionlock()

        assert pkghandler.call_yum_cmd.call_count == 1
        assert pkghandler.call_yum_cmd.command == "versionlock"
        assert pkghandler.call_yum_cmd.args == ["clear"]

    def test_clear_versionlock_user_says_no(self, monkeypatch):
        monkeypatch.setattr(utils, "ask_to_continue", SysExitCallableObject(spec=utils.ask_to_continue))
        monkeypatch.setattr(os.path, "isfile", mock.Mock(return_value=True))
        monkeypatch.setattr(os.path, "getsize", mock.Mock(return_value=1))
        monkeypatch.setattr(pkghandler, "call_yum_cmd", mock.Mock())

        with pytest.raises(SystemExit):
            pkghandler.clear_versionlock()

        assert not pkghandler.call_yum_cmd.called


class TestCallYumCmd:
    def test_call_yum_cmd(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(8, 0))
        monkeypatch.setattr(system_info, "releasever", "8")
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())

        pkghandler.call_yum_cmd("install")

        assert utils.run_subprocess.cmd == [
            "yum",
            "install",
            "-y",
            "--releasever=8",
            "--setopt=module_platform_id=platform:el8",
        ]

    def test_call_yum_cmd_not_setting_releasever(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", "7Server")
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())

        pkghandler.call_yum_cmd("install", set_releasever=False)

        assert utils.run_subprocess.cmd == ["yum", "install", "-y"]

    def test_call_yum_cmd_with_disablerepo_and_enablerepo(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())
        monkeypatch.setattr(tool_opts, "no_rhsm", True)
        monkeypatch.setattr(tool_opts, "disablerepo", ["*"])
        monkeypatch.setattr(tool_opts, "enablerepo", ["rhel-7-extras-rpm"])

        pkghandler.call_yum_cmd("install")

        assert utils.run_subprocess.cmd == [
            "yum",
            "install",
            "-y",
            "--disablerepo=*",
            "--enablerepo=rhel-7-extras-rpm",
        ]

    def test_call_yum_cmd_with_submgr_enabled_repos(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())
        monkeypatch.setattr(system_info, "submgr_enabled_repos", ["rhel-7-extras-rpm"])
        monkeypatch.setattr(tool_opts, "enablerepo", ["not-to-be-used-in-the-yum-call"])

        pkghandler.call_yum_cmd("install")

        assert utils.run_subprocess.cmd == ["yum", "install", "-y", "--enablerepo=rhel-7-extras-rpm"]

    def test_call_yum_cmd_with_repo_overrides(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())
        monkeypatch.setattr(system_info, "submgr_enabled_repos", ["not-to-be-used-in-the-yum-call"])
        monkeypatch.setattr(tool_opts, "enablerepo", ["not-to-be-used-in-the-yum-call"])

        pkghandler.call_yum_cmd("install", ["pkg"], enable_repos=[], disable_repos=[])

        assert utils.run_subprocess.cmd == ["yum", "install", "-y", "pkg"]

        pkghandler.call_yum_cmd(
            "install",
            ["pkg"],
            enable_repos=["enable-repo"],
            disable_repos=["disable-repo"],
        )

        assert utils.run_subprocess.cmd == [
            "yum",
            "install",
            "-y",
            "--disablerepo=disable-repo",
            "--enablerepo=enable-repo",
            "pkg",
        ]


class TestGetRpmHeader:
    @pytest.mark.skipif(
        not is_rpm_based_os(),
        reason="Current test runs only on rpm based systems.",
    )
    def test_get_rpm_header(self, monkeypatch):
        monkeypatch.setattr(rpm, "TransactionSet", TransactionSetMocked())
        pkg = create_pkg_obj(name="pkg1", version="1", release="2")

        hdr = pkghandler.get_rpm_header(pkg)

        assert hdr == {
            rpm.RPMTAG_NAME: "pkg1",
            rpm.RPMTAG_VERSION: "1",
            rpm.RPMTAG_RELEASE: "2",
            rpm.RPMTAG_EVR: "1-2",
        }

    def test_get_rpm_header_failure(self, monkeypatch):
        monkeypatch.setattr(rpm, "TransactionSet", TransactionSetMocked())
        unknown_pkg = create_pkg_obj(name="unknown", version="1", release="1")

        with pytest.raises(SystemExit):
            pkghandler.get_rpm_header(unknown_pkg)


class TestPreserveOnlyRHELKernel:
    def test_preserve_only_rhel_kernel(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(pkghandler, "install_rhel_kernel", lambda: True)
        monkeypatch.setattr(pkghandler, "fix_invalid_grub2_entries", lambda: None)
        monkeypatch.setattr(pkghandler, "remove_non_rhel_kernels", mock.Mock(return_value=[]))
        monkeypatch.setattr(pkghandler, "install_gpg_keys", mock.Mock())
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())
        monkeypatch.setattr(pkghandler, "get_installed_pkgs_by_fingerprint", mock.Mock(return_value=["kernel"]))
        monkeypatch.setattr(system_info, "name", "CentOS7")
        monkeypatch.setattr(system_info, "arch", "x86_64")
        monkeypatch.setattr(utils, "store_content_to_file", mock.Mock())

        pkghandler.preserve_only_rhel_kernel()

        assert utils.run_subprocess.cmd == ["yum", "update", "-y", "kernel"]
        assert pkghandler.get_installed_pkgs_by_fingerprint.call_count == 1


class TestGetKernelAvailability:
    @pytest.mark.parametrize(
        ("subprocess_output", "expected_installed", "expected_available"),
        (
            (
                YUM_KERNEL_LIST_OLDER_AVAILABLE,
                ["4.7.4-200.fc24"],
                ["4.5.5-300.fc24", "4.7.2-201.fc24", "4.7.4-200.fc24"],
            ),
            (YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE, ["4.7.4-200.fc24"], ["4.7.4-200.fc24"]),
            (
                YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE_MULTIPLE_INSTALLED,
                ["4.7.2-201.fc24", "4.7.4-200.fc24"],
                ["4.7.4-200.fc24"],
            ),
        ),
    )
    def test_get_kernel_availability(self, subprocess_output, expected_installed, expected_available, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=subprocess_output))

        installed, available = pkghandler.get_kernel_availability()

        assert installed == expected_installed
        assert available == expected_available


class TestHandleNoNewerRHELKernelAvailable:
    def test_handle_older_rhel_kernel_available(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=YUM_KERNEL_LIST_OLDER_AVAILABLE))

        pkghandler.handle_no_newer_rhel_kernel_available()

        assert utils.run_subprocess.cmd == ["yum", "install", "-y", "kernel-4.7.2-201.fc24"]

    def test_handle_older_rhel_kernel_not_available(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(
            utils, "run_subprocess", RunSubprocessMocked(return_string=YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE)
        )
        monkeypatch.setattr(pkghandler, "replace_non_rhel_installed_kernel", mock.Mock())

        pkghandler.handle_no_newer_rhel_kernel_available()

        assert pkghandler.replace_non_rhel_installed_kernel.call_count == 1

    def test_handle_older_rhel_kernel_not_available_multiple_installed(self, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(7, 0))
        monkeypatch.setattr(system_info, "releasever", None)
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())
        monkeypatch.setattr(
            utils,
            "run_subprocess",
            RunSubprocessMocked(return_string=YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE_MULTIPLE_INSTALLED),
        )
        monkeypatch.setattr(pkghandler, "remove_pkgs", RemovePkgsMocked())

        pkghandler.handle_no_newer_rhel_kernel_available()

        assert len(pkghandler.remove_pkgs.pkgs) == 1
        assert pkghandler.remove_pkgs.pkgs[0] == "kernel-4.7.4-200.fc24"
        assert utils.run_subprocess.cmd == ["yum", "install", "-y", "kernel-4.7.4-200.fc24"]


class TestReplaceNonRHELInstalledKernel:
    def test_replace_non_rhel_installed_kernel_rhsm_repos(self, monkeypatch):
        monkeypatch.setattr(system_info, "submgr_enabled_repos", ["enabled_rhsm_repo"])
        monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
        monkeypatch.setattr(utils, "download_pkg", DownloadPkgMocked())
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())

        version = "4.7.4-200.fc24"

        pkghandler.replace_non_rhel_installed_kernel(version)

        assert utils.download_pkg.call_count == 1
        assert utils.download_pkg.pkg == "kernel-4.7.4-200.fc24"
        assert utils.download_pkg.enable_repos == ["enabled_rhsm_repo"]
        assert utils.run_subprocess.cmd == [
            "rpm",
            "-i",
            "--force",
            "--nodeps",
            "--replacepkgs",
            "%skernel-4.7.4-200.fc24*" % utils.TMP_DIR,
        ]

    def test_replace_non_rhel_installed_kernel_custom_repos(self, monkeypatch):
        monkeypatch.setattr(system_info, "submgr_enabled_repos", [])
        monkeypatch.setattr(tool_opts, "enablerepo", ["custom_repo"])
        monkeypatch.setattr(tool_opts, "no_rhsm", True)
        monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
        monkeypatch.setattr(utils, "download_pkg", DownloadPkgMocked())
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())

        version = "4.7.4-200.fc24"

        pkghandler.replace_non_rhel_installed_kernel(version)

        assert utils.download_pkg.enable_repos == ["custom_repo"]

    @pytest.mark.parametrize(
        ("download_pkg_return", "subprocess_return_code"),
        (
            (None, 0),
            ("/path/to.rpm", 1),
        ),
        ids=(
            "Unable to download the kernel",
            "",
        ),
    )
    def test_replace_non_rhel_installed_kernel_failing(self, download_pkg_return, subprocess_return_code, monkeypatch):
        monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
        monkeypatch.setattr(utils, "download_pkg", DownloadPkgMocked(return_value=download_pkg_return))
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_code=subprocess_return_code))
        version = "4.7.4-200.fc24"

        with pytest.raises(SystemExit):
            pkghandler.replace_non_rhel_installed_kernel(version)


class TestGetKernel:
    def test_get_kernel(self):
        kernel_version = list(pkghandler.get_kernel(YUM_KERNEL_LIST_OLDER_NOT_AVAILABLE))

        assert kernel_version == ["4.7.4-200.fc24", "4.7.4-200.fc24"]


class TestVerifyRHELKernelInstalled:
    def test_verify_rhel_kernel_installed(self, monkeypatch):
        monkeypatch.setattr(pkghandler, "is_rhel_kernel_installed", lambda: True)

        pkghandler.verify_rhel_kernel_installed()

    def test_verify_rhel_kernel_installed_not_installed(self, monkeypatch):
        monkeypatch.setattr(pkghandler, "is_rhel_kernel_installed", lambda: False)

        with pytest.raises(SystemExit):
            pkghandler.verify_rhel_kernel_installed()


class TestIsRHELKernelInstalled:
    def test_is_rhel_kernel_installed_no(self, monkeypatch):
        monkeypatch.setattr(pkghandler, "get_installed_pkgs_by_fingerprint", lambda x, name: [])

        assert not pkghandler.is_rhel_kernel_installed()

    def test_is_rhel_kernel_installed_yes(self, monkeypatch):
        monkeypatch.setattr(
            pkghandler,
            "get_installed_pkgs_by_fingerprint",
            lambda x, name: ["kernel"],
        )

        assert pkghandler.is_rhel_kernel_installed()


class TestFixInvalidGrub2Entries:
    @pytest.mark.parametrize(
        ("version", "arch"),
        (
            (Version(7, 0), "x86_64"),
            (Version(8, 0), "s390x"),
        ),
    )
    def test_fix_invalid_grub2_entries_not_applicable(self, version, arch, caplog, monkeypatch):
        monkeypatch.setattr(system_info, "version", version)
        monkeypatch.setattr(system_info, "arch", arch)

        pkghandler.fix_invalid_grub2_entries()

        assert not [r for r in caplog.records if r.levelname != "DEBUG"]

    def test_fix_invalid_grub2_entries(self, caplog, monkeypatch):
        monkeypatch.setattr(system_info, "version", Version(8, 0))
        monkeypatch.setattr(system_info, "arch", "x86_64")
        monkeypatch.setattr(
            utils,
            "get_file_content",
            lambda x: "1b11755afe1341d7a86383ca4944c324\n",
        )
        monkeypatch.setattr(
            glob,
            "glob",
            lambda x: [
                "/boot/loader/entries/1b11755afe1341d7a86383ca4944c324-0-rescue.conf",
                "/boot/loader/entries/1b11755afe1341d7a86383ca4944c324-4.18.0-193.28.1.el8_2.x86_64.conf",
                "/boot/loader/entries/b5aebfb91bff486bb9d44ba85e4ae683-0-rescue.conf",
                "/boot/loader/entries/b5aebfb91bff486bb9d44ba85e4ae683-4.18.0-193.el8.x86_64.conf",
                "/boot/loader/entries/b5aebfb91bff486bb9d44ba85e4ae683-5.4.17-2011.7.4.el8uek.x86_64.conf",
            ],
        )
        monkeypatch.setattr(os, "remove", mock.Mock())
        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked())

        pkghandler.fix_invalid_grub2_entries()

        assert os.remove.call_count == 3
        assert utils.run_subprocess.call_count == 2


class TestFixDefaultKernel:
    @pytest.mark.parametrize(
        ("system_name", "version", "old_kernel", "new_kernel", "not_default_kernels"),
        (
            (
                "Oracle Linux Server release 7.9",
                Version(7, 9),
                "kernel-uek",
                "kernel",
                ("kernel-uek", "kernel-core"),
            ),
            (
                "Oracle Linux Server release 8.1",
                Version(8, 1),
                "kernel-uek",
                "kernel-core",
                ("kernel-uek", "kernel"),
            ),
            (
                "CentOS Plus Linux Server release 7.9",
                Version(7, 9),
                "kernel-plus",
                "kernel",
                ("kernel-plus",),
            ),
        ),
    )
    def test_fix_default_kernel_converting_success(
        self, system_name, version, old_kernel, new_kernel, not_default_kernels, caplog, monkeypatch
    ):
        monkeypatch.setattr(system_info, "name", system_name)
        monkeypatch.setattr(system_info, "arch", "x86_64")
        monkeypatch.setattr(system_info, "version", version)
        monkeypatch.setattr(
            utils,
            "get_file_content",
            lambda _: "UPDATEDEFAULT=yes\nDEFAULTKERNEL=%s\n" % old_kernel,
        )
        monkeypatch.setattr(utils, "store_content_to_file", mock.Mock())

        pkghandler.fix_default_kernel()

        warning_msgs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warning_msgs
        assert "Detected leftover boot kernel, changing to RHEL kernel" in warning_msgs[-1].message

        (filename, content), _dummy = utils.store_content_to_file.call_args
        kernel_file_lines = content.splitlines()

        assert "/etc/sysconfig/kernel" == filename
        assert "DEFAULTKERNEL=%s" % new_kernel in kernel_file_lines

        for kernel_name in not_default_kernels:
            assert "DEFAULTKERNEL=%s" % kernel_name not in kernel_file_lines

    def test_fix_default_kernel_with_no_incorrect_kernel(self, caplog, monkeypatch):
        monkeypatch.setattr(system_info, "name", "CentOS Plus Linux Server release 7.9")
        monkeypatch.setattr(system_info, "arch", "x86_64")
        monkeypatch.setattr(system_info, "version", Version(7, 9))
        monkeypatch.setattr(
            utils,
            "get_file_content",
            lambda _: "UPDATEDEFAULT=yes\nDEFAULTKERNEL=kernel\n",
        )
        monkeypatch.setattr(utils, "store_content_to_file", mock.Mock())

        pkghandler.fix_default_kernel()

        info_records = [m for m in caplog.records if m.levelname == "INFO"]
        warning_records = [m for m in caplog.records if m.levelname == "WARNING"]
        debug_records = [m for m in caplog.records if m.levelname == "DEBUG"]

        assert not warning_records
        assert any("Boot kernel validated." in r.message for r in debug_records)

        for record in info_records:
            assert not re.search("Boot kernel [^ ]\\+ was changed to [^ ]\\+", record.message)


class TestRestorablePackageSet:
    @staticmethod
    def fake_download_pkg(pkg, *args, **kwargs):
        pkg_to_filename = {
            "subscription-manager": "subscription-manager-1.0-1.el7.noarch.rpm",
            "python-syspurpose": "python-syspurpose-1.2-2.el7.noarch.rpm",
            "json-c.x86_64": "json-c-0.14-1.el7.x86_64.rpm",
            "json-c.i686": "json-c-0.14-1.el7.i686.rpm",
            "json-c": "json-c-0.14-1.el7.x86_64.rpm",
        }

        rpm_path = os.path.join(pkghandler.SUBMGR_RPMS_DIR, pkg_to_filename[pkg])
        with open(rpm_path, "w"):
            # We just need to create this file
            pass

        return rpm_path

    @staticmethod
    def fake_get_pkg_name_from_rpm(path):
        path = path.rsplit("/", 1)[-1]
        return path.rsplit("-", 2)[0]

    @pytest.fixture
    def package_set(self, monkeypatch, tmpdir):
        pkg_download_dir = tmpdir / "pkg-download-dir"
        yum_repo_dir = tmpdir / "yum-repo.d"
        ubi7_repo_path = yum_repo_dir / "ubi_7.repo"
        ubi8_repo_path = yum_repo_dir / "ubi_8.repo"

        monkeypatch.setattr(pkghandler, "SUBMGR_RPMS_DIR", str(pkg_download_dir))
        monkeypatch.setattr(pkghandler, "_RHSM_TMP_DIR", str(yum_repo_dir))
        monkeypatch.setattr(pkghandler, "_UBI_7_REPO_PATH", str(ubi7_repo_path))
        monkeypatch.setattr(pkghandler, "_UBI_8_REPO_PATH", str(ubi8_repo_path))

        return pkghandler.RestorablePackageSet(["subscription-manager", "python-syspurpose"])

    def smoketest_init(self):
        package_set = pkghandler.RestorablePackageSet(["pkg1"])

        assert package_set.pkg_set == ["pkg1"]
        assert package_set.enabled is False
        # We actually care that this is an empty list and not just False-y
        assert package_set.installed_pkgs == []  # pylint: disable=use-implicit-booleaness-not-comparison

    @pytest.mark.parametrize(
        ("rhel_major_version"),
        (
            (7, 10),
            (8, 5),
        ),
    )
    def test_enable_need_to_install(self, rhel_major_version, package_set, global_system_info, caplog, monkeypatch):
        global_system_info.version = Version(*rhel_major_version)
        monkeypatch.setattr(pkghandler, "system_info", global_system_info)

        monkeypatch.setattr(utils, "download_pkg", self.fake_download_pkg)
        monkeypatch.setattr(pkghandler, "call_yum_cmd", CallYumCmdMocked())
        monkeypatch.setattr(utils, "get_package_name_from_rpm", self.fake_get_pkg_name_from_rpm)

        package_set.pkgs_to_update = ["json-c.x86_64"]

        package_set.enable()

        assert package_set.enabled is True
        assert frozenset(("python-syspurpose", "subscription-manager")) == frozenset(package_set.installed_pkgs)

        assert "\nPackages we installed or updated:\n" in caplog.records[-1].message
        assert "python-syspurpose" in caplog.records[-1].message
        assert "subscription-manager" in caplog.records[-1].message
        assert "json-c" in caplog.records[-1].message
        assert "json-c" not in package_set.installed_pkgs
        assert "json-c.x86_64" not in package_set.installed_pkgs

    def test_enable_call_yum_cmd_fail(self, package_set, global_system_info, caplog, monkeypatch):
        global_system_info.version = Version(7, 0)
        monkeypatch.setattr(pkghandler, "system_info", global_system_info)

        monkeypatch.setattr(
            pkghandler, "get_installed_pkg_information", mock.Mock(side_effect=(["sbscription-manager"], [], []))
        )
        monkeypatch.setattr(utils, "download_pkg", self.fake_download_pkg)

        yum_cmd = CallYumCmdMocked(return_code=1)
        monkeypatch.setattr(pkghandler, "call_yum_cmd", yum_cmd)
        monkeypatch.setattr(utils, "get_package_name_from_rpm", self.fake_get_pkg_name_from_rpm)

        with pytest.raises(SystemExit):
            package_set.enable()

        assert (
            "Failed to install subscription-manager packages. See the above yum output for details."
            in caplog.records[-1].message
        )

    def test_enable_already_enabled(self, package_set, monkeypatch):
        enable_worker_mock = mock.Mock()
        monkeypatch.setattr(pkghandler.RestorablePackageSet, "_enable", enable_worker_mock)
        package_set.enable()
        previous_number_of_calls = enable_worker_mock.call_count
        package_set.enable()

        assert enable_worker_mock.call_count == previous_number_of_calls

    def test_enable_no_packages(self, package_set, caplog, monkeypatch, global_system_info):
        global_system_info.version = Version(8, 0)
        monkeypatch.setattr(pkghandler, "system_info", global_system_info)

        package_set.pkgs_to_install = []
        package_set.pkgs_to_update = ["python-syspurpose", "json-c.x86_64"]

        package_set.enable()

        assert caplog.records[-1].levelname == "INFO"
        assert "All packages were already installed" in caplog.records[-1].message

    def test_restore(self, package_set, monkeypatch):
        mock_remove_pkgs = mock.Mock()
        monkeypatch.setattr(backup, "remove_pkgs", mock_remove_pkgs)
        package_set.enabled = 1
        package_set.installed_pkgs = ["one", "two"]

        package_set.restore()

        assert mock_remove_pkgs.call_count == 1
        assert mock_remove_pkgs.called_with(["one", "two"], backup=False, critical=False)

    def test_restore_jsonc_in_upgrade_pkgs(self, package_set):
        package_set.enabled = 1
        package_set.installed_pkgs = ["subscription-manager", "python-syspurpose", "json-c"]
        package_set.pkgs_to_update = ["json-c.x86_64"]
        remove_pkgs_mock = mock.Mock()

        package_set.restore()

        assert remove_pkgs_mock.called_with(installed_pkgs=["subscription-manager", "python-syspurpose"])

    def test_restore_syspurpose_in_upgrade_pkgs(self, package_set):
        package_set.enabled = 1
        package_set.installed_pkgs = ["subscription-manager", "python-syspurpose", "json-c"]
        package_set.pkgs_to_update = ["python-syspurpose"]
        remove_pkgs_mock = mock.Mock()

        package_set.restore()

        assert remove_pkgs_mock.called_with(installed_pkgs=["subscription-manager"])

    def test_restore_not_enabled(self, package_set, monkeypatch):
        mock_remove_pkgs = mock.Mock()
        monkeypatch.setattr(backup, "remove_pkgs", mock_remove_pkgs)

        package_set.enabled = 1
        package_set.restore()
        previously_called = mock_remove_pkgs.call_count

        package_set.restore()

        assert previously_called >= 1
        assert mock_remove_pkgs.call_count == previously_called


class TestDownloadRHSMPkgs:
    def test_download_rhsm_pkgs(self, monkeypatch, tmpdir):
        """Smoketest that download_rhsm_pkgs works in the happy path"""
        download_rpms_directory = tmpdir.join("submgr-downloads")
        monkeypatch.setattr(pkghandler, "SUBMGR_RPMS_DIR", str(download_rpms_directory))

        monkeypatch.setattr(utils, "store_content_to_file", mock.Mock())
        monkeypatch.setattr(utils, "download_pkgs", DownloadPkgsMocked(str(download_rpms_directory)))

        pkghandler.download_rhsm_pkgs(["testpkg"], "/path/to.repo", "content")

        assert utils.store_content_to_file.call_args == mock.call(filename="/path/to.repo", content="content")
        assert utils.download_pkgs.called == 1

    def test_download_rhsm_pkgs_one_package_failed_to_download(self, monkeypatch):
        """
        Test that download_rhsm_pkgs() aborts when one of the subscription-manager packages fails to download.
        """
        monkeypatch.setattr(utils, "store_content_to_file", StoreContentMocked())
        monkeypatch.setattr(utils, "download_pkgs", DownloadPkgsMocked())

        utils.download_pkgs.to_return.append(None)

        with pytest.raises(SystemExit):
            pkghandler.download_rhsm_pkgs(["testpkg"], "/path/to.repo", "content")


@pytest.mark.parametrize(
    ("version1", "version2", "expected"),
    (
        pytest.param(
            "kernel-core-0:4.18.0-240.10.1.el8_3.i86", "kernel-core-0:4.18.0-240.10.1.el8_3.i86", 0, id="NEVRA"
        ),
        pytest.param("kernel-core-0:123-5.fc35", "kernel-core-0:123-4.fc35", 1, id="NEVR"),
        pytest.param("kernel-core-123-3.fc35.aarch64", "kernel-core-123-4.fc35.aarch64", -1, id="NVRA"),
        pytest.param("kernel-3.10.0-1160.83.1.0.1.el7", "kernel-3.10.0-1160.83.1.el7", 1, id="NVR"),
        pytest.param(
            "kernel-core-0:4.6~pre16262021g84ef6bd9-3.fc35",
            "kernel-core-0:4.6~pre16262021g84ef6bd9-3.fc35",
            0,
            id="NEVR",
        ),
        pytest.param("kernel-core-2:8.2.3568-1.fc35", "kernel-core-2:8.2.3568-1.fc35", 0, id="NEVR"),
        pytest.param(
            "1:NetworkManager-1.18.8-2.0.1.el7_9.aarch64", "1:NetworkManager-1.18.8-1.0.1.el7_9.aarch64", 1, id="ENVRA"
        ),
        pytest.param("1:NetworkManager-1.18.8-2.0.1.el7_9", "1:NetworkManager-1.18.8-3.0.1.el7_9", -1, id="ENVR"),
        pytest.param("NetworkManager-1.18.8-2.0.1.el7_9", "1:NetworkManager-2.18.8-3.0.1.el7_9", -1, id="NVR&ENVR"),
        pytest.param("2:NetworkManager-1.18.8-2.0.1.el7_9", "0:NetworkManager-1.18.8-3.0.1.el7_9", 1, id="ENVR"),
    ),
)
def test_compare_package_versions(version1, version2, expected):
    assert pkghandler.compare_package_versions(version1, version2) == expected


@pytest.mark.parametrize(
    ("version1", "version2", "exception_message"),
    (
        (
            "kernel-core-0:390-287.fc36",
            "kernel-0:390-287.fc36",
            re.escape(
                "The package names ('kernel-core' and 'kernel') do not match. Can only compare versions for the same packages."
            ),
        ),
        (
            "kernel-core-0:390-287.fc36.aarch64",
            "kernel-core-0:391-287.fc36.i86",
            re.escape("The arches ('aarch64' and 'i86') do not match. Can only compare versions for the same arches."),
        ),
    ),
)
def test_compare_package_versions_warnings(version1, version2, exception_message):
    with pytest.raises(ValueError, match=exception_message):
        pkghandler.compare_package_versions(version1, version2)


PACKAGE_FORMATS = (
    pytest.param(
        "kernel-core-0:4.18.0-240.10.1.el8_3.i86", ("kernel-core", "0", "4.18.0", "240.10.1.el8_3", "i86"), id="NEVRA"
    ),
    pytest.param(
        "kernel-core-0:4.18.0-240.10.1.el8_3", ("kernel-core", "0", "4.18.0", "240.10.1.el8_3", None), id="NEVR"
    ),
    pytest.param(
        "1:NetworkManager-1.18.8-2.0.1.el7_9.aarch64",
        ("NetworkManager", "1", "1.18.8", "2.0.1.el7_9", "aarch64"),
        id="ENVRA",
    ),
    pytest.param(
        "1:NetworkManager-1.18.8-2.0.1.el7_9", ("NetworkManager", "1", "1.18.8", "2.0.1.el7_9", None), id="ENVR"
    ),
    pytest.param(
        "NetworkManager-1.18.8-2.0.1.el7_9.aarch64",
        ("NetworkManager", None, "1.18.8", "2.0.1.el7_9", "aarch64"),
        id="NVRA",
    ),
    pytest.param(
        "NetworkManager-1.18.8-2.0.1.el7_9", ("NetworkManager", None, "1.18.8", "2.0.1.el7_9", None), id="NVR"
    ),
    pytest.param(
        "bind-export-libs-32:9.11.4-26.P2.el7_9.13.x86_64",
        ("bind-export-libs", "32", "9.11.4", "26.P2.el7_9.13", "x86_64"),
        id="high epoch number",
    ),
    pytest.param("libgcc-8.5.0-4.el8_5.i686", ("libgcc", None, "8.5.0", "4.el8_5", "i686"), id="i686 package version"),
)


@pytest.mark.skipif(pkgmanager.TYPE == "yum", reason="cannot test dnf backend if dnf is not present")
def test_parse_pkg_string_dnf_called(monkeypatch):
    package = "kernel-core-0:4.18.0-240.10.1.el8_3.i86"
    parse_pkg_with_dnf_mock = mock.Mock(return_value=("kernel-core", "0", "4.18.0", "240.10.1.el8_3", "i86"))
    monkeypatch.setattr(pkghandler, "_parse_pkg_with_dnf", value=parse_pkg_with_dnf_mock)
    pkghandler.parse_pkg_string(package)
    parse_pkg_with_dnf_mock.assert_called_once()


@pytest.mark.skipif(pkgmanager.TYPE == "dnf", reason="cannot test yum backend if yum is not present")
def test_parse_pkg_string_yum_called(monkeypatch):
    package = "kernel-core-0:4.18.0-240.10.1.el8_3.i86"
    parse_pkg_with_yum_mock = mock.Mock(return_value=("kernel-core", "0", "4.18.0", "240.10.1.el8_3", "i86"))
    monkeypatch.setattr(pkghandler, "_parse_pkg_with_yum", value=parse_pkg_with_yum_mock)
    pkghandler.parse_pkg_string(package)
    parse_pkg_with_yum_mock.assert_called_once()


@pytest.mark.skipif(pkgmanager.TYPE == "dnf", reason="cannot test yum backend if yum is not present")
@pytest.mark.parametrize(
    ("package", "expected"),
    (PACKAGE_FORMATS),
)
def test_parse_pkg_with_yum(package, expected):
    assert pkghandler._parse_pkg_with_yum(package) == expected


@pytest.mark.skipif(pkgmanager.TYPE == "yum", reason="cannot test dnf backend if dnf is not present")
@pytest.mark.parametrize(
    ("package", "expected"),
    (PACKAGE_FORMATS),
)
def test_parse_pkg_with_dnf(package, expected):
    assert pkghandler._parse_pkg_with_dnf(package) == expected


@pytest.mark.skipif(pkgmanager.TYPE == "yum", reason="cannot test dnf backend if dnf is not present")
@pytest.mark.parametrize(
    ("package"),
    (
        ("not a valid package"),
        ("centos:0.1.0-34.aarch64"),
        ("name:0-10._12.aarch64"),
        ("kernel:0-10-1-2.aarch64"),
        ("foo-15.x86_64"),
    ),
)
def test_parse_pkg_with_dnf_value_error(package):
    with pytest.raises(ValueError):
        pkghandler._parse_pkg_with_dnf(package)


@pytest.mark.skipif(pkgmanager.TYPE == "dnf", reason="dnf parsing function will raise a different valueError")
@pytest.mark.parametrize(
    ("package", "name", "epoch", "version", "release", "arch", "expected"),
    (
        (
            "Network Manager:0-1.18.8-2.0.1.el7_9.aarch64",
            "Network Manager",
            "1",
            "1.18.8",
            "2.0.1.el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - name : Network Manager"),
        ),
        (
            "NetworkManager:01-1.18.8-2.0.1.el7_9.aarch64",
            "NetworkManager",
            "O1",
            "1.18.8",
            "2.0.1.el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - epoch : O1"),
        ),
        (
            "NetworkManager:1-1.1 8.8-2.0.1.el7_9.aarch64",
            "NetworkManager",
            "1",
            "1.1 8.8",
            "2.0.1.el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - version : 1.1 8.8"),
        ),
        (
            "NetworkManager:1-1.18.8-2.0.1-el7_9.aarch64",
            "NetworkManager",
            "1",
            "1.18.8",
            "2.0.1-el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - release : 2.0.1-el7_9"),
        ),
        (
            "NetworkManager:1-1.18.8-2.0.1.el7_9.aarch65",
            "NetworkManager",
            "1",
            "1.18.8",
            "2.0.1.el7_9",
            "aarch65",
            re.escape("The following field(s) are invalid - arch : aarch65"),
        ),
        (
            "Network Manager:01-1.1 8.8-2.0.1-el7_9.aarch65",
            "Network Manager",
            "O1",
            "1.1 8.8",
            "2.0.1-el7_9",
            "aarch65",
            re.escape(
                "The following field(s) are invalid - name : Network Manager, epoch : O1, version : 1.1 8.8, release : 2.0.1-el7_9, arch : aarch65"
            ),
        ),
        (
            "1-18.8-2.0.1.el7_9.aarch64",
            None,
            "1",
            "1.18.8",
            "2.0.1.el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - name : [None]"),
        ),
        (
            "NetworkManager:1-2.0.1.el7_9.aarch64",
            "NetworkManager",
            "1",
            None,
            "2.0.1.el7_9",
            "aarch64",
            re.escape("The following field(s) are invalid - version : [None]"),
        ),
        (
            "NetworkManager:1-1.18.8.el7_9.aarch64",
            "NetworkManager",
            "1",
            "1.18.8",
            None,
            "aarch64",
            re.escape("The following field(s) are invalid - release : [None]"),
        ),
    ),
)
def test_validate_parsed_fields_invalid(package, name, epoch, version, release, arch, expected):
    with pytest.raises(ValueError, match=expected):
        pkghandler._validate_parsed_fields(package, name, epoch, version, release, arch)


@pytest.mark.skipif(pkgmanager.TYPE == "dnf", reason="dnf parsing function will raise a different valueError")
@pytest.mark.parametrize(
    ("package", "expected"),
    (
        (
            "0:Network Manager-1.1.1-82.aarch64",
            re.escape("The following field(s) are invalid - name : Network Manager"),
        ),
        (
            "foo-15.x86_64",
            re.escape(
                "Invalid package - foo-15.x86_64, packages need to be in one of the following formats: NEVRA, NEVR, NVRA, NVR, ENVRA, ENVR. Reason: The total length of the parsed package fields does not equal the package length,"
            ),
        ),
        (
            "notavalidpackage",
            re.escape(
                "Invalid package - notavalidpackage, packages need to be in one of the following formats: NEVRA, NEVR, NVRA, NVR, ENVRA, ENVR. Reason: The total length of the parsed package fields does not equal the package length,"
            ),
        ),
    ),
)
def test_validate_parsed_fields_invalid_package(package, expected):
    with pytest.raises(ValueError, match=expected):
        pkghandler.parse_pkg_string(package)


@pytest.mark.parametrize(
    ("package"),
    (
        pytest.param("kernel-core-0:4.18.0-240.10.1.el8_3.i86", id="NEVRA"),
        pytest.param("kernel-core-0:4.18.0-240.10.1.el8_3", id="NEVR"),
        pytest.param(
            "1:NetworkManager-1.18.8-2.0.1.el7_9.aarch64",
            id="ENVRA",
        ),
        pytest.param("1:NetworkManager-1.18.8-2.0.1.el7_9", id="ENVR"),
        pytest.param(
            "NetworkManager-1.18.8-2.0.1.el7_9.aarch64",
            id="NVRA",
        ),
        pytest.param("NetworkManager-1.18.8-2.0.1.el7_9", id="NVR"),
    ),
)
def test_validate_parsed_fields_valid(package):
    pkghandler.parse_pkg_string(package)


@pytest.mark.parametrize(
    ("package_manager_type", "packages", "expected", "reposdir"),
    (
        (
            "yum",
            [
                "convert2rhel.noarch-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
                "convert2rhel.src-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
            ],
            frozenset(
                (
                    "convert2rhel.noarch-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
                    "convert2rhel.src-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
                )
            ),
            None,
        ),
        (
            "yum",
            [
                "convert2rhel.noarch-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
                "convert2rhel.noarch-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",
            ],
            frozenset(("convert2rhel.noarch-0.24-1.20211111151554764702.pr356.28.ge9ed160.el8",)),
            None,
        ),
        (
            "dnf",
            [
                "dunst-1.7.1-1.fc35.x86_64",
                "dunst-1.7.0-1.fc35.x86_64",
                "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
            ],
            frozenset(
                (
                    "dunst-1.7.1-1.fc35.x86_64",
                    "dunst-1.7.0-1.fc35.x86_64",
                    "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
                )
            ),
            None,
        ),
        (
            "dnf",
            [
                "dunst-1.7.1-1.fc35.x86_64",
                "dunst-1.7.0-1.fc35.x86_64",
                "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
            ],
            frozenset(
                (
                    "dunst-1.7.1-1.fc35.x86_64",
                    "dunst-1.7.0-1.fc35.x86_64",
                    "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
                )
            ),
            "test/reposdir",
        ),
        (
            "dnf",
            [
                "dunst-1.7.1-1.fc35.x86_64",
                "dunst-1.7.1-1.fc35.x86_64",
                "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
            ],
            frozenset(
                (
                    "dunst-1.7.1-1.fc35.x86_64",
                    "java-11-openjdk-headless-1:11.0.13.0.8-2.fc35.x86_64",
                )
            ),
            "test/reposdir",
        ),
    ),
)
@centos8
def test_get_total_packages_to_update(
    package_manager_type,
    packages,
    expected,
    reposdir,
    pretend_os,
    monkeypatch,
):
    monkeypatch.setattr(pkgmanager, "TYPE", package_manager_type)
    if package_manager_type == "dnf":
        monkeypatch.setattr(
            pkghandler,
            "_get_packages_to_update_%s" % package_manager_type,
            value=lambda reposdir: packages,
        )
    else:
        monkeypatch.setattr(
            pkghandler,
            "_get_packages_to_update_%s" % package_manager_type,
            value=lambda: packages,
        )
    assert get_total_packages_to_update(reposdir=reposdir) == expected


@pytest.mark.skipif(
    pkgmanager.TYPE != "yum",
    reason="No yum module detected on the system, skipping it.",
)
@pytest.mark.parametrize(("packages"), ((["package-1", "package-2", "package-3"],)))
def test_get_packages_to_update_yum(packages, monkeypatch):
    PkgName = namedtuple("PkgNames", ["name"])
    PkgUpdates = namedtuple("PkgUpdates", ["updates"])
    transaction_pkgs = []
    for package in packages:
        transaction_pkgs.append(PkgName(package))

    pkg_lists_mock = mock.Mock(return_value=PkgUpdates(transaction_pkgs))

    monkeypatch.setattr(pkgmanager.YumBase, "doPackageLists", value=pkg_lists_mock)

    assert _get_packages_to_update_yum() == packages


@pytest.mark.skipif(
    pkgmanager.TYPE != "yum",
    reason="No yum module detected on the system, skipping it.",
)
def test_get_packages_to_update_yum_no_more_mirrors(monkeypatch, caplog):
    monkeypatch.setattr(
        pkgmanager.YumBase,
        "doPackageLists",
        mock.Mock(side_effect=pkgmanager.Errors.NoMoreMirrorsRepoError("Failed to connect to repository.")),
    )
    with pytest.raises(pkgmanager.Errors.NoMoreMirrorsRepoError, match="Failed to connect to repository."):
        _get_packages_to_update_yum()


@pytest.mark.skipif(
    pkgmanager.TYPE != "dnf",
    reason="No dnf module detected on the system, skipping it.",
)
@pytest.mark.parametrize(
    ("packages", "reposdir"),
    (
        (
            ["package-1", "package-2", "package-i3"],
            None,
        ),
        (
            ["package-1"],
            "test/reposdir",
        ),
    ),
)
@all_systems
def test_get_packages_to_update_dnf(packages, reposdir, pretend_os, monkeypatch):
    dummy_mock = mock.Mock()
    PkgName = namedtuple("PkgNames", ["name"])
    transaction_pkgs = [PkgName(package) for package in packages]

    monkeypatch.setattr(pkgmanager.Base, "read_all_repos", value=dummy_mock)
    monkeypatch.setattr(pkgmanager.Base, "fill_sack", value=dummy_mock)
    monkeypatch.setattr(pkgmanager.Base, "upgrade_all", value=dummy_mock)
    monkeypatch.setattr(pkgmanager.Base, "resolve", value=dummy_mock)
    monkeypatch.setattr(pkgmanager.Base, "transaction", value=transaction_pkgs)

    assert _get_packages_to_update_dnf(reposdir=reposdir) == packages


class TestInstallGpgKeys:
    data_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "../data/version-independent"))
    gpg_keys_dir = os.path.join(data_dir, "gpg-keys")

    def test_install_gpg_keys(self, monkeypatch, global_backup_control):
        monkeypatch.setattr(utils, "DATA_DIR", self.data_dir)

        # Prevent RestorableRpmKey from actually performing any work
        enable_mock = mock.Mock()
        monkeypatch.setattr(backup.RestorableRpmKey, "enable", enable_mock)

        pkghandler.install_gpg_keys()

        # Get the filenames for every gpg key registered with backup_control
        restorable_keys = set()
        for key in global_backup_control._restorables:
            restorable_keys.add(key.keyfile)

        gpg_file_glob = os.path.join(self.gpg_keys_dir, "*")
        gpg_keys = glob.glob(gpg_file_glob)

        # Make sure we have some keys in the data dir to check
        assert len(gpg_keys) != 0

        # check that all of the keys from data_dir have been registered with the backup_control.
        # We'll test what the restorable keys do in backup_test (for the RestorableKey class)
        assert len(restorable_keys) == len(global_backup_control._restorables)
        for gpg_key in gpg_keys:
            assert gpg_key in restorable_keys

    def test_install_gpg_keys_fail_create_restorable(self, monkeypatch, tmpdir, global_backup_control):
        keys_dir = os.path.join(str(tmpdir), "gpg-keys")
        os.mkdir(keys_dir)
        bad_gpg_key_filename = os.path.join(keys_dir, "bad-key")
        with open(bad_gpg_key_filename, "w") as f:
            f.write("BAD_DATA")

        monkeypatch.setattr(utils, "DATA_DIR", str(tmpdir))

        with pytest.raises(SystemExit, match="Importing the GPG key into rpm failed:\n .*"):
            pkghandler.install_gpg_keys()


@pytest.mark.parametrize(
    ("rpm_paths", "expected"),
    ((["pkg1", "pkg2"], ["pkg1", "pkg2"]),),
)
def test_get_pkg_names_from_rpm_paths(rpm_paths, expected, monkeypatch):
    monkeypatch.setattr(utils, "get_package_name_from_rpm", lambda x: x)
    assert pkghandler.get_pkg_names_from_rpm_paths(rpm_paths) == expected


@pytest.mark.parametrize(
    ("pkgs", "expected"),
    (
        (
            [
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-1",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="not-the-centos7-fingerprint",
                    signature="test",
                )
            ],
            [],
        ),
        (
            [
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-1",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                )
            ],
            ["pkg-1.x86_64"],
        ),
        (
            [
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-1",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                ),
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-2",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                ),
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-3",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                ),
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-4",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                ),
            ],
            ["pkg-1.x86_64", "pkg-2.x86_64", "pkg-3.x86_64", "pkg-4.x86_64"],
        ),
        (
            [
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-1",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="24c6a8a7f4a80eb5",
                    signature="test",
                ),
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="pkg-2",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch="x86_64",
                    fingerprint="this-is-a-fingerprint",
                    signature="test",
                ),
            ],
            ["pkg-1.x86_64"],
        ),
        (
            [
                create_pkg_information(
                    packager="test",
                    vendor="test",
                    name="gpg-pubkey",
                    epoch="0",
                    release="1.0.0",
                    version="1",
                    arch=".(none)",
                    fingerprint="none",
                    signature="(none)",
                )
            ],
            [],
        ),
    ),
)
@centos7
def test_get_system_packages_for_replacement(pretend_os, pkgs, expected, monkeypatch):
    monkeypatch.setattr(pkghandler, "get_installed_pkg_information", value=lambda: pkgs)

    result = pkghandler.get_system_packages_for_replacement()
    assert expected == result


@pytest.mark.skipif(
    pkgmanager.TYPE != "yum",
    reason="No yum module detected on the system, skipping it.",
)
@pytest.mark.parametrize(
    ("name", "version", "release", "arch", "total_pkgs_installed"),
    (
        (None, None, None, None, 1),
        ("installed_pkg", "1", "20.1", "x86_64", 1),
        ("non_existing", None, None, None, 0),  # Special name to return an empty list.
    ),
)
def test_get_installed_pkg_objects_yum(name, version, release, arch, total_pkgs_installed, monkeypatch):
    monkeypatch.setattr(pkgmanager.rpmsack.RPMDBPackageSack, "returnPackages", ReturnPackagesMocked())
    pkgs = pkghandler.get_installed_pkg_objects(name, version, release, arch)

    assert len(pkgs) == total_pkgs_installed
    if total_pkgs_installed > 0:
        assert pkgs[0].name == "installed_pkg"


@pytest.mark.skipif(
    pkgmanager.TYPE != "dnf",
    reason="No dnf module detected on the system, skipping it.",
)
@pytest.mark.parametrize(
    ("name", "version", "release", "arch", "total_pkgs_installed"),
    (
        (None, None, None, None, 1),
        ("installed_pkg", "1", "20.1", "x86_64", 1),
        ("non_existing", None, None, None, 0),
    ),
)
def test_get_installed_pkg_objects_dnf(name, version, release, arch, total_pkgs_installed, monkeypatch):
    monkeypatch.setattr(pkgmanager.query, "Query", QueryMocked())
    pkgs = pkghandler.get_installed_pkg_objects(name, version, release, arch)

    assert len(pkgs) == total_pkgs_installed
    if total_pkgs_installed > 0:
        assert pkgs[0].name == "installed_pkg"


@centos7
def test_get_installed_pkgs_by_fingerprint_correct_fingerprint(pretend_os, monkeypatch):
    package = [
        create_pkg_information(
            packager="test",
            vendor="test",
            name="pkg1",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),  # RHEL
        create_pkg_information(
            packager="test",
            vendor="test",
            name="pkg2",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="72f97b74ec551f03",
            signature="test",
        ),  # OL
        create_pkg_information(
            packager="test",
            vendor="test",
            name="gpg-pubkey",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),
    ]
    monkeypatch.setattr(pkghandler, "get_installed_pkg_information", lambda name: package)
    pkgs_by_fingerprint = pkghandler.get_installed_pkgs_by_fingerprint("199e2f91fd431d51")

    for pkg in pkgs_by_fingerprint:
        assert pkg in ("pkg1.x86_64", "gpg-pubkey.x86_64")


@centos7
def test_get_installed_pkgs_by_fingerprint_incorrect_fingerprint(pretend_os, monkeypatch):
    package = [
        create_pkg_information(
            packager="test",
            vendor="test",
            name="pkg1",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),  # RHEL
        create_pkg_information(
            packager="test",
            vendor="test",
            name="pkg2",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="72f97b74ec551f03",
            signature="test",
        ),  # OL
        create_pkg_information(
            packager="test",
            vendor="test",
            name="gpg-pubkey",
            epoch="0",
            version="1.0.0",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),
    ]
    monkeypatch.setattr(pkghandler, "get_installed_pkg_information", lambda name: package)
    pkgs_by_fingerprint = pkghandler.get_installed_pkgs_by_fingerprint("non-existing fingerprint")

    assert not pkgs_by_fingerprint


@pytest.mark.skipif(
    pkgmanager.TYPE != "yum",
    reason="No yum module detected on the system, skipping it.",
)
@centos7
def test_format_pkg_info_yum(pretend_os, monkeypatch):
    packages = [
        create_pkg_information(
            packager="Oracle",
            vendor="(none)",
            name="pkg1",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),  # RHEL
        create_pkg_information(
            name="pkg2",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="72f97b74ec551f03",
            signature="test",
        ),  # OL
        create_pkg_information(
            name="gpg-pubkey",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),
    ]

    monkeypatch.setattr(
        utils,
        "run_subprocess",
        mock.Mock(
            return_value=(
                """\
C2R 0:pkg1-0.1-1.x86_64&anaconda
C2R 0:pkg2-0.1-1.x86_64&
C2R 0:gpg-pubkey-0.1-1.x86_64&test
    """,
                0,
            )
        ),
    )

    result = pkghandler.format_pkg_info(packages)
    assert re.search(
        r"^Package\s+Vendor/Packager\s+Repository$",
        result,
        re.MULTILINE,
    )
    assert re.search(
        r"^0:pkg1-0\.1-1\.x86_64\s+Oracle\s+anaconda$",
        result,
        re.MULTILINE,
    )
    assert re.search(r"^0:pkg2-0\.1-1\.x86_64\s+N/A\s+N/A$", result, re.MULTILINE)
    assert re.search(
        r"^0:gpg-pubkey-0\.1-1\.x86_64\s+N/A\s+test$",
        result,
        re.MULTILINE,
    )


@pytest.mark.skipif(
    pkgmanager.TYPE != "dnf",
    reason="No dnf module detected on the system, skipping it.",
)
@centos8
def test_format_pkg_info_dnf(pretend_os, monkeypatch):
    packages = [
        create_pkg_information(
            packager="Oracle",
            vendor="(none)",
            name="pkg1",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",  # RHEL
            signature="test",
        ),
        create_pkg_information(
            name="pkg2",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="72f97b74ec551f03",
            signature="test",
        ),  # OL
        create_pkg_information(
            name="gpg-pubkey",
            epoch="0",
            version="0.1",
            release="1",
            arch="x86_64",
            fingerprint="199e2f91fd431d51",
            signature="test",
        ),
    ]

    monkeypatch.setattr(
        utils,
        "run_subprocess",
        mock.Mock(
            return_value=(
                """\
C2R pkg1-0:0.1-1.x86_64&anaconda
C2R pkg2-0:0.1-1.x86_64&@@System
C2R gpg-pubkey-0:0.1-1.x86_64&test
    """,
                0,
            )
        ),
    )

    result = pkghandler.format_pkg_info(packages)

    assert re.search(
        r"^pkg1-0:0\.1-1\.x86_64\s+Oracle\s+anaconda$",
        result,
        re.MULTILINE,
    )
    assert re.search(r"^pkg2-0:0\.1-1\.x86_64\s+N/A\s+@@System$", result, re.MULTILINE)
    assert re.search(
        r"^gpg-pubkey-0:0\.1-1\.x86_64\s+N/A\s+test$",
        result,
        re.MULTILINE,
    )


class GetInstalledPkgObjectsWDiffFingerprintMocked(unit_tests.MockFunction):
    def __call__(self, fingerprints, name=""):
        if name and name != "installed_pkg":
            return []
        if "rhel_fingerprint" in fingerprints:
            pkg_obj = create_pkg_information(
                packager="Oracle", vendor=None, name="installed_pkg", version="0.1", release="1", arch="x86_64"
            )
        else:
            pkg_obj = create_pkg_information(
                packager="Red Hat",
                name="installed_pkg",
                version="0.1",
                release="1",
                arch="x86_64",
            )
        return [pkg_obj]


def testget_packages_to_remove(monkeypatch):
    monkeypatch.setattr(system_info, "fingerprints_rhel", ["rhel_fingerprint"])
    monkeypatch.setattr(
        pkghandler, "get_installed_pkgs_w_different_fingerprint", GetInstalledPkgObjectsWDiffFingerprintMocked()
    )
    original_func = pkghandler.get_packages_to_remove.__wrapped__
    monkeypatch.setattr(pkghandler, "get_packages_to_remove", mock_decorator(original_func))

    result = pkghandler.get_packages_to_remove(["installed_pkg", "not_installed_pkg"])
    assert len(result) == 1
    assert result[0].nevra.name == "installed_pkg"


def test_remove_pkgs_with_confirm(monkeypatch):
    monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
    monkeypatch.setattr(pkghandler, "format_pkg_info", mock.Mock())
    monkeypatch.setattr(pkghandler, "remove_pkgs", RemovePkgsMocked())

    pkghandler.remove_pkgs_unless_from_redhat(
        [
            create_pkg_information(
                packager="Oracle", vendor=None, name="installed_pkg", version="0.1", release="1", arch="x86_64"
            )
        ]
    )

    assert len(pkghandler.remove_pkgs.pkgs) == 1
    assert pkghandler.remove_pkgs.pkgs[0] == "installed_pkg-0.1-1.x86_64"


@pytest.mark.parametrize(
    ("signature", "expected"),
    (
        ("RSA/SHA256, Sun Feb  7 18:35:40 2016, Key ID 73bde98381b46521", "73bde98381b46521"),
        ("RSA/SHA256, Sun Feb  7 18:35:40 2016, teest ID 73bde98381b46521", "none"),
        ("test", "none"),
    ),
)
def test_get_pkg_fingerprint(signature, expected):
    fingerprint = pkghandler._get_pkg_fingerprint(signature)
    assert fingerprint == expected


@pytest.mark.parametrize(
    ("package", "expected"),
    (
        (
            create_pkg_information(
                vendor="Oracle",
            ),
            "Oracle",
        ),
        (
            create_pkg_information(
                packager="Oracle",
            ),
            "N/A",
        ),
    ),
)
def test_get_vendor(package, expected):
    assert pkghandler.get_vendor(package) == expected


@pytest.mark.parametrize(
    ("pkgmanager_name", "package", "include_zero_epoch", "expected"),
    (
        (
            "dnf",
            create_pkg_information(name="pkg", epoch="1", version="2", release="3", arch="x86_64"),
            True,
            "pkg-1:2-3.x86_64",
        ),
        (
            "yum",
            create_pkg_information(name="pkg", epoch="1", version="2", release="3", arch="x86_64"),
            True,
            "1:pkg-2-3.x86_64",
        ),
        (
            "dnf",
            create_pkg_information(name="pkg", epoch="0", version="2", release="3", arch="x86_64"),
            False,
            "pkg-2-3.x86_64",
        ),
        (
            "yum",
            create_pkg_information(name="pkg", epoch="0", version="2", release="3", arch="x86_64"),
            False,
            "pkg-2-3.x86_64",
        ),
        (
            "yum",
            create_pkg_information(name="pkg", epoch="0", version="2", release="3", arch="x86_64"),
            True,
            "0:pkg-2-3.x86_64",
        ),
    ),
)
def test_get_pkg_nevra(pkgmanager_name, package, include_zero_epoch, expected, monkeypatch):
    monkeypatch.setattr(pkgmanager, "TYPE", pkgmanager_name)
    assert pkghandler.get_pkg_nevra(package, include_zero_epoch) == expected


@pytest.mark.parametrize(
    ("fingerprint_orig_os", "expected_count", "expected_pkgs"),
    (
        (["24c6a8a7f4a80eb5", "a963bbdbf533f4fa"], 0, 1),
        (["72f97b74ec551f03"], 0, 0),
    ),
)
def test_get_third_party_pkgs(fingerprint_orig_os, expected_count, expected_pkgs, monkeypatch):
    monkeypatch.setattr(utils, "ask_to_continue", mock.Mock())
    monkeypatch.setattr(pkghandler, "format_pkg_info", PrintPkgInfoMocked())
    monkeypatch.setattr(system_info, "fingerprints_orig_os", fingerprint_orig_os)
    monkeypatch.setattr(pkghandler, "get_installed_pkg_information", unit_tests.GetInstalledPkgsWFingerprintsMocked())

    pkgs = pkghandler.get_third_party_pkgs()

    assert pkghandler.format_pkg_info.called == expected_count
    assert len(pkgs) == expected_pkgs


def test_list_non_red_hat_pkgs_left(monkeypatch):
    monkeypatch.setattr(pkghandler, "format_pkg_info", PrintPkgInfoMocked())
    monkeypatch.setattr(pkghandler, "get_installed_pkg_information", unit_tests.GetInstalledPkgsWFingerprintsMocked())
    pkghandler.list_non_red_hat_pkgs_left()

    assert len(pkghandler.format_pkg_info.pkgs) == 1
    assert pkghandler.format_pkg_info.pkgs[0].nevra.name == "pkg2"


@pytest.mark.parametrize(
    (
        "subprocess_output",
        "is_only_rhel_kernel",
        "expected",
    ),
    (
        ("Package kernel-3.10.0-1127.19.1.el7.x86_64 already installed and latest version", True, False),
        ("Package kernel-3.10.0-1127.19.1.el7.x86_64 already installed and latest version", False, True),
        ("Installed:\nkernel", False, False),
    ),
    ids=(
        "Kernels collide and installed is already RHEL. Do not update.",
        "Kernels collide and installed is not RHEL and older. Update.",
        "Kernels do not collide. Install RHEL kernel and do not update.",
    ),
)
@centos7
def test_install_rhel_kernel(subprocess_output, is_only_rhel_kernel, expected, pretend_os, monkeypatch):
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=subprocess_output))
    monkeypatch.setattr(pkghandler, "handle_no_newer_rhel_kernel_available", mock.Mock())
    monkeypatch.setattr(
        pkghandler, "get_installed_pkgs_w_different_fingerprint", GetInstalledPkgsWDifferentFingerprintMocked()
    )

    pkghandler.get_installed_pkgs_w_different_fingerprint.is_only_rhel_kernel_installed = is_only_rhel_kernel

    update_kernel = pkghandler.install_rhel_kernel()

    assert update_kernel is expected


@pytest.mark.parametrize(
    ("subprocess_output",),
    (
        ("Package kernel-2.6.32-754.33.1.el7.x86_64 already installed and latest version",),
        ("Package kernel-4.18.0-193.el8.x86_64 is already installed.",),
    ),
)
@centos7
def test_install_rhel_kernel_already_installed_regexp(subprocess_output, pretend_os, monkeypatch):
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=subprocess_output))
    monkeypatch.setattr(
        pkghandler, "get_installed_pkgs_w_different_fingerprint", GetInstalledPkgsWDifferentFingerprintMocked()
    )

    pkghandler.install_rhel_kernel()

    assert pkghandler.get_installed_pkgs_w_different_fingerprint.called == 1


def test_remove_non_rhel_kernels(monkeypatch):
    monkeypatch.setattr(
        pkghandler, "get_installed_pkgs_w_different_fingerprint", GetInstalledPkgsWDifferentFingerprintMocked()
    )
    monkeypatch.setattr(pkghandler, "format_pkg_info", mock.Mock())
    monkeypatch.setattr(pkghandler, "remove_pkgs", RemovePkgsMocked())

    removed_pkgs = pkghandler.remove_non_rhel_kernels()

    assert len(removed_pkgs) == 6
    assert [p.nevra.name for p in removed_pkgs] == [
        "kernel",
        "kernel-uek",
        "kernel-headers",
        "kernel-uek-headers",
        "kernel-firmware",
        "kernel-uek-firmware",
    ]


def test_install_additional_rhel_kernel_pkgs(monkeypatch):
    monkeypatch.setattr(
        pkghandler, "get_installed_pkgs_w_different_fingerprint", GetInstalledPkgsWDifferentFingerprintMocked()
    )
    monkeypatch.setattr(pkghandler, "format_pkg_info", mock.Mock())
    monkeypatch.setattr(pkghandler, "remove_pkgs", RemovePkgsMocked())
    monkeypatch.setattr(pkghandler, "call_yum_cmd", CallYumCmdMocked())

    removed_pkgs = pkghandler.remove_non_rhel_kernels()
    pkghandler.install_additional_rhel_kernel_pkgs(removed_pkgs)
    assert pkghandler.call_yum_cmd.call_count == 2


@pytest.mark.parametrize(
    ("package_name", "subprocess_output", "expected", "expected_command"),
    (
        (
            "libgcc*",
            "C2R CentOS Buildsys <bugs@centos.org>&CentOS&libgcc-0:8.5.0-4.el8_5.i686&RSA/SHA256, Fri Nov 12 21:15:26 2021, Key ID 05b555b38483c65d",
            [
                PackageInformation(
                    packager="CentOS Buildsys <bugs@centos.org>",
                    vendor="CentOS",
                    nevra=PackageNevra(
                        name="libgcc",
                        epoch="0",
                        version="8.5.0",
                        release="4.el8_5",
                        arch="i686",
                    ),
                    fingerprint="05b555b38483c65d",
                    signature="RSA/SHA256, Fri Nov 12 21:15:26 2021, Key ID 05b555b38483c65d",
                )
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-qa",
                "libgcc*",
            ],
        ),
        pytest.param(
            "gpg-pubkey",
            "C2R Fedora (37) <fedora-37-primary@fedoraproject.org>&(none)&gpg-pubkey-0:5323552a-6112bcdc.(none)&(none)",
            [
                PackageInformation(
                    packager="Fedora (37) <fedora-37-primary@fedoraproject.org>",
                    vendor="(none)",
                    nevra=PackageNevra(
                        name="gpg-pubkey",
                        epoch="0",
                        version="5323552a",
                        release="6112bcdc",
                        arch=None,
                    ),
                    fingerprint="none",
                    signature="(none)",
                )
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-q",
                "gpg-pubkey",
            ],
            id="gpg-pubkey case with .(none) as arch",
        ),
        (
            "libgcc-0:8.5.0-4.el8_5.i686",
            "C2R CentOS Buildsys <bugs@centos.org>&CentOS&libgcc-0:8.5.0-4.el8_5.i686&RSA/SHA256, Fri Nov 12 21:15:26 2021, Key ID 05b555b38483c65d",
            [
                PackageInformation(
                    packager="CentOS Buildsys <bugs@centos.org>",
                    vendor="CentOS",
                    nevra=PackageNevra(
                        name="libgcc",
                        epoch="0",
                        version="8.5.0",
                        release="4.el8_5",
                        arch="i686",
                    ),
                    fingerprint="05b555b38483c65d",
                    signature="RSA/SHA256, Fri Nov 12 21:15:26 2021, Key ID 05b555b38483c65d",
                )
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-q",
                "libgcc-0:8.5.0-4.el8_5.i686",
            ],
        ),
        (
            "rpmlint-fedora-license-data-0:1.17-1.fc37.noarch",
            "C2R Fedora Project&Fedora Project&rpmlint-fedora-license-data-0:1.17-1.fc37.noarch&RSA/SHA256, Wed 05 Apr 2023 14:27:35 -03, Key ID f55ad3fb5323552a",
            [
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="rpmlint-fedora-license-data", epoch="0", version="1.17", release="1.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Wed 05 Apr 2023 14:27:35 -03, Key ID f55ad3fb5323552a",
                )
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-q",
                "rpmlint-fedora-license-data-0:1.17-1.fc37.noarch",
            ],
        ),
        (
            "rpmlint-fedora-license-data-0:1.17-1.fc37.noarch",
            """
            C2R Fedora Project&Fedora Project&rpmlint-fedora-license-data-0:1.17-1.fc37.noarch&RSA/SHA256, Wed 05 Apr 2023 14:27:35 -03, Key ID f55ad3fb5323552a
            test test what a line
            """,
            [
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="rpmlint-fedora-license-data", epoch="0", version="1.17", release="1.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Wed 05 Apr 2023 14:27:35 -03, Key ID f55ad3fb5323552a",
                )
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-q",
                "rpmlint-fedora-license-data-0:1.17-1.fc37.noarch",
            ],
        ),
        (
            "whatever",
            "random line",
            [],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-q",
                "whatever",
            ],
        ),
        (
            "*",
            """
            C2R Fedora Project&Fedora Project&fonts-filesystem-1:2.0.5-9.fc37.noarch&RSA/SHA256, Tue 23 Aug 2022 08:06:00 -03, Key ID f55ad3fb5323552a
            C2R Fedora Project&Fedora Project&fedora-logos-0:36.0.0-3.fc37.noarch&RSA/SHA256, Thu 21 Jul 2022 02:54:29 -03, Key ID f55ad3fb5323552a
            """,
            [
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="fonts-filesystem", epoch="1", version="2.0.5", release="9.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Tue 23 Aug 2022 08:06:00 -03, Key ID f55ad3fb5323552a",
                ),
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="fedora-logos", epoch="0", version="36.0.0", release="3.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Thu 21 Jul 2022 02:54:29 -03, Key ID f55ad3fb5323552a",
                ),
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-qa",
                "*",
            ],
        ),
        (
            "*",
            """
            C2R Fedora Project&Fedora Project&fonts-filesystem-1:2.0.5-9.fc37.noarch&RSA/SHA256, Tue 23 Aug 2022 08:06:00 -03, Key ID f55ad3fb5323552a
            C2R Fedora Project&Fedora Project&fedora-logos-0:36.0.0-3.fc37.noarch&RSA/SHA256, Thu 21 Jul 2022 02:54:29 -03, Key ID f55ad3fb5323552a
            testest what a line
            """,
            [
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="fonts-filesystem", epoch="1", version="2.0.5", release="9.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Tue 23 Aug 2022 08:06:00 -03, Key ID f55ad3fb5323552a",
                ),
                PackageInformation(
                    packager="Fedora Project",
                    vendor="Fedora Project",
                    nevra=PackageNevra(
                        name="fedora-logos", epoch="0", version="36.0.0", release="3.fc37", arch="noarch"
                    ),
                    fingerprint="f55ad3fb5323552a",
                    signature="RSA/SHA256, Thu 21 Jul 2022 02:54:29 -03, Key ID f55ad3fb5323552a",
                ),
            ],
            [
                "rpm",
                "--qf",
                "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
                "-qa",
                "*",
            ],
        ),
    ),
)
def test_get_installed_pkg_information(package_name, subprocess_output, expected, expected_command, monkeypatch):
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=subprocess_output))

    result = pkghandler.get_installed_pkg_information(package_name)
    assert utils.run_subprocess.cmd == expected_command
    assert result == expected


def test_get_installed_pkg_information_value_error(monkeypatch, caplog):
    output = "C2R Fedora Project&Fedora Project&fonts-filesystem-a:aabb.d.1-l.fc37.noarch&RSA/SHA256, Tue 23 Aug 2022 08:06:00 -03, Key ID f55ad3fb5323552a"
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=output))

    result = pkghandler.get_installed_pkg_information()
    assert not result
    assert "Failed to parse a package" in caplog.records[-1].message


@pytest.mark.parametrize(
    ("packages", "subprocess_output", "expected_result"),
    (
        (
            ["0:eog-44.1-1.fc38.x86_64", "0:gnome-backgrounds-44.0-1.fc38.noarch", "0:gnome-maps-44.1-1.fc38.x86_64"],
            """\
                C2R 0:eog-44.1-1.fc38.x86_64&updates
                C2R 0:gnome-backgrounds-44.0-1.fc38.noarch&fedora
                C2R 0:gnome-maps-44.1-1.fc38.x86_64&updates
            """,
            {
                "0:eog-44.1-1.fc38.x86_64": "updates",
                "0:gnome-backgrounds-44.0-1.fc38.noarch": "fedora",
                "0:gnome-maps-44.1-1.fc38.x86_64": "updates",
            },
        ),
        (
            ["0:eog-44.1-1.fc38.x86_64", "0:gnome-backgrounds-44.0-1.fc38.noarch", "0:gnome-maps-44.1-1.fc38.x86_64"],
            """\
                C2R 0:eog-44.1-1.fc38.x86_64&updates
                C2R 0:gnome-backgrounds-44.0-1.fc38.noarch&fedora
                C2R 0:gnome-maps-44.1-1.fc38.x86_64&updates
                test line without identifier
            """,
            {
                "0:eog-44.1-1.fc38.x86_64": "updates",
                "0:gnome-backgrounds-44.0-1.fc38.noarch": "fedora",
                "0:gnome-maps-44.1-1.fc38.x86_64": "updates",
            },
        ),
        (
            ["0:eog-44.1-1.fc38.x86_64", "0:gnome-backgrounds-44.0-1.fc38.noarch", "0:gnome-maps-44.1-1.fc38.x86_64"],
            """\
                test line without identifier
            """,
            {},
        ),
    ),
)
@centos7
def test_get_package_repositories(pretend_os, packages, subprocess_output, expected_result, monkeypatch, caplog):
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_string=subprocess_output))

    result = pkghandler._get_package_repositories(packages)
    assert expected_result == result
    if caplog.records[-1].message:
        assert "Got a line without the C2R identifier" in caplog.records[-1].message


@centos7
def test_get_package_repositories_repoquery_failure(pretend_os, monkeypatch, caplog):
    monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_code=1, return_string="failed"))

    packages = ["0:gnome-backgrounds-44.0-1.fc38.noarch", "0:eog-44.1-1.fc38.x86_64", "0:gnome-maps-44.1-1.fc38.x86_64"]
    result = pkghandler._get_package_repositories(packages)

    assert "Repoquery exited with return code 1 and with output: failed" in caplog.records[-1].message
    for package in result:
        assert package in packages
        assert result[package] == "N/A"
