import json
from jsonschema import Draft202012Validator
from agentlab.abi import PluginOperationError
from agentlab.contracts import local_schema


def fail(code, message=''):
    raise PluginOperationError(code, message or code)


def schema_check(schema):
    try:
        local_schema(schema)
        Draft202012Validator.check_schema(schema)
    except Exception:
        fail('TOOL.INVALID_SCHEMA')


def check(schema, value, code='TOOL.INVALID_ARGUMENTS'):
    if not Draft202012Validator(schema).is_valid(value):
        fail(code)


def tool(name, description, properties, required=None, side_effect=False):
    return dict(name=name, description=description,
        input_schema={'type':'object','properties':properties,'required':required or [],'additionalProperties':False},
        output_schema={'type':'object'}, side_effect=side_effect, concurrency_safe=not side_effect,
        timeout_ms=10000)


def result(text='', *, is_error=False, **extra):
    return dict(content=[{'type':'text','text':str(text)}], is_error=is_error, **extra)


def text_of(value):
    return '\n'.join(r.get('text','') for r in value.get('content',[]))


def emit(ctx, topic, detail, call):
    ctx.emit(topic, detail, parent=call)
