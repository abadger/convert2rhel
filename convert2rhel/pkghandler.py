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

import glob
import logging
import os
import os.path
import re

from collections import namedtuple

import rpm

from convert2rhel import backup, pkgmanager, utils
from convert2rhel.backup import RestorableFile, remove_pkgs
from convert2rhel.systeminfo import system_info
from convert2rhel.toolopts import tool_opts


loggerinst = logging.getLogger(__name__)

# Limit the number of loops over yum command calls for the case there was
# an error.
MAX_YUM_CMD_CALLS = 3

# Directory to temporarily store rpms to be installed
SUBMGR_RPMS_DIR = os.path.join(utils.DATA_DIR, "subscription-manager")
# Dirctory to temporarily store yum repo configuration to download rhs packages
# from
_RHSM_TMP_DIR = os.path.join(utils.TMP_DIR, "rhsm")

# Configuration of the repository to get Red Hat created packages for RHEL7
# from before we have access to all of RHEL.
_UBI_7_REPO_CONTENT = (
    "[ubi-7-convert2rhel]\n"
    "name=Red Hat Universal Base Image 7 - added by Convert2RHEL\n"
    "baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi/server/7/7Server/$basearch/os/\n"
    "gpgcheck=1\n"
    "enabled=1\n"
)
# Path to the repository file that we store the RHEL7-compatible repo file.
_UBI_7_REPO_PATH = os.path.join(_RHSM_TMP_DIR, "ubi_7.repo")

# Configuration of the repository to get Red Hat created packages for RHEL8
# from before we have access to all of RHEL
# We are using UBI 8 instead of CentOS Linux 8 because there's a bug in subscription-manager-rhsm-certificates on CentOS Linux 8
# https://bugs.centos.org/view.php?id=17907
_UBI_8_REPO_CONTENT = (
    "[ubi-8-baseos-convert2rhel]\n"
    "name=Red Hat Universal Base Image 8 - BaseOS added by Convert2RHEL\n"
    "baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi8/8/$basearch/baseos/os/\n"
    "gpgcheck=1\n"
    "enabled=1\n"
)
# Path to the repository file that we store the RHEL8-compatible repo file.
_UBI_8_REPO_PATH = os.path.join(_RHSM_TMP_DIR, "ubi_8.repo")

_VERSIONLOCK_FILE_PATH = "/etc/yum/pluginconf.d/versionlock.list"  # This file is used by the dnf plugin as well
versionlock_file = RestorableFile(_VERSIONLOCK_FILE_PATH)  # pylint: disable=C0103

#
# Regular expressions used to find package names in yum output
#

# This regex finds if a package is in ENVR/ENVRA format by searching for the epoch field
# being the first set of characters in the package string
ENVRA_ENVR_FORMAT = re.compile(r"^\d+:")

# This regex finds if a package is in NEVRA/NEVR format by searching for any digit
# found between a "-" and a ":"
NEVRA_NEVR_FORMAT = re.compile(r"-\d+:")

# This regex ensures there are no whitespace charcters in the package name
PKG_NAME = re.compile(r"^[^\s]+$")

# This regex ensures package epoch has only 1 or more digits
PKG_EPOCH = re.compile(r"^\d+$")

# This regex ensures there are no whitespace charcters or dashes in the package version and release
PKG_VERSION = re.compile(r"^[^\s-]+$")
PKG_RELEASE = PKG_VERSION

# Set of valid arches
PKG_ARCH = ("x86_64", "s390x", "i686", "i86", "ppc64le", "aarch64", "noarch")

# Namedtuple to represent a package NEVRA.
PackageNevra = namedtuple(
    "PackageNevra",
    (
        "name",
        "epoch",
        "version",
        "release",
        "arch",
    ),
)

# Namedtuple that represents package information, including the NEVRA.
PackageInformation = namedtuple(
    "PackageInformation",
    (
        "packager",
        "vendor",
        "nevra",
        "fingerprint",
        "signature",
    ),
)


