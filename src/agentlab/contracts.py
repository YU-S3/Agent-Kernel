"""Structural ABI validation. Business payload schemas stay in plugins."""
from jsonschema import Draft202012Validator
from .abi import KernelError

NAME = {'type': 'string', 'minLength': 1, 'maxLength': 256}
NAMES = {'type': 'array', 'items': NAME, 'uniqueItems': True}
OBJECT = {'type': 'object'}
LOADER = {'oneOf': [
    {'type': 'object', 'required': ['adapter', 'target'], 'additionalProperties': False,
     'properties': {'adapter': {'const': 'python'}, 'target': {'type':'string', 'pattern':'^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$'}}},
    {'type': 'object', 'required': ['adapter', 'command'], 'additionalProperties': False,
     'properties': {'adapter': {'const': 'jsonrpc_stdio'},
                    'command': {'type': 'array', 'minItems': 1, 'items': NAME}}}]}
MANIFEST = {'type': 'object', 'required': ['id', 'version', 'kernel_api', 'loader', 'exports'],
    'additionalProperties': False, 'properties': {
        'id': NAME, 'version': NAME, 'kernel_api': {'const': 1}, 'loader': LOADER,
        'exports': {**NAMES, 'minItems': 1}, 'local_only': NAMES,
        'requires': {'type': 'object', 'additionalProperties': NAMES},
        'optional_requires': {'type': 'object', 'additionalProperties': NAMES},
        'requested_permissions': NAMES,
        'python_dependencies': NAMES,
        'config_schema': {'anyOf': [OBJECT, NAME]}, 'description': {'type': 'string'}}}
EXECUTION = {'type': 'object', 'additionalProperties': False, 'properties': {
    'isolation': {'enum': ['local', 'process']}, 'backend': {'enum': ['local', 'docker']},
    'image': NAME, 'max_inflight': {'type': 'integer', 'minimum': 1},
    'reentrant': {'type':'boolean'},
    'max_queue': {'type': 'integer', 'minimum': 0}}}
INSTANCE = {'type': 'object', 'required': ['plugin'], 'additionalProperties': False,
    'properties': {'plugin': NAME, 'config': OBJECT, 'execution': EXECUTION, 'control': OBJECT,
                   'bindings': {'type': 'object', 'additionalProperties': NAME}}}
COMPOSITION = {'type': 'object', 'required': ['kernel_api', 'composition', 'packages', 'instances'],
    'additionalProperties': False, 'properties': {
        'kernel_api': {'const': 1}, 'composition': NAME,
        'packages': {'type': 'array', 'items': {'type': 'object',
            'required': ['id', 'version', 'digest'], 'additionalProperties': False,
            'properties': {'id': NAME, 'version': NAME,
                           'digest': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'}}}},
        'instances': {'type': 'object', 'additionalProperties': INSTANCE},
        'templates': {'type': 'object', 'additionalProperties': INSTANCE}, 'control': OBJECT,
        'entry': {'type': 'object', 'required': ['instance', 'method'], 'additionalProperties': False,
                  'properties': {'instance': NAME, 'method': NAME}}}}


def validate(schema, obj):
    errors = sorted(Draft202012Validator(schema).iter_errors(obj), key=lambda e: str(e.path))
    if errors:
        # Never echo configuration values (which can contain caller mistakes/secrets).
        raise KernelError('COMPOSITION.CONFIG_INVALID',
                          'Invalid structure at ' + '/'.join(map(str, errors[0].path)), phase='resolve')


def local_schema(schema):
    if isinstance(schema, dict):
        for key, val in schema.items():
            if key in ('$ref', '$dynamicRef') and (not isinstance(val, str) or not val.startswith('#')):
                raise KernelError('COMPOSITION.CONFIG_INVALID', 'External schema references forbidden', phase='resolve')
            local_schema(val)
    elif isinstance(schema, list):
        for val in schema:
            local_schema(val)
