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

import json
import os.path
import re

import pytest

from convert2rhel.actions import STATUS_CODE, report
from convert2rhel.logger import bcolors


#: _LONG_MESSAGE since we do line wrapping
_LONG_MESSAGE = {
    "title": "Will Robinson! Will Robinson!",
    "description": " Danger Will Robinson...!",
    "diagnosis": " Danger! Danger! Danger!",
    "remediation": " Please report directly to your parents in the spaceship immediately.",
    "variables": {},
}


@pytest.mark.parametrize(
    ("results", "expected"),
    (
        (
            {
                "CONVERT2RHEL_LATEST_VERSION": {
                    "result": dict(level=STATUS_CODE["SUCCESS"], id="SUCCESS"),
                    "messages": [
                        dict(
                            level=STATUS_CODE["WARNING"],
                            id="WARNING_ONE",
                            title="A warning message",
                            description="",
                            diagnosis="",
                            remediation="",
                        ),
                    ],
                },
            },
            {
                "format_version": "1.0",
                "actions": {
                    "CONVERT2RHEL_LATEST_VERSION": {
                        "result": dict(level="SUCCESS", id="SUCCESS"),
                        "messages": [
                            dict(
                                level="WARNING",
                                id="WARNING_ONE",
                                title="A warning message",
                                description="",
                                diagnosis="",
                                remediation="",
                            ),
                        ],
                    },
                },
            },
        ),
        (
            {
                "CONVERT2RHEL_LATEST_VERSION": {
                    "result": dict(level=STATUS_CODE["SUCCESS"], id="SUCCESS"),
                    "messages": [
                        dict(
                            level=STATUS_CODE["WARNING"],
                            id="WARNING_ONE",
                            title="A warning message",
                            description="A description",
                            diagnosis="A diagnosis",
                            remediation="A remediation",
                        ),
                    ],
                },
            },
            {
                "format_version": "1.0",
                "actions": {
                    "CONVERT2RHEL_LATEST_VERSION": {
                        "result": dict(level="SUCCESS", id="SUCCESS"),
                        "messages": [
                            dict(
                                level="WARNING",
                                id="WARNING_ONE",
                                title="A warning message",
                                description="A description",
                                diagnosis="A diagnosis",
                                remediation="A remediation",
                            ),
                        ],
                    },
                },
            },
        ),
    ),
)
def test_summary_as_json(results, expected, tmpdir):
    """Test that the results that we're given are what is written to the json log file."""
    json_report_file = os.path.join(str(tmpdir), "c2r-assessment.json")

    report.summary_as_json(results, json_report_file)

    with open(json_report_file, "r") as f:
        file_contents = json.load(f)

    assert file_contents == expected


@pytest.mark.parametrize(
    ("results", "include_all_reports", "expected_results"),
    (
        # Test that all messages are being used with the `include_all_reports`
        # parameter.
        (
            {
                "PreSubscription": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                )
            },
            True,
            [
                "(WARNING) PreSubscription::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(SUCCESS) PreSubscription::SUCCESS - [No further information given]",
            ],
        ),
        (
            {
                "PreSubscription": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                ),
                "PreSubscription2": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIPPED",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            True,
            [
                "(SUCCESS) PreSubscription::SUCCESS - [No further information given]",
                "(WARNING) PreSubscription2::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(SKIP) PreSubscription2::SKIPPED - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
            ],
        ),
        # Test that messages that are below WARNING will not appear in
        # the logs.
        (
            {
                "PreSubscription": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                )
            },
            False,
            ["No problems detected during the analysis!"],
        ),
        (
            {
                "PreSubscription": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                )
            },
            False,
            [
                "(WARNING) PreSubscription::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on"
            ],
        ),
        (
            {
                "PreSubscription": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                ),
                "PreSubscription2": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIPPED",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                "(SKIP) PreSubscription2::SKIPPED - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) PreSubscription::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) PreSubscription2::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
            ],
        ),
        # Test all messages are displayed, SKIP and higher
        (
            {
                "PreSubscription1": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIPPED",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "PreSubscription2": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE_ID",
                        "title": "Overridable",
                        "description": "Action overridable",
                        "diagnosis": "User overridable",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                "(OVERRIDABLE) PreSubscription2::OVERRIDABLE_ID - Overridable\n     Description: Action overridable\n     Diagnosis: User overridable\n     Remediation: move on",
                "(SKIP) PreSubscription1::SKIPPED - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) PreSubscription1::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) PreSubscription2::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
            ],
        ),
        (
            {
                "SkipAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "OverridableAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action overridable",
                        "diagnosis": "User overridable",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "ErrorAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "TestAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "SECONDERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                "(ERROR) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                "(ERROR) TestAction::SECONDERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                "(OVERRIDABLE) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action overridable\n     Diagnosis: User overridable\n     Remediation: move on",
                "(SKIP) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) SkipAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) OverridableAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) ErrorAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) TestAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
            ],
        ),
    ),
)
def test_summary(results, expected_results, include_all_reports, caplog):
    report.summary(results, include_all_reports, with_colors=False)

    for expected in expected_results:
        assert expected in caplog.records[-1].message