class RestorablePackageSet(backup.RestorableChange):
    """Install a set of packages in a way that they can be uninstalled later.

    .. warn:: This functionality is incomplete (These are things that need cleanup)
        Installing and restoring packagesets are very complex. This class needs work before it is
        generic for any set of packages. It currently hardcodes values that are specific to
        downloading subscription-manager and how we do that.

        Implementing it in a half-ready state because we need it to install and remove
        subscription-manager rpms at the right time relative to unregistering the system. As such,
        this class relies heavily on the implementation of downloading and installing
        subscription-manager. To make this generic, some pieces of that will need to move into
        this class:

        * Need a generic way to specify the UBI_X_REPO_PATH and UBI_X_REPO_CONTENT vars. Parameters
          to __init__?  Parameter to install pkgs from "symbolic name" (vendor, pre-rhel, rhel,
          enablerepos)? which we map to specific repo configurations?
        * Backup and restore the vendor versions of packages which are in update_pkgs.
        * Rename the global variables for SUBMGR_RPS_DIR, _RHSM_TMP_DIR, _UBI_7_REPO_CONTENT,
          _UBI_7_REPO_PATH, _UBI_8_REPO_CONTENT, _UBI_8_REPO_PATH to more generic names
        * Rename the helper functions: download_rhsm_pkgs, _log_rhsm_download_directory_contents,
          exit_on_failed_download
        * Is the "We're using distro-sync" comment wrong?  We are using install, not distro-sync and
          git log never shows us using distro-sync.
        * This class is useful for package installation but not package removal. To replace backup.ChangedRPMPackagesController and back.RestorablePackage, we need to implement removal as well.  Should we do that here or in a second class?
          * Note: ChangedRPMPackagesController might still have code that deals with package replacement.  AFAIK, that can be removed entirely.  As of 1.4, there's never a time where we replace rpms and can restore them.  (Installing might upgrade dependencies from the vendor to other vendor packages).
        * Do we need to deal with dependency version issues?  With this code, if an installed dependency is an older version than the subscription-manager package we're installing needs to upgrade and the upgraded version is not present in the vendor's repo, then we will fail.
        * Do we want to filter already installed packages from the package_list in this function
          or leave it to the caller? If we leave it to the caller, then we need to backup vendor supplied previous files here and restore them on rollback. (Currently the
          caller handles this via subscription.needed_subscription_manager_pkgs().  This could cause problems if we need to do extra handling in enable/restore for packages which already exist on the system.)
        * Do we always want to pre-download the rpms and install from a directory of package files
          or do we sometimes want yum to download and install as one step? (Current caller
          doesn't care in subscription.install_rhel_subscription_manager().)
        * Why is system_info.is_rpm_installed() implemented in syste_info? Evaluate if it should be
          moved here.

    .. warn:: Some things that are not handled by this class:
        * Packages installed as a dependency on packages listed here will not be rolled back to the
          system default if we rollback the changes.
    """

    def __init__(self, pkgs_to_install, pkgs_to_update=None):
        self.pkgs_to_install = pkgs_to_install
        self.pkgs_to_update = pkgs_to_update or []
        self.installed_pkgs = []
        self.updated_pkgs = []

        super(RestorablePackageSet, self).__init__()

    def enable(self):
        if self.enabled:
            return

        self._enable()

        super(RestorablePackageSet, self).enable()

    def _enable(self):
        """
        Actually install packages.  Do it in a helper so that we always call super() even if we
        exit early.
        """
        if not self.pkgs_to_install:
            loggerinst.info("All packages were already installed")
            return

        # Note, this use of mkdir_p is secure because SUBMGR_RPMS_DIR and
        # _RHSM_TMP_DIR do not contain any path components writable by
        # a different user.
        utils.mkdir_p(SUBMGR_RPMS_DIR)
        utils.mkdir_p(_RHSM_TMP_DIR)

        loggerinst.info("Downloading requested packages")
        all_pkgs_to_install = self.pkgs_to_install + self.pkgs_to_update

        if system_info.version.major == 7:
            download_rhsm_pkgs(all_pkgs_to_install, _UBI_7_REPO_PATH, _UBI_7_REPO_CONTENT)
        elif system_info.version.major == 8:
            download_rhsm_pkgs(all_pkgs_to_install, _UBI_8_REPO_PATH, _UBI_8_REPO_CONTENT)

        # installing the packages
        rpms_to_install = [os.path.join(SUBMGR_RPMS_DIR, filename) for filename in os.listdir(SUBMGR_RPMS_DIR)]

        loggerinst.info("Installing subscription-manager RPMs.")
        loggerinst.debug("Rpms scheduled to be installed: %s" % utils.format_sequence_as_message(rpms_to_install))

        _, ret_code = call_yum_cmd(
            # We're using distro-sync as there might be various versions of the subscription-manager pkgs installed
            # and we need these packages to be replaced with the provided RPMs from RHEL.
            command="install",
            args=rpms_to_install,
            print_output=True,
            # When installing subscription-manager packages, the RHEL repos are
            # not available yet for getting dependencies so we need to use the repos that are
            # available on the system
            enable_repos=[],
            disable_repos=[],
            # When using the original system repos, we need YUM/DNF to expand the $releasever by itself
            set_releasever=False,
        )
        if ret_code:
            loggerinst.critical(
                "Failed to install subscription-manager packages. See the above yum output for details."
            )

        installed_pkg_names = get_pkg_names_from_rpm_paths(rpms_to_install)
        loggerinst.info(
            "\nPackages we installed or updated:\n%s" % utils.format_sequence_as_message(installed_pkg_names)
        )

        # We could rely on these always being installed/updated when
        # self.enabled is True but putting the values into separate attributes
        # is more friendly if outside code needs to inspect the values.
        # It is tempting to use the rpms we actually installed to populate these
        # but we would have to extract both name and arch information from the
        # rpms if we do that. (for the cornercases where a pkg for one arch is
        # already installed and we have to install a different one.
        self.installed_pkgs = self.pkgs_to_install[:]
        self.updated_pkgs = self.pkgs_to_update[:]

        super(RestorablePackageSet, self).enable()

    def restore(self):
        if not self.enabled:
            return

        loggerinst.task("Convert: Remove installed RHSM packages")
        loggerinst.info("Removing set of installed pkgs: %s" % utils.format_sequence_as_message(self.installed_pkgs))
        backup.remove_pkgs(self.installed_pkgs, backup=False, critical=False)

        super(RestorablePackageSet, self).restore()


def download_rhsm_pkgs(pkgs_to_download, repo_path, repo_content):
    _log_rhsm_download_directory_contents(SUBMGR_RPMS_DIR, "before RHEL rhsm packages download")

    utils.store_content_to_file(filename=repo_path, content=repo_content)
    paths = utils.download_pkgs(pkgs_to_download, dest=SUBMGR_RPMS_DIR, reposdir=_RHSM_TMP_DIR)

    _log_rhsm_download_directory_contents(SUBMGR_RPMS_DIR, "after RHEL rhsm packages download")

    if None in paths:
        loggerinst.critical(
            "Unable to download the subscription-manager package or its dependencies. See details of"
            " the failed yumdownloader call above. These packages are necessary for the conversion"
            " unless you use the --no-rhsm option."
        )


def _log_rhsm_download_directory_contents(directory, when_message):
    pkgs = ["<download directory does not exist>"]
    if os.path.isdir(SUBMGR_RPMS_DIR):
        pkgs = os.listdir(SUBMGR_RPMS_DIR)
    loggerinst.debug(
        "Contents of %s directory %s:\n%s",
        SUBMGR_RPMS_DIR,
        when_message,
        "\n".join(pkgs),
    )


def call_yum_cmd(
    command,
    args=None,
    print_output=True,
    enable_repos=None,
    disable_repos=None,
    set_releasever=True,
):
    """Call yum command and optionally print its output.
    The enable_repos and disable_repos function parameters accept lists and they override the default use of repos,
    which is:
    * --disablerepo yum option = "*" by default OR passed through a CLI option by the user
    * --enablerepo yum option = is the repo enabled through subscription-manager based on a convert2rhel configuration
      file for the particular system OR passed through a CLI option by the user
    YUM/DNF typically expands the $releasever variable used in repofiles. However it fails to do so after we remove the
    release packages (centos-release, oraclelinux-release, etc.) and before the redhat-release package is installed.
    By default, for the above reason, we provide the --releasever option to each yum call. However before we remove the
    release package, we need YUM/DNF to expand the variable by itself (for that, use set_releasever=False).
    """
    if args is None:
        args = []

    cmd = ["yum", command, "-y"]

    # The --disablerepo yum option must be added before --enablerepo,
    #   otherwise the enabled repo gets disabled if --disablerepo="*" is used
    repos_to_disable = []
    if isinstance(disable_repos, list):
        repos_to_disable = disable_repos
    else:
        repos_to_disable = tool_opts.disablerepo

    for repo in repos_to_disable:
        cmd.append("--disablerepo=%s" % repo)

    if set_releasever and system_info.releasever:
        cmd.append("--releasever=%s" % system_info.releasever)

    # Without the release package installed, dnf can't determine the modularity platform ID.
    if system_info.version.major == 8:
        cmd.append("--setopt=module_platform_id=platform:el8")

    repos_to_enable = []
    if isinstance(enable_repos, list):
        repos_to_enable = enable_repos
    else:
        # When using subscription-manager for the conversion, use those repos for the yum call that have been enabled
        # through subscription-manager
        repos_to_enable = system_info.get_enabled_rhel_repos()

    for repo in repos_to_enable:
        cmd.append("--enablerepo=%s" % repo)

    cmd.extend(args)

    stdout, returncode = utils.run_subprocess(cmd, print_output=print_output)
    # handle when yum returns non-zero code when there is nothing to do
    nothing_to_do_error_exists = stdout.endswith("Error: Nothing to do\n")
    if returncode == 1 and nothing_to_do_error_exists:
        loggerinst.debug("Yum has nothing to do. Ignoring.")
        returncode = 0
    return stdout, returncode


