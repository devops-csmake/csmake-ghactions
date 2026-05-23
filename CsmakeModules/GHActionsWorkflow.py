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

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from CsmakeCore.CsmakeModuleAllPhase import CsmakeModuleAllPhase

_EXPR_RE = re.compile(r'\$\{\{\s*(.*?)\s*\}\}')


class GHActionsWorkflow(CsmakeModuleAllPhase):
    """Purpose: Execute a GitHub Actions workflow YAML file locally
       Type: Module   Library: csmake-ghactions
       Phases: *any*
       Options:
           --workflow  - path to the workflow YAML file
                         (e.g. .github/workflows/ci.yml)
           --job       - (OPTIONAL) comma-separated job id(s) to run;
                         runs all jobs in dependency order if omitted
           <key>=<value> - extra environment variables injected at the
                           workflow level, available to all steps
       Notes:
           'uses:' steps are executed via GHActions.
           'run:' steps are executed as shell scripts.
           GITHUB_OUTPUT and GITHUB_ENV values produced by each step are
           captured as step outputs and bridged into the csmake environment.
           ${{ secrets.KEY }} references are resolved from the csmake
           environment (which can hold secrets via the secrets module).
       Example:
           [GHActionsWorkflow@ci]
           --workflow=.github/workflows/ci.yml
           --job=build,test
    """

    REQUIRED_OPTIONS = ['--workflow']

    def default(self, options):
        workflow_path = options['--workflow'].strip()
        raw_jobs = options.get('--job', '').strip()
        job_filter = (set(j.strip() for j in raw_jobs.split(',') if j.strip())
                      if raw_jobs else None)
        extra_env = {k: v.strip() for k, v in options.items()
                     if not k.startswith('--')}
        try:
            workflow = self._load_workflow(workflow_path)
            self._run_workflow(workflow, job_filter, extra_env)
            self.log.passed()
            return True
        except Exception as e:
            self.log.error("GHActionsWorkflow failed: " + str(e))
            self.log.failed()
            return None

    # ------------------------------------------------------------------ #
    # Workflow loading                                                      #
    # ------------------------------------------------------------------ #

    def _load_workflow(self, path):
        if not _HAS_YAML:
            raise RuntimeError(
                "PyYAML is required for GHActionsWorkflow. "
                "Install with: pip install pyyaml")
        if not os.path.exists(path):
            raise RuntimeError("Workflow file not found: " + path)
        with open(path) as f:
            return _yaml.safe_load(f)

    # ------------------------------------------------------------------ #
    # Workflow execution                                                    #
    # ------------------------------------------------------------------ #

    def _run_workflow(self, workflow, job_filter, extra_env):
        # Workflow-level env: start from os.environ, layer workflow env on top
        wf_env = dict(os.environ)
        for k, v in (workflow.get('env') or {}).items():
            wf_env[k] = '' if v is None else str(v)
        wf_env.update(extra_env)
        wf_env.setdefault('GITHUB_WORKSPACE', os.getcwd())

        jobs = workflow.get('jobs') or {}
        if not jobs:
            raise RuntimeError("Workflow has no jobs")

        job_outputs = {}
        for job_id in self._topo_order(jobs):
            if job_filter and job_id not in job_filter:
                continue
            job = jobs[job_id]
            self.log.chat("Job: " + job_id)
            job_env = dict(wf_env)
            for k, v in (job.get('env') or {}).items():
                job_env[k] = '' if v is None else str(v)
            job_outputs[job_id] = self._run_job(job, job_env, job_outputs)

    def _topo_order(self, jobs):
        """Return job ids sorted so dependencies come before dependents."""
        order, seen = [], set()

        def visit(jid):
            if jid in seen:
                return
            seen.add(jid)
            needs = jobs.get(jid, {}).get('needs') or []
            if isinstance(needs, str):
                needs = [needs]
            for dep in needs:
                if dep in jobs:
                    visit(dep)
            order.append(jid)

        for jid in jobs:
            visit(jid)
        return order

    # ------------------------------------------------------------------ #
    # Job execution                                                         #
    # ------------------------------------------------------------------ #

    def _run_job(self, job, job_env, job_outputs):
        steps = job.get('steps') or []
        step_outputs = {}

        for i, step in enumerate(steps):
            if not step:
                continue

            # Evaluate 'if:' condition
            condition = step.get('if')
            if condition is not None:
                if not self._eval_condition(
                        condition, job_env, step_outputs, job_outputs):
                    self.log.chat(
                        "    Skipping (condition false): "
                        + str(step.get('name') or i))
                    continue

            step_id   = step.get('id')   or ('step_%d' % i)
            step_name = step.get('name') or step_id
            self.log.chat("  Step: " + step_name)

            # Step-level env: layer on top of job env
            step_env = dict(job_env)
            for k, v in (step.get('env') or {}).items():
                step_env[k] = self._subst(
                    '' if v is None else str(v),
                    step_outputs, job_outputs, job_env)

            uses = step.get('uses')
            run  = step.get('run')

            if uses:
                outputs = self._run_uses_step(
                    step, uses, step_env, step_outputs, job_outputs, step_id)
            elif run:
                outputs = self._run_run_step(
                    step, run, step_env, step_outputs, job_outputs, step_id)
            else:
                outputs = {}

            step_outputs[step_id] = outputs or {}

        # Resolve job-level outputs
        result = {}
        for name, expr in (job.get('outputs') or {}).items():
            result[name] = self._subst(
                str(expr), step_outputs, job_outputs, job_env)
        return result

    # ------------------------------------------------------------------ #
    # uses: step — delegates to GHActions                                  #
    # ------------------------------------------------------------------ #

    def _run_uses_step(self, step, uses, step_env,
                       step_outputs, job_outputs, step_id):
        from CsmakeModules.GHActions import GHActions

        options = {'--action': uses}
        for k, v in (step.get('with') or {}).items():
            options[k] = self._subst(
                '' if v is None else str(v),
                step_outputs, job_outputs, step_env)

        # Inject step-level env into os.environ so GHActions inherits it;
        # restore afterwards to avoid leaking between steps.
        saved_os_env = dict(os.environ)
        os.environ.update(step_env)
        try:
            runner = GHActions(self.env, self.log)
            result = runner.default(options)
        finally:
            os.environ.clear()
            os.environ.update(saved_os_env)

        if result is None:
            raise RuntimeError(
                "uses: step '%s' (%s) failed" % (step_id, uses))

        # Read the action's declared outputs by name from the csmake env.
        # Using a diff of env keys misses updates to keys already present
        # (e.g. two sequential steps that both produce 'greeting').
        outputs = {}
        try:
            action_path = runner._get_action(uses)
            action_def  = runner._load_action_def(action_path)
            for out_name in (action_def.get('outputs') or {}):
                if out_name in self.env.env:
                    outputs[out_name] = str(self.env.env[out_name])
        except Exception:
            pass
        return outputs

    # ------------------------------------------------------------------ #
    # run: step — direct shell execution                                   #
    # ------------------------------------------------------------------ #

    def _run_run_step(self, step, script, step_env,
                      step_outputs, job_outputs, step_id):
        shell = step.get('shell', 'bash')
        workdir = step.get('working-directory')
        if workdir:
            workdir = self._subst(workdir, step_outputs, job_outputs, step_env)
            workdir = os.path.normpath(os.path.join(os.getcwd(), workdir))
        else:
            workdir = os.getcwd()

        script = self._subst(script, step_outputs, job_outputs, step_env)

        out_f  = tempfile.mktemp(prefix='csmake_wf_out_')
        env_f  = tempfile.mktemp(prefix='csmake_wf_env_')
        path_f = tempfile.mktemp(prefix='csmake_wf_path_')

        run_env = dict(step_env)
        run_env['GITHUB_OUTPUT']    = out_f
        run_env['GITHUB_ENV']       = env_f
        run_env['GITHUB_PATH']      = path_f
        run_env['GITHUB_WORKSPACE'] = workdir

        try:
            self._exec_shell(script, shell, run_env, workdir)
            outputs = self._parse_gha_file(out_f)

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
            return outputs
        finally:
            for f in (out_f, env_f, path_f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    _SHELL_PREAMBLE = {
        'bash':    ['bash', '--noprofile', '--norc', '-eo', 'pipefail'],
        'sh':      ['sh', '-e'],
        'pwsh':    ['pwsh', '-NonInteractive', '-Command'],
        'python':  ['python'],
        'python3': ['python3'],
    }

    def _exec_shell(self, script, shell, env, cwd):
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
            raise RuntimeError("run: step exited with code %d" % rc)

    # ------------------------------------------------------------------ #
    # if: condition evaluation                                             #
    # ------------------------------------------------------------------ #

    def _eval_condition(self, condition, job_env, step_outputs, job_outputs):
        if condition is True:
            return True
        if condition is False:
            return False
        expr = self._subst(str(condition).strip(),
                           step_outputs, job_outputs, job_env)
        return expr.lower().strip() not in ('false', '0', '', 'null', 'none')

    # ------------------------------------------------------------------ #
    # ${{ }} expression substitution                                       #
    # ------------------------------------------------------------------ #

    def _subst(self, text, step_outputs, job_outputs, env):
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
                    return step_outputs.get(parts[1], {}).get(parts[3], '')

            if expr.startswith('needs.') and '.outputs.' in expr:
                parts = expr.split('.')
                if len(parts) >= 4:
                    return job_outputs.get(parts[1], {}).get(parts[3], '')

            if expr.startswith('secrets.'):
                key = expr[8:]
                # Resolve via csmake secrets system if registered, else env
                try:
                    secret_val = self.env.doSubstitutions('(((%s)))' % key)
                    return str(secret_val)
                except Exception:
                    pass
                return env.get(key, os.environ.get(key, ''))

            return m.group(0)

        return _EXPR_RE.sub(_replace, text)

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