@pytest.mark.parametrize(
    ("long_message"),
    (
        (_LONG_MESSAGE),
        (
            {
                "title": "Will Robinson! Will Robinson!",
                "description": " Danger Will Robinson...!" * 8,
                "diagnosis": " Danger!" * 15,
                "remediation": " Please report directly to your parents in the spaceship immediately." * 2,
                "variables": {},
            }
        ),
    ),
)
def test_results_summary_with_long_message(long_message, caplog):
    """Test a long message because we word wrap those."""
    result = {"level": STATUS_CODE["ERROR"], "id": "ERROR"}
    result.update(long_message)
    report.summary(
        {
            "ErrorAction": dict(
                messages=[],
                result=result,
            )
        },
        with_colors=False,
    )

    # Word wrapping might break on any spaces so we need to substitute
    # a pattern for those
    pattern = long_message["title"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["description"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["diagnosis"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["remediation"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)


@pytest.mark.parametrize(
    ("long_message"),
    (
        (_LONG_MESSAGE),
        (
            {
                "title": "Will Robinson! Will Robinson!",
                "description": " Danger Will Robinson...!" * 8,
                "diagnosis": " Danger!" * 15,
                "remediation": " Please report directly to your parents in the spaceship immediately." * 2,
                "variables": {},
            }
        ),
    ),
)
def test_messages_summary_with_long_message(long_message, caplog):
    """Test a long message because we word wrap those."""
    messages = {"level": STATUS_CODE["WARNING"], "id": "WARNING_ID"}
    messages.update(long_message)
    report.summary(
        {
            "ErrorAction": dict(
                messages=[messages],
                result={
                    "level": STATUS_CODE["SUCCESS"],
                    "id": "",
                    "title": "",
                    "description": "",
                    "diagnosis": "",
                    "remediation": "",
                    "variables": {},
                },
            )
        },
        with_colors=False,
    )

    # Word wrapping might break on any spaces so we need to substitute
    # a pattern for those
    pattern = long_message["title"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["description"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["diagnosis"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)

    pattern = long_message["remediation"].replace(" ", "[ \t\n]+")
    assert re.search(pattern, caplog.records[-1].message)


@pytest.mark.parametrize(
    ("results", "include_all_reports", "expected_results"),
    (
        # Test all messages are displayed, SKIP and higher
        (
            {
                "PreSubscription2": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIPPED",
                        "title": "Skipped",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "PreSubscription1": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "SOME_OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action override",
                        "diagnosis": "User override",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                r"\(OVERRIDABLE\) PreSubscription1::SOME_OVERRIDABLE - Overridable\n     Description: Action override\n     Diagnosis: User override\n     Remediation: move on",
                r"\(SKIP\) PreSubscription2::SKIPPED - Skipped\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
            ],
        ),
        (
            {
                "SkipAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "OverridableAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action override",
                        "diagnosis": "User override",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "ErrorAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                r"\(ERROR\) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                r"\(OVERRIDABLE\) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action override\n     Diagnosis: User override\n     Remediation: move on",
                r"\(SKIP\) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
            ],
        ),
        # Message order with `include_all_reports` set to True.
        (
            {
                "PreSubscription": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                ),
                "SkipAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "OverridableAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action override",
                        "diagnosis": "User override",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "ErrorAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            True,
            [
                r"\(ERROR\) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                r"\(OVERRIDABLE\) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action override\n     Diagnosis: User override\n     Remediation: move on",
                r"\(SKIP\) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                r"\(SUCCESS\) PreSubscription::SUCCESS - \[No further information given\]",
            ],
        ),
    ),
)
def test_results_summary_ordering(results, include_all_reports, expected_results, caplog):

    report.summary(results, include_all_reports, with_colors=False)

    # Prove that all the messages occurred and in the right order.
    message = caplog.records[-1].message

    pattern = []
    for entry in expected_results:
        pattern.append(entry)
    pattern = ".*".join(pattern)

    assert re.search(pattern, message, re.DOTALL | re.MULTILINE)


@pytest.mark.parametrize(
    ("results", "include_all_reports", "expected_results"),
    (
        # Test all messages are displayed, SKIP and higher
        (
            {
                "PreSubscription2": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIPPED",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "PreSubscription1": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "SOME_OVERRIDABLE",
                        "title": "Override",
                        "description": "Action override",
                        "diagnosis": "User override",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                "(OVERRIDABLE) PreSubscription1::SOME_OVERRIDABLE - Override\n     Description: Action override\n     Diagnosis: User override\n     Remediation: move on",
                "(SKIP) PreSubscription2::SKIPPED - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) PreSubscription2::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
            ],
        ),
        (
            {
                "SkipAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "OverridableAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action overridable",
                        "diagnosis": "User overridable",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "ErrorAction": dict(
                    messages=[],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            False,
            [
                "(ERROR) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                "(OVERRIDABLE) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action overridable\n     Diagnosis: User overridable\n     Remediation: move on",
                "(SKIP) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) SkipAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) OverridableAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
            ],
        ),
        # Message order with `include_all_reports` set to True.
        (
            {
                "PreSubscription": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                ),
                "SkipAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "OverridableAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action overridable",
                        "diagnosis": "User overridable",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
                "ErrorAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                ),
            },
            True,
            [
                "(ERROR) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on",
                "(OVERRIDABLE) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action overridable\n     Diagnosis: User overridable\n     Remediation: move on",
                "(SKIP) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on",
                "(WARNING) PreSubscription::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) SkipAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) OverridableAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(WARNING) ErrorAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on",
                "(SUCCESS) PreSubscription::SUCCESS - [No further information given]",
            ],
        ),
    ),
)
def test_messages_summary_ordering(results, include_all_reports, expected_results, caplog):

    report.summary(results, include_all_reports, with_colors=False)

    # Filter informational messages and empty strings out of message.splitlines
    caplog_messages = []
    for message in caplog.records[1].message.splitlines():
        if not message.startswith("==========") and not message == "":
            caplog_messages.append(message)

    # Prove that all the messages occurred
    for expected in expected_results:
        message = "\n".join(caplog_messages)
        assert expected in message


@pytest.mark.parametrize(
    ("results", "expected_result", "expected_message"),
    (
        (
            {
                "ErrorAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["ERROR"],
                        "id": "ERROR",
                        "title": "Error",
                        "description": "Action error",
                        "diagnosis": "User error",
                        "remediation": "move on",
                        "variables": {},
                    },
                )
            },
            "{begin}(ERROR) ErrorAction::ERROR - Error\n     Description: Action error\n     Diagnosis: User error\n     Remediation: move on{end}".format(
                begin=bcolors.FAIL, end=bcolors.ENDC
            ),
            "{begin}(WARNING) ErrorAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on{end}".format(
                begin=bcolors.WARNING, end=bcolors.ENDC
            ),
        ),
        (
            {
                "OverridableAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["OVERRIDABLE"],
                        "id": "OVERRIDABLE",
                        "title": "Overridable",
                        "description": "Action overridable",
                        "diagnosis": "User overridable",
                        "remediation": "move on",
                        "variables": {},
                    },
                )
            },
            "{begin}(OVERRIDABLE) OverridableAction::OVERRIDABLE - Overridable\n     Description: Action overridable\n     Diagnosis: User overridable\n     Remediation: move on{end}".format(
                begin=bcolors.FAIL, end=bcolors.ENDC
            ),
            "{begin}(WARNING) OverridableAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on{end}".format(
                begin=bcolors.WARNING, end=bcolors.ENDC
            ),
        ),
        (
            {
                "SkipAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SKIP"],
                        "id": "SKIP",
                        "title": "Skip",
                        "description": "Action skip",
                        "diagnosis": "User skip",
                        "remediation": "move on",
                        "variables": {},
                    },
                )
            },
            "{begin}(SKIP) SkipAction::SKIP - Skip\n     Description: Action skip\n     Diagnosis: User skip\n     Remediation: move on{end}".format(
                begin=bcolors.FAIL, end=bcolors.ENDC
            ),
            "{begin}(WARNING) SkipAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on{end}".format(
                begin=bcolors.WARNING, end=bcolors.ENDC
            ),
        ),
        (
            {
                "SuccessfulAction": dict(
                    messages=[
                        {
                            "level": STATUS_CODE["WARNING"],
                            "id": "WARNING_ID",
                            "title": "Warning",
                            "description": "Action warning",
                            "diagnosis": "User warning",
                            "remediation": "move on",
                            "variables": {},
                        }
                    ],
                    result={
                        "level": STATUS_CODE["SUCCESS"],
                        "id": "SUCCESS",
                        "title": "",
                        "description": "",
                        "diagnosis": "",
                        "remediation": "",
                        "variables": {},
                    },
                )
            },
            "{begin}(SUCCESS) SuccessfulAction::SUCCESS - [No further information given]{end}".format(
                begin=bcolors.OKGREEN, end=bcolors.ENDC
            ),
            "{begin}(WARNING) SuccessfulAction::WARNING_ID - Warning\n     Description: Action warning\n     Diagnosis: User warning\n     Remediation: move on{end}".format(
                begin=bcolors.WARNING, end=bcolors.ENDC
            ),
        ),
    ),
)
def test_summary_colors(results, expected_result, expected_message, caplog):
    report.summary(results, include_all_reports=True, with_colors=True)
    assert expected_result in caplog.records[-1].message
    assert expected_message in caplog.records[-1].message