def get_installed_pkgs_by_fingerprint(fingerprints, name=""):
    """
    Return list of names of installed packages that are signed by the specific
    OS GPG keys. Fingerprints of the GPG keys are passed as a list in the
    fingerprints parameter.
    The packages can be optionally filtered by name.

    :param fingerprints: Fingerprints to filter packages found
    :type fingerprints: list[str]
    :param name: Name of a package to filter. Defaults to empty string
    :type name: str
    :return: A list of packages with name and arch.
    :rtype: list[str]
    """
    pkgs_w_fingerprints = get_installed_pkg_information(name)

    # We have a problem regarding the package names not being converted and
    # causing duplicate problems if they are both installed on their i686 and
    # x86_64 versions on the system. To fix this, we return the package_name +
    # architecture to make sure both of them will be passed to dnf and, if
    # possible, converted. This issue does not happen on yum, so we can still
    # use only the package name for it.
    return [
        "%s.%s" % (pkg.nevra.name, pkg.nevra.arch) for pkg in pkgs_w_fingerprints if pkg.fingerprint in fingerprints
    ]


def _get_pkg_fingerprint(signature):
    """Get fingerprint of the key used to sign a package."""
    fingerprint_match = re.search("Key ID (.*)", signature)
    return fingerprint_match.group(1) if fingerprint_match else "none"


def get_installed_pkg_information(pkg_name="*"):
    """
    Get information about a package, such as signature from the RPM database,
    packager, vendor, NEVRA and fingerprint.

    :param pkg_name: Full name of a package to check their signature.  If not given, information about all installed packages is returned.
    :type pkg_obj: str
    :return: Return the package signature.
    :rtype: list[PackageInformation]
    """
    cmd = [
        "rpm",
        "--qf",
        "C2R %{PACKAGER}&%{VENDOR}&%{NAME}-%|EPOCH?{%{EPOCH}}:{0}|:%{VERSION}-%{RELEASE}.%{ARCH}&%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|\n",
    ]

    if "*" in pkg_name:
        cmd.extend(["-qa", pkg_name])
    else:
        cmd.extend(["-q", pkg_name])

    output, _ = utils.run_subprocess(cmd, print_cmd=False, print_output=False)

    # Filter out the empty values, u''
    split_output = [value for value in output.split("\n") if value]

    normalized_list = []
    for value in split_output:
        if "C2R" in value:
            try:
                packager, vendor, name, signature = tuple(value.replace("C2R", "").split("&"))
                # Special case for `gpg-pubkeys` package that does not contain
                # an arch set.
                if name.endswith(".(none)"):
                    name = name.rstrip(".(none)")

                name, epoch, version, release, arch = tuple(parse_pkg_string(name))

                # If a package has a signature, then proceed to get the package
                # fingerprint. Otherwise, just set it to None.
                fingerprint = _get_pkg_fingerprint(signature) if signature else None

                normalized_list.append(
                    PackageInformation(
                        packager.strip(),
                        vendor,
                        PackageNevra(name, epoch, version, release, arch),
                        fingerprint,
                        signature,
                    )
                )
            except ValueError as e:
                loggerinst.debug("Failed to parse a package: %s", e)

    return normalized_list


def get_rpm_header(pkg_obj):
    """The dnf python API does not provide the package rpm header:
      https://bugzilla.redhat.com/show_bug.cgi?id=1876606.
    The header is instead fetched directly from the rpm db.
    """
    ts = rpm.TransactionSet()
    rpm_hdr_iter = ts.dbMatch("name", pkg_obj.name)
    for rpm_hdr in rpm_hdr_iter:
        # There might be multiple pkgs with the same name installed.
        if rpm_hdr[rpm.RPMTAG_VERSION] == pkg_obj.v and rpm_hdr[rpm.RPMTAG_RELEASE] == pkg_obj.r:
            # One might think that we could have used the package EVR for comparison, instead of version and release
            #  separately, but there's a bug: https://bugzilla.redhat.com/show_bug.cgi?id=1876885.

            return rpm_hdr
    else:
        # Package not found in the rpm db
        loggerinst.critical("Unable to find package '%s' in the rpm database." % pkg_obj.name)


def get_installed_pkg_objects(name=None, version=None, release=None, arch=None):
    """Return list with installed package objects. The packages can be
    optionally filtered by name.
    """
    if pkgmanager.TYPE == "yum":
        return _get_installed_pkg_objects_yum(name, version, release, arch)

    return _get_installed_pkg_objects_dnf(name, version, release, arch)


def _get_installed_pkg_objects_yum(name=None, version=None, release=None, arch=None):
    yum_base = pkgmanager.YumBase()
    # Disable plugins (when kept enabled yum outputs useless text every call)
    yum_base.doConfigSetup(init_plugins=False)

    if name:
        pattern = name
        if version:
            pattern += "-%s" % version

        if release:
            pattern += "-%s" % release

        if arch:
            pattern += ".%s" % arch

        return yum_base.rpmdb.returnPackages(patterns=[pattern])

    installed_packages = yum_base.rpmdb.returnPackages()
    yum_base.close()
    del yum_base
    return installed_packages


def _get_installed_pkg_objects_dnf(name=None, version=None, release=None, arch=None):
    dnf_base = pkgmanager.Base()
    dnf_base.conf.module_platform_id = "platform:el8"
    dnf_base.fill_sack(load_system_repo=True, load_available_repos=False)
    query = dnf_base.sack.query()
    installed = query.installed()

    if name:
        # Appending the kwargs here dynamically based if they exist or not
        # because the query filter cannot handle properly the situation where
        # any of those parameters are "empty". Basically, dnf thinks that if you
        # specified an empty string in any of those parameters, then it should
        # "match" exactly that, and then to avoid extra logic to play with
        # `__glob`, `__neq` and so on, it's easier to build the `kwargs`
        # dinamycally.
        kwargs = {}

        if version:
            kwargs.update({"version__glob": version})

        if release:
            kwargs.update({"release__glob": release})

        if arch:
            kwargs.update({"arch__glob": arch})

        # name provides "shell-style wildcard match" per
        # https://dnf.readthedocs.io/en/latest/api_queries.html#dnf.query.Query.filter
        installed = installed.filter(name__glob=name, **kwargs)

    return list(installed)


def get_third_party_pkgs():
    """
    Get all the third party packages (non-Red Hat and non-original OS-signed)
    that are going to be kept untouched.
    """
    third_party_pkgs = get_installed_pkgs_w_different_fingerprint(
        system_info.fingerprints_orig_os + system_info.fingerprints_rhel
    )

    return third_party_pkgs


def get_installed_pkgs_w_different_fingerprint(fingerprints, name="*"):
    """Return list of all the packages (yum.rpmsack.RPMInstalledPackage objects in case
    of yum and hawkey.Package objects in case of dnf) that are not signed
    by the specific OS GPG keys. Fingerprints of the GPG keys are passed as a
    list in the fingerprints parameter. The packages can be optionally
    filtered by name.
    """
    # if no fingerprints, skip this check.
    if not fingerprints:
        return []

    pkgs_w_fingerprints = get_installed_pkg_information(name)

    return [
        pkg for pkg in pkgs_w_fingerprints if pkg.fingerprint not in fingerprints and pkg.nevra.name != "gpg-pubkey"
    ]


