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
import tarfile
import tempfile

try:
    import urllib.request as _urllib_request
except ImportError:
    import urllib as _urllib_request  # Python 2

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from CsmakeCore.CsmakeModuleAllPhase import CsmakeModuleAllPhase
from CsmakeModules.NodeRuntime import NodeRuntime
from CsmakeModules.DockerRuntime import DockerRuntime

_ACTIONS_CACHE = os.path.expanduser('~/.csmake/ghactions')
_EXPR_RE = re.compile(r'\$\{\{\s*(.*?)\s*\}\}')


class GHActions(CsmakeModuleAllPhase):
    """Purpose: Execute a GitHub Actions action locally
       Type: Module   Library: csmake-ghactions
       Phases: *any*
       Options:
           --action  - GitHub Actions action reference (e.g. actions/checkout@v4)
           <key>=<value> - Input parameters passed to the action as 'with:' inputs;
                           all keys NOT starting with '--' are forwarded.
       Outputs:
           GITHUB_OUTPUT and GITHUB_ENV values produced by the action are
           bridged into the csmake environment for downstream sections.
       Example:
           [GHActions@checkout]
           --action=actions/checkout@v4
           ref=main
           path=.
    """

    REQUIRED_OPTIONS = ['--action']

    def default(self, options):
        action_ref = options['--action'].strip()
        inputs = {k: v.strip() for k, v in options.items() if not k.startswith('--')}
        try:
            action_path = self._get_action(action_ref)
            action_def  = self._load_action_def(action_path)
            self._run_action(action_def, action_path, inputs, action_ref)
            self.log.passed()
            return True
        except Exception as e:
            self.log.error("GHActions '%s' failed: %s", action_ref, str(e))
            self.log.failed()
            return None

    # ------------------------------------------------------------------ #
    # Action resolution / download                                         #
    # ------------------------------------------------------------------ #

    def _parse_ref(self, ref):
        """'owner/repo@gitref' -> (owner, repo, gitref_or_None)"""
        at_parts = ref.split('@', 1)
        git_ref = at_parts[1] if len(at_parts) > 1 else None
        slash_parts = at_parts[0].split('/', 1)
        owner = slash_parts[0]
        repo  = slash_parts[1] if len(slash_parts) > 1 else slash_parts[0]
        return owner, repo, git_ref

    def _get_action(self, action_ref):
        """Return local path to the action, downloading from GitHub if necessary.
        If action_ref starts with './' or '/' it is treated as a local path and
        returned directly without any download."""
        if action_ref.startswith('./') or action_ref.startswith('/'):
            path = os.path.normpath(
                os.path.join(os.getcwd(), action_ref)
                if action_ref.startswith('./') else action_ref)
            if not os.path.isdir(path):
                raise RuntimeError(
                    "Local action path does not exist: %s" % path)
            return path

        owner, repo, git_ref = self._parse_ref(action_ref)
        ref   = git_ref or 'main'
        cache = os.path.join(_ACTIONS_CACHE, owner, repo, ref)
        if os.path.isdir(cache) and os.listdir(cache):
            self.log.chat("Using cached action: " + action_ref)
            return cache
        self.log.chat("Downloading action: " + action_ref)
        self._download_tarball(owner, repo, ref, cache)
        return cache

    def _download_tarball(self, owner, repo, ref, dest):
        try:
            os.makedirs(dest)
        except OSError:
            pass
        tmp = tempfile.mktemp(suffix='.tar.gz')
        try:
            downloaded = False
            for url in [
                'https://github.com/%s/%s/archive/refs/tags/%s.tar.gz' % (owner, repo, ref),
                'https://github.com/%s/%s/archive/refs/heads/%s.tar.gz' % (owner, repo, ref),
            ]:
                try:
                    _urllib_request.urlretrieve(url, tmp)
                    downloaded = True
                    break
                except Exception:
                    continue
            if not downloaded:
                raise RuntimeError(
                    "Could not download %s/%s at ref '%s'" % (owner, repo, ref))
            self._extract_tarball(tmp, dest)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _extract_tarball(self, tarball, dest):
        _extract_kwargs = {}
        if hasattr(tarfile, 'data_filter'):  # Python 3.12+
            _extract_kwargs['filter'] = 'data'
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                parts = member.name.lstrip('/').split('/', 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                if '..' in parts[1].split('/'):
                    continue
                member.name = parts[1]
                tar.extract(member, dest, **_extract_kwargs)

    # ------------------------------------------------------------------ #
    # action.yml loading                                                   #
    # ------------------------------------------------------------------ #

    def _load_action_def(self, action_path):
        if not _HAS_YAML:
            raise RuntimeError(
                "PyYAML is required for GHActions. Install with: pip install pyyaml")
        for name in ('action.yml', 'action.yaml'):
            p = os.path.join(action_path, name)
            if os.path.exists(p):
                with open(p) as f:
                    return _yaml.safe_load(f)
        raise RuntimeError("No action.yml found in %s" % action_path)

    # ------------------------------------------------------------------ #
    # Action execution dispatch                                            #
    # ------------------------------------------------------------------ #

    def _run_action(self, action_def, action_path, inputs, action_ref):
        runs  = action_def.get('runs') or {}
        using = runs.get('using', '')
        if using == 'composite':
            self._run_composite(action_def, action_path, inputs)
        elif using.startswith('node'):
            self._run_node(action_def, action_path, inputs)
        elif using == 'docker':
            self._run_docker(action_def, action_path, inputs)
        else:
            raise RuntimeError(
                "Unsupported action type '%s' in %s" % (using, action_ref))

    # ------------------------------------------------------------------ #
    # Composite runner                                                     #
    # ------------------------------------------------------------------ #

    def _run_composite(self, action_def, action_path, inputs):
        steps = (action_def.get('runs') or {}).get('steps') or []
        step_outputs = {}
        out_f  = tempfile.mktemp(prefix='csmake_gha_out_')
        env_f  = tempfile.mktemp(prefix='csmake_gha_env_')
        path_f = tempfile.mktemp(prefix='csmake_gha_path_')

        # Merge action.yml defaults with caller-supplied inputs so that
        # ${{ inputs.* }} expressions resolve correctly even when the
        # caller omitted an input that has a default value.
        effective_inputs = {}
        for name, defn in (action_def.get('inputs') or {}).items():
            default = (defn or {}).get('default', '') if defn else ''
            effective_inputs[name] = '' if default is None else str(default)
        effective_inputs.update(inputs)

        try:
            for step in steps:
                if not step:
                    continue
                step_id = step.get('id') or ''
                env = self._build_gha_env(
                    effective_inputs, action_def, out_f, env_f, path_f, action_path)
                for sid, outs in step_outputs.items():
                    for ok, ov in outs.items():
                        env['STEPS_%s_OUTPUTS_%s' % (sid.upper(), ok.upper())] = str(ov)

                uses       = step.get('uses')
                run_script = step.get('run')

                if uses:
                    if uses.startswith('./'):
                        sub_path = os.path.normpath(
                            os.path.join(action_path, uses[2:]))
                    else:
                        sub_path = self._get_action(uses)
                    sub_def = self._load_action_def(sub_path)
                    sub_inputs = {
                        k: self._subst(str(v), effective_inputs, step_outputs, env)
                        for k, v in (step.get('with') or {}).items()
                    }
                    self._run_action(sub_def, sub_path, sub_inputs, uses)
                elif run_script:
                    shell = step.get('shell', 'bash')
                    self._run_shell_step(
                        self._subst(run_script, effective_inputs, step_outputs, env),
                        shell, env, action_path)

                if step_id:
                    step_outputs[step_id] = self._parse_gha_file(out_f)
                    open(out_f, 'w').close()

            # Resolve composite action outputs: ${{ steps.*.outputs.* }} → out_f
            composite_outputs = action_def.get('outputs') or {}
            if composite_outputs:
                tmp_env = self._build_gha_env(
                    effective_inputs, action_def, out_f, env_f, path_f, action_path)
                with open(out_f, 'w') as f:
                    for out_name, out_defn in composite_outputs.items():
                        value_expr = (
                            out_defn.get('value', '')
                            if isinstance(out_defn, dict) else str(out_defn or ''))
                        value = self._subst(
                            str(value_expr), effective_inputs, step_outputs, tmp_env)
                        f.write('%s=%s\n' % (out_name, value))

            self._bridge_to_csmake(out_f, env_f, path_f)
        finally:
            for f in (out_f, env_f, path_f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # Node.js runner (delegates to NodeRuntime)                           #
    # ------------------------------------------------------------------ #

    def _run_node(self, action_def, action_path, inputs):
        runs  = action_def.get('runs') or {}
        main  = runs.get('main')
        pre   = runs.get('pre')
        post  = runs.get('post')
        if not main:
            raise RuntimeError("Node action has no 'main' defined")

        out_f  = tempfile.mktemp(prefix='csmake_gha_out_')
        env_f  = tempfile.mktemp(prefix='csmake_gha_env_')
        path_f = tempfile.mktemp(prefix='csmake_gha_path_')
        try:
            env = self._build_gha_env(
                inputs, action_def, out_f, env_f, path_f, action_path)
            runner = NodeRuntime(self.env, self.log)
            if pre:
                runner.execute(os.path.join(action_path, pre), env, action_path)
            runner.execute(os.path.join(action_path, main), env, action_path)
            if post:
                runner.execute(os.path.join(action_path, post), env, action_path)
            self._bridge_to_csmake(out_f, env_f, path_f)
        finally:
            for f in (out_f, env_f, path_f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # Docker runner (delegates to DockerRuntime)                          #
    # ------------------------------------------------------------------ #

    def _run_docker(self, action_def, action_path, inputs):
        runs  = action_def.get('runs') or {}
        image = runs.get('image', '')

        # GitHub Actions docker actions mount workspace at /github/workspace
        workdir = '/github/workspace'

        out_f  = tempfile.mktemp(prefix='csmake_gha_out_')
        env_f  = tempfile.mktemp(prefix='csmake_gha_env_')
        path_f = tempfile.mktemp(prefix='csmake_gha_path_')
        try:
            env = self._build_gha_env(
                inputs, action_def, out_f, env_f, path_f, action_path)

            # Resolve image vs local Dockerfile
            if image.startswith('docker://'):
                actual_image = image[len('docker://'):]
                dockerfile   = None
            elif image == 'Dockerfile' or image.startswith('Dockerfile'):
                actual_image = None
                dockerfile   = os.path.join(action_path, 'Dockerfile')
            else:
                actual_image = image
                dockerfile   = None

            action_args = [
                self._subst(str(a), inputs, {}, env)
                for a in (runs.get('args') or [])
            ]

            runner = DockerRuntime(self.env, self.log)
            runner.execute(
                image=actual_image,
                env=env,
                host_cwd=os.getcwd(),
                workdir=workdir,
                dockerfile=dockerfile,
                args=action_args or None,
                extra_volumes=['%s:/github/action_path' % action_path],
            )
            self._bridge_to_csmake(out_f, env_f, path_f)
        finally:
            for f in (out_f, env_f, path_f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # Shell step execution (composite 'run:' steps)                       #
    # ------------------------------------------------------------------ #

    _SHELL_PREAMBLE = {
        'bash':    ['bash', '--noprofile', '--norc', '-eo', 'pipefail'],
        'sh':      ['sh', '-e'],
        'pwsh':    ['pwsh', '-NonInteractive', '-Command'],
        'cmd':     ['cmd', '/D', '/E:ON', '/V:OFF', '/S', '/C'],
        'python':  ['python'],
        'python3': ['python3'],
    }

    def _run_shell_step(self, script, shell, env, cwd):
        preamble = self._SHELL_PREAMBLE.get(
            shell, ['bash', '--noprofile', '--norc', '-eo', 'pipefail'])
        if shell in ('bash', 'sh'):
            fd, tmp = tempfile.mkstemp(suffix='.sh')
            try:
                os.write(fd, script.encode('utf-8'))
                os.close(fd)
                os.chmod(tmp, 0o700)
                rc = subprocess.call(preamble + [tmp], env=env, cwd=cwd)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            rc = subprocess.call(preamble + [script], env=env, cwd=cwd)
        if rc != 0:
            raise RuntimeError("Shell step exited with code %d" % rc)

    # ------------------------------------------------------------------ #
    # GHA environment construction                                         #
    # ------------------------------------------------------------------ #

    def _build_gha_env(self, inputs, action_def, out_f, env_f, path_f, action_path):
        """Build the subprocess environment for a GHA step."""
        env = dict(os.environ)
        env['GITHUB_OUTPUT']      = out_f
        env['GITHUB_ENV']         = env_f
        env['GITHUB_PATH']        = path_f
        env['GITHUB_ACTION_PATH'] = action_path
        env['GITHUB_WORKSPACE']   = os.getcwd()
        env['RUNNER_OS']          = os.uname().sysname if hasattr(os, 'uname') else 'Linux'

        # Merge action.yml defaults with caller-supplied inputs
        resolved = {}
        for name, defn in (action_def.get('inputs') or {}).items():
            default = (defn or {}).get('default', '') if defn else ''
            resolved[name] = '' if default is None else str(default)
        resolved.update(inputs)

        for k, v in resolved.items():
            env_key = 'INPUT_' + k.upper().replace(' ', '_')
            env[env_key] = str(v) if v is not None else ''

        return env

    # ------------------------------------------------------------------ #
    # Output bridging                                                      #
    # ------------------------------------------------------------------ #

    def _bridge_to_csmake(self, out_f, env_f, path_f):
        """Push GITHUB_OUTPUT and GITHUB_ENV values into the csmake environment."""
        merged = {}
        merged.update(self._parse_gha_file(out_f))
        merged.update(self._parse_gha_file(env_f))
        if merged:
            self.env.update(merged)

        if os.path.exists(path_f):
            with open(path_f) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        os.environ['PATH'] = (
                            line + os.pathsep + os.environ.get('PATH', ''))

    def _parse_gha_file(self, path):
        """Parse GITHUB_OUTPUT / GITHUB_ENV file (key=val or key<<DELIM blocks)."""
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

    # ------------------------------------------------------------------ #
    # Expression substitution (${{ ... }})                                #
    # ------------------------------------------------------------------ #

    def _subst(self, text, inputs, step_outputs, env):
        def _replace(m):
            expr = m.group(1).strip()
            if expr.startswith('inputs.'):
                return inputs.get(expr[7:], '')
            if expr.startswith('env.'):
                return env.get(expr[4:], '')
            if expr == 'github.workspace':
                return os.getcwd()
            if expr == 'github.action_path':
                return env.get('GITHUB_ACTION_PATH', '')
            if expr.startswith('steps.') and '.outputs.' in expr:
                parts = expr.split('.')
                if len(parts) >= 4:
                    return step_outputs.get(parts[1], {}).get(parts[3], '')
            return m.group(0)
        return _EXPR_RE.sub(_replace, text)
