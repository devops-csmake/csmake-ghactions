# <copyright>
# (c) Copyright 2025 Autumn Patterson
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# </copyright>
"""csmake extension: adds --gha-workflow to the csmake CLI.

When csmake-ghactions is on PYTHONPATH, csmake's startup discovers this
file via its Extension__*.py scan and imports it.  The import triggers
CliDriver.register_extension(GHActionsExtension) at the bottom of this
module, which registers the --gha-workflow flag before argument parsing
begins.

Usage:
    csmake --gha-workflow=.github/workflows/ci.yml [--command=<job>]

If both --makefile and --gha-workflow are supplied, the one that appears
last on the command line is used and a warning is emitted.
"""
from CsmakeCore.CliDriver import CliDriver


class GHActionsExtension:
    """csmake extension that adds --gha-workflow support."""

    BUILDSPEC_FLAG = 'gha-workflow'

    @classmethod
    def get_settings(cls):
        """Settings dict injected into CliDriver before argument parsing."""
        return {
            'gha-workflow': [
                None,
                'GitHub Actions workflow YAML file to execute '
                '(replaces --makefile when specified last)',
                False,   # not a boolean flag; expects a path value
            ],
        }

    @classmethod
    def load_buildspec(cls, driver):
        """Translate the workflow YAML into a csmake sections dict."""
        path = driver.settings['gha-workflow']
        if not path:
            return None
        from GHActionsLibrary.GHActionsFileReader import read_gha_workflow
        return read_gha_workflow(path)


# Self-register when this module is imported by csmake's extension discovery.
CliDriver.register_extension(GHActionsExtension)