@utils.run_as_child_process
def format_pkg_info(pkgs):
    """Format package information.

    :param pkgs: List of packages to be formatted
    :type pkgs: list[PackageInformation] | list[RPMInstalledPackage]
    """
    package_info = {}
    for pkg in pkgs:
        nevra = get_pkg_nevra(pkg, include_zero_epoch=True)
        packager = get_vendor(pkg) if pkg.vendor != "(none)" else get_packager(pkg)
        # Setting repoid as N/A to make it default. Later in the function this
        # value is changed to the actual repoid, if there is one.
        package_info[nevra] = {"packager": packager, "repoid": "N/A"}

    # Get packager length
    packager_field_lengths = (len(package["packager"]) for package in package_info.values())
    max_packager_length = max(max(packager_field_lengths), len("Vendor/Packager"))

    # Get nevra length
    max_nvra_length = max(len(nvra) for nvra in package_info)

    header = (
        "%-*s  %-*s  %s"
        % (
            max_nvra_length,
            "Package",
            max_packager_length,
            "Vendor/Packager",
            "Repository",
        )
        + "\n"
    )
    header_underline = (
        "%-*s  %-*s  %s"
        % (
            max_nvra_length,
            "-" * len("Package"),
            max_packager_length,
            "-" * len("Vendor/Packager"),
            "-" * len("Repository"),
        )
        + "\n"
    )

    packages_with_repos = _get_package_repositories(list(package_info))
    # Update package_info reference with repoid
    for nevra, repoid in packages_with_repos.items():
        package_info[nevra]["repoid"] = repoid

    pkg_list = ""
    for package, info in package_info.items():
        pkg_list += (
            "%-*s  %-*s  %s"
            % (
                max_nvra_length,
                package,
                max_packager_length,
                info["packager"],
                info["repoid"],
            )
            + "\n"
        )

    pkg_table = header + header_underline + pkg_list

    return pkg_table


def print_pkg_info(pkgs):
    """
    Print the results of format_pkg_info

    :param pkgs: List of packages to be printed
    :type pkgs: list[PackageInformation] | list[RPMInstalledPackage]
    """

    loggerinst.info(format_pkg_info(pkgs))


def _get_package_repositories(pkgs):
    """Retrieve repository information from packages.

    :param pkgs: List of packages to get their associated repositories
    :type pkgs: list[PackageInformation]
    :return: Mapping of packages with their repositories names
    :rtype: dict[str, dict[str, str]
    """
    repositories_mapping = {}

    query_format = "C2R %{EPOCH}:%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}&%{REPOID}\n"
    if system_info.version.major == 8:
        query_format = "C2R %{NAME}-%{EPOCH}:%{VERSION}-%{RELEASE}.%{ARCH}&%{REPOID}\n"

    output, retcode = utils.run_subprocess(
        ["repoquery", "--quiet", "-q"] + pkgs + ["--qf", query_format],
        print_cmd=False,
        print_output=False,
    )
    output = [line for line in output.split("\n") if line]

    # In case of repoquery returning an retcode different from 0, let's log the
    # output as a debug and return N/A for the caller.
    if retcode != 0:
        loggerinst.debug("Repoquery exited with return code %s and with output: %s", retcode, " ".join(output))
        for package in pkgs:
            repositories_mapping[package] = "N/A"
    else:
        for line in output:
            if "C2R" in line:
                split_output = line.lstrip("C2R ").split("&")
                nevra = split_output[0]
                repoid = split_output[1]
                repositories_mapping[nevra] = repoid if repoid else "N/A"
            else:
                loggerinst.debug("Got a line without the C2R identifier: %s", line)

    return repositories_mapping


def _get_nevra_from_pkg_obj(pkg_obj):
    """
    Helper function to convert from a RPMInstalledPackage object to a
    PackageNevra object.

    If the `pkg_obj` param is already an instance of `PackageInformation`, we
    just return the `nevra` property from it.

    :param pkg_obj: Instance of a RPMInstalledPackage.
    :type pkg_obj: RPMInstalledPackage
    :return: A new instance of PackageNevra if `pkg_obj` is a
        RPMInstalledPackage instance, otherwise, just return the nevra from the
        PackageInformation instance.
    :rtype: PackageNevra
    """
    if isinstance(pkg_obj, PackageInformation):
        return pkg_obj.nevra
    return PackageNevra(
        name=pkg_obj.name,
        epoch=pkg_obj.epoch,
        version=pkg_obj.version,
        release=pkg_obj.release,
        arch=pkg_obj.arch,
    )


def get_pkg_nvra(pkg_obj):
    """
    Get package NVRA as a string: name, version, release, architecture. Some
    utilities don't accept the full NEVRA of a package, for example rpm.

    :param pkg_obj: The package object to extract its NVRA
    :type pkg_obj: RPMInstalledPackage | PackageInformation
    :return: A formatted string with a package NVRA
    :rtype: str
    """
    nevra = _get_nevra_from_pkg_obj(pkg_obj)
    return "%s-%s-%s.%s" % (
        nevra.name,
        nevra.version,
        nevra.release,
        nevra.arch,
    )


def get_pkg_nevra(pkg_obj, include_zero_epoch=False):
    """
    Get package NEVRA as a string: name, epoch, version, release, architecture.
    Epoch is included when it is present. However it's on a different position
    when printed by YUM or DNF.

    Example's::
        YUM - epoch before name: 7:oraclelinux-release-7.9-1.0.9.el7.x86_64
        DNF - epoch before version: oraclelinux-release-8:8.2-1.0.8.el8.x86_64

    :param pkg_obj: The package object to extract its NEVRA
    :type pkg_obj: RPMInstalledPackage | PackageInformation
    :param include_zero_epoch: Whether to include the epoch as 0 in the string.
    :type include_zero_epoch: bool
    :return: A formatted string with a package NEVRA
    :rtype: str
    """
    nevra = _get_nevra_from_pkg_obj(pkg_obj)
    epoch = "" if str(nevra.epoch) == "0" and not include_zero_epoch else str(nevra.epoch) + ":"
    if pkgmanager.TYPE == "yum":
        return "%s%s-%s-%s.%s" % (
            epoch,
            nevra.name,
            nevra.version,
            nevra.release,
            nevra.arch,
        )

    return "%s-%s%s-%s.%s" % (
        nevra.name,
        epoch,
        nevra.version,
        nevra.release,
        nevra.arch,
    )


def get_packager(pkg_obj):
    """Get the package packager from the yum/dnf package object.
    The packager may not be set for all packages. E.g. Oracle Linux packages have the packager info empty.
    """
    packager = pkg_obj.packager if pkg_obj.packager else "N/A"
    # Typical packager format:
    #  Red Hat, Inc. <http://bugzilla.redhat.com/bugzilla>
    #  CentOS Buildsys <bugs@centos.org>
    # Get only the string before the left angle bracket
    return packager.split("<", 1)[0].rstrip()


def get_vendor(pkg_obj):
    """Get the package vendor from the yum/dnf package object.
    The vendor information is provided by the yum/dnf python API on all systems except systems derived from
    RHEL 8.0-8.3 (see bug https://bugzilla.redhat.com/show_bug.cgi?id=1876561).
    """
    return pkg_obj.vendor if pkg_obj.vendor else "N/A"


