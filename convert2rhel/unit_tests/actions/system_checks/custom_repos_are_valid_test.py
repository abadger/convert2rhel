# -*- coding: utf-8 -*-
#
# Copyright(C) 2023 Red Hat, Inc.
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

import pytest

from convert2rhel import actions, unit_tests
from convert2rhel.actions.system_checks import custom_repos_are_valid


@pytest.fixture
def custom_repos_are_valid_action():
    return custom_repos_are_valid.CustomReposAreValid()


class CallYumCmdMocked(unit_tests.MockFunction):
    def __init__(self, ret_code, ret_string):
        self.called = 0
        self.return_code = ret_code
        self.return_string = ret_string
        self.fail_once = False
        self.command = None

    def __call__(self, command, *args, **kwargs):
        if self.fail_once and self.called == 0:
            self.return_code = 1
        if self.fail_once and self.called > 0:
            self.return_code = 0
        self.called += 1
        self.command = command
        return self.return_string, self.return_code


def test_custom_repos_are_valid(custom_repos_are_valid_action, monkeypatch, caplog):
    monkeypatch.setattr(
        custom_repos_are_valid,
        "call_yum_cmd",
        CallYumCmdMocked(ret_code=0, ret_string="Abcdef"),
    )
    monkeypatch.setattr(custom_repos_are_valid.tool_opts, "no_rhsm", True)

    custom_repos_are_valid_action.run()

    assert "The repositories passed through the --enablerepo option are all accessible." in caplog.text


def test_custom_repos_are_invalid(custom_repos_are_valid_action, monkeypatch):
    monkeypatch.setattr(
        custom_repos_are_valid,
        "call_yum_cmd",
        CallYumCmdMocked(ret_code=1, ret_string="YUM/DNF failed"),
    )
    monkeypatch.setattr(custom_repos_are_valid.tool_opts, "no_rhsm", True)

    custom_repos_are_valid_action.run()

    unit_tests.assert_actions_result(
        custom_repos_are_valid_action,
        level="ERROR",
        id="UNABLE_TO_ACCESS_REPOSITORIES",
        title="Unable to access repositories",
        description="Access could not be made to the custom repositories.",
        diagnosis="Unable to access the repositories passed through the --enablerepo option.",
        remediation="For more details, see YUM/DNF output:\nYUM/DNF failed",
    )


def test_custom_repos_are_valid_skip(custom_repos_are_valid_action, monkeypatch):
    monkeypatch.setattr(custom_repos_are_valid.tool_opts, "no_rhsm", False)

    custom_repos_are_valid_action.run()

    expected = set(
        (
            actions.ActionMessage(
                level="INFO",
                id="CUSTOM_REPOSITORIES_ARE_VALID_CHECK_SKIP",
                title="Skipping the custom repos are valid check",
                description="Skipping the check of repositories due to the use of RHSM for the conversion.",
                diagnosis="",
                remediation="",
            ),
        )
    )
    assert expected.issuperset(custom_repos_are_valid_action.messages)
    assert expected.issubset(custom_repos_are_valid_action.messages)
