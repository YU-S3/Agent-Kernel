"""Control: fail-closed system enforcement, separate from application budgets."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import time
import hashlib
import base64
from .abi import KernelError, digest


class Control:
    def __init__(self):
        self.receipts = []
        self.secrets = set()
        self.resources = {}

    def narrow(self, parent, child):
        for key in ('permissions', 'env'):
            if key in parent and not set(child.get(key, [])).issubset(parent[key]):
                raise KernelError('CONTROL.DENIED', 'Scope grant exceeds parent')
        for key in ('memory_mb', 'pids', 'cpus', 'max_wall_seconds'):
            if key in parent and child.get(key, parent[key]) > parent[key]:
                raise KernelError('CONTROL.DENIED', 'Scope resource bound exceeds parent')
        if parent.get('network') == 'none' and child.get('network') != 'none':
            raise KernelError('CONTROL.DENIED', 'Scope network exceeds parent')

    def check_config(self, config, policy):
        self.secrets.update(os.environ[k] for k in policy.get('env', []) if os.environ.get(k))
        def walk(obj):
            if isinstance(obj, dict):
                for key, val in obj.items():
                    if key.lower() in ('api_key', 'password', 'secret', 'access_token') and val:
                        raise KernelError('CONTROL.DENIED', 'Use an environment name/SecretRef, not a literal secret')
                    walk(val)
            elif isinstance(obj, list):
                for val in obj: walk(val)
            elif isinstance(obj, str) and any(secret in obj for secret in self.secrets if len(secret) >= 8):
                raise KernelError('CONTROL.DENIED', 'Configuration contains a granted secret value')
        walk(config)

    def redact(self, obj):
        if isinstance(obj, dict): return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list): return [self.redact(v) for v in obj]
        if isinstance(obj, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                if secret: obj = obj.replace(secret, '[redacted]')
        return obj

    def publish_resource(self, content, *, subject, scope, ttl=60):
        """Host-only byte grant; no arbitrary path access or ambient credentials."""
        if not isinstance(content, bytes) or not 0 < ttl <= 3600 or scope.closed:
            raise KernelError('CONTROL.DENIED', 'Invalid resource grant')
        self.resources = {k: r for k, r in self.resources.items() if r[0]['expires_at'] > time.time() and not r[2].closed}
        if len(content) > 16 * 1024 * 1024 or sum(len(r[1]) for r in self.resources.values()) + len(content) > 64 * 1024 * 1024:
            raise KernelError('CONTROL.RESOURCE_LIMIT', 'Resource store capacity')
        token = uuid.uuid4().hex
        ref = dict(type='ResourceRef', token=token, subject=subject, expires_at=time.time()+ttl,
                   sha256=hashlib.sha256(content).hexdigest(), size=len(content), rights=['read'])
        self.resources[token] = (ref, content, scope)
        scope.defer(lambda: self.resources.pop(token, None))
        return dict(ref)

    def read_resource(self, ref, *, subject, offset=0, length=65536):
        row = self.resources.get(ref.get('token')) if isinstance(ref, dict) else None
        if not row or row[0] != ref or subject != ref['subject'] or row[2].closed or time.time() >= ref['expires_at']:
            raise KernelError('CONTROL.DENIED', 'Resource grant invalid, expired, revoked or wrong subject')
        if type(offset) is not int or type(length) is not int or offset < 0 or not 0 <= length <= 65536:
            raise KernelError('CONTROL.DENIED', 'Invalid resource range')
        return base64.b64encode(row[1][offset:offset+length]).decode('ascii')

    def validate(self, spec, policy):
        execution = spec.get('execution', {})
        isolation = execution.get('isolation', 'local')
        allowed = {'network', 'memory_mb', 'pids', 'cpus', 'max_wall_seconds', 'env', 'permissions'}
        if set(policy) - allowed:
            raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Unknown control policy', phase='resolve')
        if isolation not in ('local', 'process'):
            raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Unknown isolation', phase='resolve')
        backend = execution.get('backend', 'docker' if isolation == 'process' else 'local')
        strict = any(k in policy for k in ('memory_mb', 'pids', 'cpus')) or policy.get('network', 'inherit' if isolation == 'local' else 'none') != 'inherit'
        if isolation == 'local' and strict or backend == 'local' and strict:
            raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Trusted local code cannot enforce this policy', phase='resolve')
        if isolation == 'process' and backend == 'docker':
            image = execution.get('image', '')
            if '@sha256:' not in image and not image.startswith('sha256:'):
                raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Pinned Docker image required', phase='resolve')
            if not shutil.which('docker'):
                raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Docker unavailable', phase='resolve')
            if policy.get('network', 'none') != 'none':
                raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'This backend implements network=none only', phase='resolve')
            check = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=15)
            if check.returncode:
                raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Pinned image unavailable', phase='resolve')
        elif backend != 'local':
            raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'Unknown backend', phase='resolve')
        for name in ('memory_mb', 'pids', 'cpus', 'max_wall_seconds'):
            if name in policy and (type(policy[name]) not in (int, float) or policy[name] <= 0):
                raise KernelError('CONTROL.DENIED', 'Invalid resource limit', phase='resolve')
        if not isinstance(policy.get('env', []), list):
            raise KernelError('CONTROL.DENIED', 'Environment must be explicit names', phase='resolve')
        for key in ('env', 'permissions'):
            if not isinstance(policy.get(key, []), list) or any(not isinstance(s, str) or not s for s in policy.get(key, [])):
                raise KernelError('CONTROL.DENIED', 'Invalid grant names', phase='resolve')
        if any(not k.replace('_', '').isalnum() for k in policy.get('env', [])):
            raise KernelError('CONTROL.DENIED', 'Invalid environment name', phase='resolve')

    def command(self, definition, spec, policy):
        loader = definition.manifest['loader']
        execution = spec.get('execution', {})
        backend = execution.get('backend', 'docker')
        env = {k: os.environ[k] for k in policy.get('env', []) if k in os.environ}
        source = Path(__file__).resolve().parents[1]
        if backend == 'docker':
            name = 'agentkernel-' + uuid.uuid4().hex
            command = ['docker', 'run', '-i', '--name', name, '--network=none',
                       '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                       '--memory', str(policy.get('memory_mb', 256)) + 'm',
                       '--memory-swap', str(policy.get('memory_mb', 256)) + 'm',
                       '--pids-limit', str(policy.get('pids', 32)), '--cpus', str(policy.get('cpus', 1)),
                       '--user', '65534:65534', '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m',
                       '-v', str(definition.root) + ':/plugin:ro',
                       '-v', str(source) + ':/sdk:ro', '-w', '/plugin', '-e', 'PYTHONPATH=/sdk']
            # Values travel only in the child environment, never in a receipt or command line.
            for key in env:
                command += ['-e', key]
            command += [execution['image']]
            if loader['adapter'] == 'python':
                command += ['python', '-m', 'agentlab.worker_process']
            else:
                command += loader['command']
            env.update({k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'DOCKER_HOST', 'HOME') if k in os.environ})
        else:
            name = None
            env.update({k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR') if k in os.environ})
            if loader['adapter'] == 'python':
                command = [sys.executable, '-m', 'agentlab.worker_process']
                env['PYTHONPATH'] = str(source)
            else:
                command = list(loader['command'])
                for n, part in enumerate(command):
                    if part.startswith('./'):
                        command[n] = str(definition.root / part)
        receipt = {'definition': definition.digest, 'backend': backend,
                   'policy_digest': digest(policy), 'environment_names': sorted(policy.get('env', [])),
                   'container': name, 'hard_isolation': backend == 'docker',
                   'image': execution.get('image'),
                   'effective_limits': {k: policy.get(k, default) for k, default in
                       [('memory_mb', 256), ('pids', 32), ('cpus', 1)]} if backend == 'docker' else {}}
        self.receipts.append(receipt)
        return command, env, name