def list_non_red_hat_pkgs_left():
    """List all the packages that have not been replaced by the
    Red Hat-signed ones during the conversion.
    """
    loggerinst.info("Listing packages not signed by Red Hat")
    non_red_hat_pkgs = get_installed_pkgs_w_different_fingerprint(system_info.fingerprints_rhel)
    if non_red_hat_pkgs:
        loggerinst.info("The following packages were left unchanged.\n")
        print_pkg_info(non_red_hat_pkgs)
    else:
        loggerinst.info("All packages are now signed by Red Hat.")


def remove_pkgs_unless_from_redhat(pkgs_to_remove, backup=True):
    """Remove packages with user confirmation and backup.

    :param pkgs_to_remove: List of packages that will be removed
    :type pkgs_to_remove: list[PackageInformation]
    :param backup: If the package should be in a backup. Defaults to True
    :type backup: bool
    """
    if not pkgs_to_remove:
        loggerinst.info("\nNothing to do.")
        return

    loggerinst.warning("Removing the following %s packages:\n" % str(len(pkgs_to_remove)))
    print_pkg_info(pkgs_to_remove)
    loggerinst.info("\n")
    # We're using the backed up yum repositories to prevent the following:
    # - the system was registered to RHSM prior to the conversion and the system didn't have the redhat.repo generated
    #   for the lack of the RHSM product certificate
    # - at this point convert2rhel has installed the RHSM product cert (e.g. /etc/pki/product-default/69.pem)
    # - this function might be performing the first yum call convert2rhel does after cleaning yum metadata
    # - the "subscription-manager" yum plugin spots that there's a new RHSM product cert and generates
    #   /etc/yum.repos.d/redhat.repo
    # - the suddenly enabled RHEL repos cause a package backup failure
    pkgs_removed = remove_pkgs(get_pkg_nevras(pkgs_to_remove), backup=backup, reposdir=utils.BACKUP_DIR)
    loggerinst.debug("Successfully removed %s packages" % str(len(pkgs_to_remove)))

    return pkgs_removed


def get_pkg_nevras(pkg_objects):
    """Get a list of package NEVRA strings from a list of PackageInformation objects."""
    return [get_pkg_nevra(pkg_obj) for pkg_obj in pkg_objects]


@utils.run_as_child_process
def get_packages_to_remove(pkgs):
    """
    Get packages information that will be removed.

    .. important::
        This function is being executed in a child process to prevent that the
        user won't be able to hit Ctrl + C during the package print, if they
        manage to do that.

        The reason that this function is ran in a child process is that we are
        using an YUM API to query installed package, to then, get the package
        information with the rpm binary. This YUM API method we use calls
        directly the rpmdb, which in its turn, traps the signal handler and
        prevent the main process to handle the Ctrl + C.

    :param pkgs: List of packages that will be removed
    :type pkgs: list[PackageInformation]
    """
    pkgs_to_remove = []
    for pkg in pkgs:
        temp = "." * (50 - len(pkg) - 2)
        pkg_objects = get_installed_pkgs_w_different_fingerprint(system_info.fingerprints_rhel, pkg)
        pkgs_to_remove.extend(pkg_objects)
        loggerinst.info("%s %s %s" % (pkg, temp, str(len(pkg_objects))))

    return pkgs_to_remove


def get_system_packages_for_replacement():
    """
    Get a list of packages in the system to be replaced. This function will
    return a list of packages installed on the system by using the
    `system_info.fingerprint_orig_os` signature.

    :return: A list of packages installed on the system.
    :rtype: list[str]
    """
    fingerprints = system_info.fingerprints_orig_os
    packages_with_fingerprints = get_installed_pkg_information()

    return [
        "%s.%s" % (pkg.nevra.name, pkg.nevra.arch)
        for pkg in packages_with_fingerprints
        if pkg.fingerprint in fingerprints
    ]


def install_gpg_keys():
    """TODO: Add a docstring here."""
    gpg_path = os.path.join(utils.DATA_DIR, "gpg-keys")
    gpg_keys = [os.path.join(gpg_path, key) for key in os.listdir(gpg_path)]
    for gpg_key in gpg_keys:
        try:
            restorable_key = backup.RestorableRpmKey(gpg_key)
            backup.backup_control.push(restorable_key)
        except utils.ImportGPGKeyError as e:
            loggerinst.critical("Importing the GPG key into rpm failed:\n %s" % str(e))

        loggerinst.info("GPG key %s imported successfuly.", gpg_key)


def preserve_only_rhel_kernel():
    kernel_update_needed = install_rhel_kernel()
    verify_rhel_kernel_installed()

    kernel_pkgs_to_install = remove_non_rhel_kernels()
    fix_invalid_grub2_entries()
    fix_default_kernel()

    if kernel_pkgs_to_install:
        install_additional_rhel_kernel_pkgs(kernel_pkgs_to_install)
    if kernel_update_needed:
        update_rhel_kernel()


def install_rhel_kernel():
    """Return boolean indicating whether it's needed to update the kernel
    later on.
    """
    loggerinst.info("Installing RHEL kernel ...")
    output, ret_code = call_yum_cmd(command="install", args=["kernel"])

    if ret_code != 0:
        loggerinst.critical("Error occured while attempting to install the RHEL kernel")

    # Check if kernel with same version is already installed.
    # Example output from yum and dnf:
    #  "Package kernel-4.18.0-193.el8.x86_64 is already installed."
    already_installed = re.search(r" (.*?)(?: is)? already installed", output, re.MULTILINE)
    if already_installed:
        rhel_kernel_nevra = already_installed.group(1)
        non_rhel_kernels = get_installed_pkgs_w_different_fingerprint(system_info.fingerprints_rhel, "kernel")
        for non_rhel_kernel in non_rhel_kernels:
            # We're comparing to NEVRA since that's what yum/dnf prints out
            if rhel_kernel_nevra == get_pkg_nevra(non_rhel_kernel):
                # If the installed kernel is from a third party (non-RHEL) and has the same NEVRA as the one available
                # from RHEL repos, it's necessary to install an older version RHEL kernel and the third party one will
                # be removed later in the conversion process. It's because yum/dnf is unable to reinstall a kernel.
                loggerinst.info(
                    "\nConflict of kernels: One of the installed kernels"
                    " has the same version as the latest RHEL kernel."
                )
                handle_no_newer_rhel_kernel_available()
                return True
    return False


def handle_no_newer_rhel_kernel_available():
    """Handle cases when the installed third party (non-RHEL) kernel has the
    same version as (or newer than) the RHEL one available in the RHEL repo(s).
    """
    installed, available = get_kernel_availability()
    to_install = [kernel for kernel in available if kernel not in installed]

    if not to_install:
        # All the available RHEL kernel versions are already installed
        if len(installed) > 1:
            # There's more than one installed non-RHEL kernel. We'll remove one
            # of them - the one that has the same version as the available RHEL
            # kernel
            older = available[-1]
            remove_pkgs(pkgs_to_remove=["kernel-%s" % older], backup=False)
            call_yum_cmd(command="install", args=["kernel-%s" % older])
        else:
            replace_non_rhel_installed_kernel(installed[0])

        return

    # Install the latest out of the available non-clashing RHEL kernels
    call_yum_cmd(command="install", args=["kernel-%s" % to_install[-1]])


