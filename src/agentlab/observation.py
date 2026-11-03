"""Non-blocking, bounded observation. Consumers cannot control execution."""
from copy import deepcopy
from datetime import datetime, timezone


class BoundedRows(list):
    """Preserve the public JSON-safe records facade without unbounded memory."""
    def __init__(self,capacity):
        super().__init__();self.capacity=capacity
    def append(self,row):
        super().append(row)
        if len(self)>self.capacity:del self[:len(self)-self.capacity]


class ObservationLog:
    def __init__(self, capacity=4096):
        self.rows = BoundedRows(capacity)
        self.sequence = 0

    def append(self, row):
        self.sequence += 1
        row = dict(row, seq=self.sequence, time=datetime.now(timezone.utc).isoformat())
        self.rows.append(row)
        return row

    def read(self, after=0):
        first = self.rows[0]['seq'] if self.rows else self.sequence + 1
        return {'events': deepcopy([r for r in self.rows if r['seq'] > after]),
                'cursor': self.sequence, 'gap': after < first - 1, 'first_available': first}


def snapshot(host):
    return {'composition': host.data.get('composition'), 'state': host.state,
            'entry': host.data.get('entry'), 'root_scope': host.scope.scope_id,
            'instances': {key: {'plugin': i.definition.id, 'version': i.definition.version,
                'bindings': i.spec.get('bindings', {}), 'state': i.state,
                'scope': i.scope.scope_id, 'owner_scope': i.scope.parent.scope_id if i.scope.parent else None}
                for key, i in host.instances.items()},
            'executions': {key: {'instance': e.instance.id, 'caller': e.caller_id, 'method': e.method,
                'state': e.state, 'parent': e.parent.id if e.parent else None,
                'scope': e.scope.scope_id, 'owner_scope': e.scope.parent.scope_id}
                for key, e in host.supervisor.executions.items()}}
