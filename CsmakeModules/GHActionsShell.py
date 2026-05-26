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
import os
import re
import subprocess
import tempfile

from CsmakeModules.Shell import Shell

_EXPR_RE = re.compile(r'\$\{\{\s*(.*?)\s*\}\}')

_SHELL_EXEC = {
    'bash':    '/bin/bash',
    'sh':      '/bin/sh',
    'pwsh':    'pwsh',
    'python':  'python',
    'python3': 'python3',
}


class GHActionsShell(Shell):
    """Purpose: Execute a GitHub Actions 'run:' step locally
       Type: Module   Library: csmake-ghactions
       Phases: *any*
       Options:
           --script    - the shell script to execute (may be multi-line)
           --shell     - shell to use (default: bash)
           --name      - display name for the step
           --if        - (OPTIONAL) GHA if: condition; step is skipped if
                         the condition evaluates to false
           --step-id   - (OPTIONAL) step id; outputs are stored in the csmake
                         environment as _gha_steps_<step_id>_outputs_<name>
           --job-id    - (OPTIONAL) job id (informational)
           --working-directory - (OPTIONAL) working directory for the script
           --env-<KEY> - inject KEY=VALUE into the script's environment;
                         generated automatically when running from a workflow
                         YAML (workflow + job + step env are merged)
           env         - (OPTIONAL) Reference to ShellEnv section(s), same as
                         Shell module
       Notes:
           GITHUB_OUTPUT, GITHUB_ENV, and GITHUB_PATH are wired up
           automatically.  Values written to GITHUB_OUTPUT are stored in the
           csmake environment both flat (key) and namespaced
           (_gha_steps_<step_id>_outputs_<key>) so that subsequent steps can
           reference them via ${{ steps.<step-id>.outputs.<key> }}.
           bash and sh run with -eo pipefail for GHA compatibility.
    """

    REQUIRED_OPTIONS = ['--script']

    # ------------------------------------------------------------------ #
    # Shell overrides                                                      #
    # ------------------------------------------------------------------ #

    def _getCommand(self, options, phase):
        if '--script' not in options:
            self.log.debug("Command for phase '%s' not defined" % phase)
            return (None, None)
        self.log.info("Executing GHA run: step in phase '%s'" % phase)
        return ('command', options['--script'])

    def _getExecer(self, options):
        shell = options.get('--shell', 'bash')
        return _SHELL_EXEC.get(shell, shell)

    def _getStartingEnvironment(self, options):
        env = dict(os.environ)
        env.setdefault('GITHUB_WORKSPACE', os.getcwd())
        for k, v in options.items():
            if k.startswith('--env-'):
                env[k[6:]] = v
        return env

    def _executeShell(self, command, env, execer='/bin/bash', cwd=None):
        """Run script via temp file with -eo pipefail for bash/sh."""
        shell_name = os.path.basename(execer or 'bash')
        if cwd is None:
            cwd = env.get('GITHUB_WORKSPACE') or os.getcwd()

        if shell_name in ('bash', 'sh'):
            preamble = ([execer, '--noprofile', '--norc', '-eo', 'pipefail']
                        if shell_name == 'bash' else [execer, '-e'])
            fd, tmp = tempfile.mkstemp(suffix='.sh')
            try:
                os.write(fd, command.encode('utf-8'))
                os.close(fd)
                os.chmod(tmp, 0o700)
                return subprocess.call(
                    preamble + [tmp],
                    env=env,
                    cwd=cwd,
                    stdout=self.log.out(),
                    stderr=self.log.err())
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            return subprocess.call(
                [execer, command],
                env=env,
                cwd=cwd,
                stdout=self.log.out(),
                stderr=self.log.err())

    # ------------------------------------------------------------------ #
    # default — GHA protocol around Shell execution                        #
    # ------------------------------------------------------------------ #

    def default(self, options):
        step_id = options.get('--step-id', '')
        name    = options.get('--name', step_id or 'run step')

        env = self._getStartingEnvironment(options).copy()

        if_cond = options.get('--if')
        if if_cond is not None and not self._eval_if(if_cond, env):
            self.log.chat("  Skipping step (condition false): " + name)
            self.log.passed()
            return True

        (_, script) = self._getCommand(options, self.engine.getPhase())
        if script is None:
            self._dontValidateFiles()
            self.log.skipped()
            return None

        script = self._subst(script, env)

        workdir = options.get('--working-directory')
        if workdir:
            workdir = self._subst(workdir, env)
            workdir = os.path.normpath(os.path.join(os.getcwd(), workdir))
            env['GITHUB_WORKSPACE'] = workdir

        out_f  = tempfile.mktemp(prefix='csmake_ghas_out_')
        env_f  = tempfile.mktemp(prefix='csmake_ghas_env_')
        path_f = tempfile.mktemp(prefix='csmake_ghas_path_')

        env['GITHUB_OUTPUT'] = out_f
        env['GITHUB_ENV']    = env_f
        env['GITHUB_PATH']   = path_f

        # Apply ShellEnv refs and mapping vars from Shell
        env = self._getEnvironment(options, env)

        execer = self._getExecer(options)
        cwd    = env.get('GITHUB_WORKSPACE') or os.getcwd()

        try:
            rc = self._executeShell(script, env, execer, cwd=cwd)

            outputs = self._parse_gha_file(out_f)
            if outputs:
                self.env.update(outputs)
                if step_id:
                    for k, v in outputs.items():
                        self.env.update(
                            {'_gha_steps_%s_outputs_%s' % (step_id, k): v})

            env_vars = self._parse_gha_file(env_f)
            if env_vars:
                self.env.update(env_vars)

            if os.path.exists(path_f):
                with open(path_f) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            os.environ['PATH'] = (
                                line + os.pathsep + os.environ.get('PATH', ''))

            if rc == 0:
                self.log.passed()
                return True
            else:
                self.log.failed()
                return None
        except Exception as e:
            self.log.error("GHActionsShell failed: " + str(e))
            self.log.failed()
            return None
        finally:
            for f in (out_f, env_f, path_f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # ${{ }} expression substitution                                       #
    # ------------------------------------------------------------------ #

    def _subst(self, text, env):
        def _replace(m):
            expr = m.group(1).strip()

            if expr == 'github.workspace':
                return os.getcwd()
            if expr.startswith('github.'):
                key = 'GITHUB_' + expr[7:].upper().replace('.', '_')
                return env.get(key, os.environ.get(key, ''))

            if expr.startswith('env.'):
                return env.get(expr[4:], '')

            if expr.startswith('steps.') and '.outputs.' in expr:
                parts = expr.split('.')
                if len(parts) >= 4:
                    csmake_key = '_gha_steps_%s_outputs_%s' % (
                        parts[1], parts[3])
                    return str(self.env.env.get(csmake_key, ''))

            if expr.startswith('needs.') and '.outputs.' in expr:
                parts = expr.split('.')
                if len(parts) >= 4:
                    csmake_key = '_gha_needs_%s_outputs_%s' % (
                        parts[1], parts[3])
                    return str(self.env.env.get(csmake_key, ''))

            return m.group(0)

        return _EXPR_RE.sub(_replace, text)

    # ------------------------------------------------------------------ #
    # if: condition evaluation                                             #
    # ------------------------------------------------------------------ #

    def _eval_if(self, condition, env):
        if condition is True or condition == 'true':
            return True
        if condition is False or condition == 'false':
            return False
        result = self._subst(str(condition).strip(), env)
        return result.lower().strip() not in ('false', '0', '', 'null', 'none')

    # ------------------------------------------------------------------ #
    # GITHUB_OUTPUT / GITHUB_ENV file parser                              #
    # ------------------------------------------------------------------ #

    def _parse_gha_file(self, path):
        result = {}
        if not os.path.exists(path):
            return result
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except IOError:
            return result
        i = 0
        while i < len(lines):
            line = lines[i]
            if '<<' in line:
                key, delim = line.split('<<', 1)
                value_lines = []
                i += 1
                while i < len(lines) and lines[i] != delim:
                    value_lines.append(lines[i])
                    i += 1
                result[key] = '\n'.join(value_lines)
            elif '=' in line:
                key, _, value = line.partition('=')
                result[key] = value
            i += 1
        return result