def get_kernel_availability():
    """Return a tuple - a list of installed kernel versions and a list of
    available kernel versions.
    """
    output, _ = call_yum_cmd(command="list", args=["--showduplicates", "kernel"], print_output=False)
    return (list(get_kernel(data)) for data in output.split("Available Packages"))


def get_kernel(kernels_raw):
    for kernel in re.findall(r"kernel.*?\s+(\S+)\s+\S+", kernels_raw, re.MULTILINE):
        yield kernel


def replace_non_rhel_installed_kernel(version):
    """Replace the installed non-RHEL kernel with RHEL kernel with same version."""
    loggerinst.warning(
        "The convert2rhel utlity is going to force-replace the only"
        " kernel installed, which has the same NEVRA as the"
        " only available RHEL kernel. If anything goes wrong"
        " with such replacement, the system will become"
        " unbootable. If you want the convert2rhel utility to install"
        " the RHEL kernel in a safer manner, you can install a"
        " different version of kernel first and then run"
        " convert2rhel again."
    )
    utils.ask_to_continue()

    pkg = "kernel-%s" % version

    # For downloading the RHEL kernel we need to use the RHEL repositories.
    repos_to_enable = system_info.get_enabled_rhel_repos()
    path = utils.download_pkg(
        pkg=pkg,
        dest=utils.TMP_DIR,
        disable_repos=tool_opts.disablerepo,
        enable_repos=repos_to_enable,
    )
    if not path:
        loggerinst.critical("Unable to download the RHEL kernel package.")

    loggerinst.info("Replacing %s %s with RHEL kernel with the same NEVRA ... " % (system_info.name, pkg))
    output, ret_code = utils.run_subprocess(
        # The --nodeps is needed as some kernels depend on system-release (alias for redhat-release) and that package
        # is not installed at this stage.
        [
            "rpm",
            "-i",
            "--force",
            "--nodeps",
            "--replacepkgs",
            "%s*" % os.path.join(utils.TMP_DIR, pkg),
        ],
        print_output=False,
    )
    if ret_code != 0:
        loggerinst.critical("Unable to replace the kernel package: %s" % output)

    loggerinst.info("\nRHEL %s installed.\n" % pkg)


def verify_rhel_kernel_installed():
    loggerinst.info("Verifying that RHEL kernel has been installed")
    if not is_rhel_kernel_installed():
        loggerinst.critical(
            "No RHEL kernel installed. Verify that the repository used for installing kernel contains RHEL packages."
        )
    else:
        loggerinst.info("RHEL kernel has been installed.")


def is_rhel_kernel_installed():
    installed_rhel_kernels = get_installed_pkgs_by_fingerprint(system_info.fingerprints_rhel, name="kernel")
    return len(installed_rhel_kernels) > 0


def remove_non_rhel_kernels():
    loggerinst.info("Searching for non-RHEL kernels ...")
    non_rhel_kernels = get_installed_pkgs_w_different_fingerprint(system_info.fingerprints_rhel, "kernel*")
    if non_rhel_kernels:
        loggerinst.info("Removing non-RHEL kernels\n")
        print_pkg_info(non_rhel_kernels)
        remove_pkgs(
            pkgs_to_remove=[get_pkg_nvra(pkg) for pkg in non_rhel_kernels],
            backup=False,
        )
    else:
        loggerinst.info("None found.")
    return non_rhel_kernels


def fix_default_kernel():
    """
    Systems converted from Oracle Linux or CentOS Linux may have leftover kernel-uek or kernel-plus in
    /etc/sysconfig/kernel as DEFAULTKERNEL.
    This function fixes that by replacing the DEFAULTKERNEL setting from kernel-uek or kernel-plus to kernel for
    RHEL7 and kernel-core for RHEL8
    """
    loggerinst = logging.getLogger(__name__)

    loggerinst.info("Checking for incorrect boot kernel")
    kernel_sys_cfg = utils.get_file_content("/etc/sysconfig/kernel")

    possible_kernels = ["kernel-uek", "kernel-plus"]
    kernel_to_change = next(
        iter(kernel for kernel in possible_kernels if kernel in kernel_sys_cfg),
        None,
    )
    if kernel_to_change:
        loggerinst.warning("Detected leftover boot kernel, changing to RHEL kernel")
        # need to change to "kernel" in rhel7 and "kernel-core" in rhel8
        new_kernel_str = "DEFAULTKERNEL=" + ("kernel" if system_info.version.major == 7 else "kernel-core")

        kernel_sys_cfg = kernel_sys_cfg.replace("DEFAULTKERNEL=" + kernel_to_change, new_kernel_str)
        utils.store_content_to_file("/etc/sysconfig/kernel", kernel_sys_cfg)
        loggerinst.info("Boot kernel %s was changed to %s" % (kernel_to_change, new_kernel_str))
    else:
        loggerinst.debug("Boot kernel validated.")


def fix_invalid_grub2_entries():
    """
    On systems derived from RHEL 8 and later, /etc/machine-id is being used to identify grub2 boot loader entries per
    the Boot Loader Specification.
    However, at the time of executing convert2rhel, the current machine-id can be different from the machine-id from the
    time when the kernels were installed. If that happens:
    - convert2rhel installs the RHEL kernel, but it's not set as default
    - convert2rhel removes the original OS kernels, but for these the boot entries are not removed
    The solution handled by this function is to remove the non-functioning boot entries upon the removal of the original
    OS kernels, and set the RHEL kernel as default.
    """
    if system_info.version.major < 8 or system_info.arch == "s390x":
        # Applicable only on systems derived from RHEL 8 and later, and systems using GRUB2 (s390x uses zipl)
        return

    loggerinst.info("Fixing GRUB boot loader entries.")

    machine_id = utils.get_file_content("/etc/machine-id")
    boot_entries = glob.glob("/boot/loader/entries/*.conf")
    for entry in boot_entries:
        # The boot loader entries in /boot/loader/entries/<machine-id>-<kernel-version>.conf
        if machine_id.strip() not in os.path.basename(entry):
            loggerinst.debug("Removing boot entry %s" % entry)
            os.remove(entry)

    # Removing a boot entry that used to be the default makes grubby to choose a different entry as default, but we will
    # call grub --set-default to set the new default on all the proper places, e.g. for grub2-editenv
    output, ret_code = utils.run_subprocess(["/usr/sbin/grubby", "--default-kernel"], print_output=False)
    if ret_code:
        # Not setting the default entry shouldn't be a deal breaker and the reason to stop the conversions, grub should
        # pick one entry in any case.
        loggerinst.warning("Couldn't get the default GRUB2 boot loader entry:\n%s" % output)
        return
    loggerinst.debug("Setting RHEL kernel %s as the default boot loader entry." % output.strip())
    output, ret_code = utils.run_subprocess(["/usr/sbin/grubby", "--set-default", output.strip()])
    if ret_code:
        loggerinst.warning("Couldn't set the default GRUB2 boot loader entry:\n%s" % output)


def install_additional_rhel_kernel_pkgs(additional_pkgs):
    """Convert2rhel removes all non-RHEL kernel packages, including kernel-tools, kernel-headers, etc. This function
    tries to install back all of these from RHEL repositories.
    """
    # OL renames some of the kernel packages by adding "-uek" (Unbreakable
    # Enterprise Kernel), e.g. kernel-uek-devel instead of kernel-devel. Such
    # package names need to be mapped to the RHEL kernel package names to have
    # them installed on the converted system.
    ol_kernel_ext = "-uek"
    pkg_names = [p.nevra.name.replace(ol_kernel_ext, "", 1) for p in additional_pkgs]
    for name in set(pkg_names):
        if name != "kernel":
            loggerinst.info("Installing RHEL %s" % name)
            call_yum_cmd("install", args=[name])


def update_rhel_kernel():
    """In the corner case where the original system kernel version is the same as the latest available RHEL kernel,
    convert2rhel needs to install older RHEL kernel version first. In this function, RHEL kernel is updated to the
    latest available version.
    """
    loggerinst.info("Updating RHEL kernel.")
    call_yum_cmd(command="update", args=["kernel"])


def clear_versionlock():
    """A package can be locked to a specific version using a YUM/DNF versionlock plugin. Then, even if a newer version
    of a package is available, yum or dnf won't update it. That may cause a problem during the conversion as other
    RHEL packages may depend on a different version than is locked. Therefore, the Convert2RHEL utility clears all the
    locks to prevent a system conversion failure.
    DNF has been designed to be backwards compatible with YUM. So the file in which the version locks are defined for
    YUM works correctly even with DNF thanks to symlinks created by DNF.
    """

    if os.path.isfile(_VERSIONLOCK_FILE_PATH) and os.path.getsize(_VERSIONLOCK_FILE_PATH) > 0:
        loggerinst.warning("YUM/DNF versionlock plugin is in use. It may cause the conversion to fail.")
        loggerinst.info("Upon continuing, we will clear all package version locks.")
        utils.ask_to_continue()

        versionlock_file.backup()

        loggerinst.info("Clearing package versions locks...")
        call_yum_cmd("versionlock", args=["clear"], print_output=False)
    else:
        loggerinst.info("Usage of YUM/DNF versionlock plugin not detected.")


def get_pkg_names_from_rpm_paths(rpm_paths):
    """Return names of packages represented by locally stored rpm packages.
    :param rpm_paths: List of rpm with filepaths.
    :type rpm_paths: list[str]
    :return: A list of package names extracted from the rpm filepath.
    :rtype: list
    """
    pkg_names = []
    for rpm_path in rpm_paths:
        pkg_names.append(utils.get_package_name_from_rpm(rpm_path))
    return pkg_names


@utils.run_as_child_process
def get_total_packages_to_update(reposdir):
    """
    Return the total number of packages to update in the system. It uses both
    yum/dnf depending on whether they are installed on the system, In case of
    RHEL 7 derivative distributions, it uses `yum`, otherwise it uses `dnf`. To
    check whether the system is updated or not, we use original vendor
    repofiles which we ship within the convert2rhel RPM. The reason is that we
    can't rely on the repofiles available on the to-be-converted system.

    .. important::
        This function is being executed in a child process so that yum does
        not handle signals like SIGINT without us knowing about it.

        We need to know about the signals to act on them, for example to
        execute a rollback when the user presses Ctrl + C.

    :param reposdir: The path to the hardcoded repositories for EUS (If any).
    :type reposdir: str | None
    :return: The packages that need to be updated.
    :rtype: list[str]
    """
    packages = []

    if pkgmanager.TYPE == "yum":
        packages = _get_packages_to_update_yum()
    elif pkgmanager.TYPE == "dnf":
        # We're using the reposdir with dnf only because we currently hardcode
        # the repofiles for RHEL 8 derivatives only.
        packages = _get_packages_to_update_dnf(reposdir=reposdir)

    return set(packages)


def _get_packages_to_update_yum():
    """Use yum to get all the installed packages that have an update available.

    :return: Return a list of packages that needs to be updated.
    :rtype: list[str] | list
    """
    all_packages = []
    base = pkgmanager.YumBase()
    packages = base.doPackageLists(pkgnarrow="updates")
    for package in packages.updates:
        all_packages.append(package.name)

    base.close()
    del base
    return all_packages


def _get_packages_to_update_dnf(reposdir):
    """Query all the packages with dnf that has an update pending on the
    system.
    :param reposdir: The path to the hardcoded repositories for EUS (If any).
    :type reposdir: str | None
    """
    packages = []
    base = pkgmanager.Base()

    # If we have a reposdir, that means we are trying to check the packages
    # under CentOS Linux 8.4 or 8.5 and Oracle Linux 8.4. That means we need to
    # use our hardcoded repository files instead of the system ones.
    if reposdir:
        base.conf.reposdir = reposdir

    # Set DNF to read from the proper config files, at this moment, DNF can't
    # automatically read and load the config files so we have to specify it to
    # him. We set the PRIO_MAINCONFIG as the base config file to be read. We
    # also set the folder /etc/dnf/vars as the main point for vars replacement
    # in repo files. See this bugzilla comment:
    # https://bugzilla.redhat.com/show_bug.cgi?id=1920735#c2
    base.conf.read(priority=pkgmanager.conf.PRIO_MAINCONFIG)
    base.conf.substitutions.update_from_etc(installroot=base.conf.installroot, varsdir=base.conf.varsdir)
    base.read_all_repos()
    base.fill_sack()

    # Get a list of all packages to upgrade in the system
    base.upgrade_all()
    base.resolve()

    # Iterate over each and every one of them and append to the packages list
    for package in base.transaction:
        packages.append(package.name)

    return packages


def compare_package_versions(version1, version2):
    """Compare two package versions against each other, including name and arch.
    This function will receive packages in any of the following formats:
        * ENVR
        * ENVRA
        * NVR
        * NEVR
        * NVRA
        * NEVRA
    :param version1: The version to be compared.
    :type version1: str
    :param version2: The version to compare against.
    :type version2: str

    .. example::
        >>> match = compare_package_versions("kernel-core-5.14.10-300.fc35", "kernel-core-5.14.15-300.fc35")
        >>> print(match) # -1

    .. note::
        Since the return type is a int, this could be difficult to understand
        the meaning of each number, so here is a list that represents
        every possible number:
            * -1 if the version1 is less then version2 version
            * 0 if the version1 is equal version2 version
            * 1 if the version1 is greater than version2 version

    :raises ValueError: In case of packages name being different.
    :raises ValueError: In case of architectures being different.

    :return: Return a number indicating if the versions match, are less or greater then.
    :rtype: int
    """
    # call parse_pkg_string to obtain a list containing name, epoch, release, version and arch fields
    # order of fields returned: name, epoch, version, release, arch
    version1_components = parse_pkg_string(version1)
    version2_components = parse_pkg_string(version2)

    # ensure package names match, error if not
    if version1_components[0] != version2_components[0]:
        raise ValueError(
            "The package names ('%s' and '%s') do not match. Can only compare versions for the same packages."
            % (version1_components[0], version2_components[0])
        )

    # ensure package arches match, error if not
    if version1_components[4] != version2_components[4] and all(([version1_components[4]], version2_components[4])):
        raise ValueError(
            "The arches ('%s' and '%s') do not match. Can only compare versions for the same arches."
            % (version1_components[4], version2_components[4])
        )

    # create list containing EVR for comparison
    evr1 = (version1_components[1], version1_components[2], version1_components[3])
    evr2 = (version2_components[1], version2_components[2], version2_components[3])

    return rpm.labelCompare(evr1, evr2)


def parse_pkg_string(pkg):
    """
    This function takes a version string in NEVRA, NEVR, NVRA, NVR, ENVRA, ENVR and decides whether
    to parse with a yum/dnf module based on the package manager type of the system.
    :param pkg: The package to be parsed.
    :type pkg: str
    :return: Return a Return a list containing name, epoch, version, release, arch
    :rtype: list[str | None]
    """
    if pkgmanager.TYPE == "yum":
        pkg_ver_components = _parse_pkg_with_yum(pkg)
    else:
        pkg_ver_components = _parse_pkg_with_dnf(pkg)

    _validate_parsed_fields(pkg, *pkg_ver_components)
    return pkg_ver_components


def _validate_parsed_fields(package, name, epoch, version, release, arch):
    """
    Validation for each field contained in pkg_ver_components from the package
    parsing functions. If one of the fields are invalid then a ValueError is
    raised.

    :param name: unparsed package
    :type name: str
    :param name: parsed package name
    :type name: str
    :param epoch: parsed package epoch
    :type name: str
    :param version: parsed package version
    :type name: str
    :param release: parsed package release
    :type name: str
    :param arch: parsed package arch
    :type name: str

    :raises ValueError: If any of the fields are invalid, raise ValueError.
    """

    errors = []
    pkg_length = len(package)
    seperators = 4

    if name is None or not PKG_NAME.match(name):
        errors.append("name : %s" % name if name else "name : [None]")
    if epoch is not None and not PKG_EPOCH.match(epoch):
        errors.append("epoch : %s" % epoch)
    if version is None or not PKG_VERSION.match(version):
        errors.append("version : %s" % version if version else "version : [None]")
    if release is None or not PKG_RELEASE.match(release):
        errors.append("release : %s" % release if release else "release : [None]")
    if arch is not None and arch not in PKG_ARCH:
        errors.append("arch : %s" % arch)

    if errors:
        raise ValueError("The following field(s) are invalid - %s" % ", ".join(errors))

    pkg_fields = [name, epoch, version, release, arch]
    # this loop determines the number of separators required for each package type. The separators
    # variable starts at 4 since a package with no None fields has 4 seperator characters, 1 None fields
    # means there will be 3 separator characters and 2 None fields means there will be 2 seperator characters

    for field in pkg_fields:
        if field is None:
            seperators -= 1

    # convert None fields to empty strings for concatenation
    pkg_fields = [(i or "") for i in (name, epoch, version, release, arch)]

    # check to see if the package length is equalivalent to the length of parsed fields + separators
    parsed_pkg_length = len("".join(pkg_fields)) + seperators
    if pkg_length != parsed_pkg_length:
        raise ValueError(
            "Invalid package - %s, packages need to be in one of the following formats: NEVRA, NEVR, NVRA, NVR, ENVRA, ENVR."
            " Reason: The total length of the parsed package fields does not equal the package length," % package
        )


def _parse_pkg_with_yum(pkg):
    """Parse verison string using yum and rpmUtils splitFilename
    :param pkg: The package to be parsed.
    :type pkg: str
    :return: Return a list containing name, epoch, version, release, arch (may contain null values)
    :rtype: list[str]
    """

    # package is in NEVRA/NEVR format
    if NEVRA_NEVR_FORMAT.findall(pkg):
        name, epoch_version, release_arch = pkg.rsplit("-", 2)
        epoch, version = epoch_version.split(":", 1)
        # package is in NEVRA format
        if release_arch.endswith(PKG_ARCH):
            release, arch = release_arch.rsplit(".", 1)
        # package is in NEVR format
        else:
            release = release_arch
            arch = None

    # package is in either ENVR, ENVRA, NVR or NVRA
    else:
        # splitFilename doesn't work with packages in NEVRA/NEVR format, that's why we are using it only here.
        (name, version, release, epoch, arch) = pkgmanager.splitFilename(pkg)

        # splitFilename places part of the release in the arch field when no arch is present
        # if arch field is invalid, append contents to release field
        if arch not in PKG_ARCH:
            temp_release = arch
            arch = None
            release = "%s.%s" % (release, temp_release)

    # convert any empty strings to None for consistency
    pkg_ver_components = tuple((i or None) for i in (name, epoch, version, release, arch))

    return pkg_ver_components


def _parse_pkg_with_dnf(pkg):
    """
    Parse verison string using hawkey and dnf.
    :param pkg: The package to be parsed.
    :type pkg: str
    :return: Return a list containing name, epoch, version, release, arch (may contain null values)
    :rtype: list[str]

    :raises ValueError: If any of the fields are invalid, raise ValueError.
    """

    name = epoch = version = release = arch = None
    no_arch_data = None

    # if format is ENVR/ENVRA, store and remove epoch and evaluate as NVR/NVRA
    if ENVRA_ENVR_FORMAT.findall(pkg):
        pkg = pkg.split(":")
        epoch = pkg[0]
        pkg = pkg[1]

    # returns generator for every possible nevra
    subject = pkgmanager.dnf.subject.Subject(pkg)
    possible_nevra = subject.get_nevra_possibilities(forms=[pkgmanager.hawkey.FORM_NEVRA, pkgmanager.hawkey.FORM_NEVR])

    # loop through each possible set of nevra fields and select the valid one
    for nevra in possible_nevra:

        # current arch is valid
        if str(nevra.arch) in PKG_ARCH:
            name = nevra.name
            epoch = epoch or nevra.epoch
            version = nevra.version
            release = nevra.release
            arch = nevra.arch
            break

        # there is no arch present and current arch is None
        if nevra.arch is None:
            no_arch_data = nevra

        # arch is not valid, move on to next iteration

    else:  # This else goes with the for loop

        # if no_arch_data is still None by this point, the parser wasn't able to find valid fields
        # therefore the package entered is invalid and/or in the wrong format
        if no_arch_data is None:
            raise ValueError(
                "Invalid package - %s, packages need to be in one of the following"
                " formats: NEVRA, NEVR, NVRA, NVR, ENVRA, ENVR." % pkg
            )

        name = no_arch_data.name
        epoch = epoch or no_arch_data.epoch
        version = no_arch_data.version
        release = no_arch_data.release
        arch = no_arch_data.arch

    # convert epoch from integer to string for consistency
    if epoch is not None:
        epoch = str(epoch)

    pkg_ver_components = (name, epoch, version, release, arch)
    return pkg_ver_components
