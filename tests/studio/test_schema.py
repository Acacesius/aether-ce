import copy
import unittest

from aether.studio import capabilities, schema
from aether.studio.schema import SchemaError, demo_spec, validate_spec


def stage(key, needs=(), **kw):
    item = {'key': key, 'title': key, 'needs': list(needs), 'executor_step': 'mock:success', 'timeout_seconds': 10,
            'capabilities': ['demo.run']}
    item.update(kw)
    return item


def with_stages(*stages):
    spec = demo_spec()
    spec['stages'] = list(stages)
    return spec


class SchemaTests(unittest.TestCase):
    def code(self, spec, **kw):
        with self.assertRaises(SchemaError) as ctx:
            validate_spec(spec, **kw)
        return ctx.exception.code

    def test_valid_minimal_definition(self):
        out = validate_spec(demo_spec())
        self.assertTrue(out['executor']['demo'])
        self.assertEqual(out['retry'], {'max_attempts': 1, 'backoff_seconds': 0})

    def test_validate_is_idempotent_and_hash_stable(self):
        once = validate_spec(demo_spec())
        self.assertEqual(validate_spec(copy.deepcopy(once)), once)
        self.assertEqual(schema.spec_sha256(once), schema.spec_sha256(validate_spec(once)))
        self.assertRegex(schema.spec_sha256(once), r'^[0-9a-f]{64}$')

    def test_unknown_field_rejected(self):
        spec = demo_spec()
        spec['surprise'] = 1
        self.assertEqual(self.code(spec), 'unknown_field')
        spec = demo_spec()
        spec['stages'][0]['extra'] = 1
        self.assertEqual(self.code(spec), 'unknown_field')

    def test_enum_values(self):
        for path, value in ((('executor', 'kind'), 'shell'), (('workspace', 'kind'), 'host')):
            spec = demo_spec()
            spec.setdefault(path[0], {'kind': 'scratch'})[path[1]] = value
            self.assertEqual(self.code(spec), 'schema_invalid')
        self.assertEqual(schema.STATUSES, ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked'))

    def test_demo_flag_follows_executor_kind(self):
        spec = demo_spec()
        spec['executor']['demo'] = False
        self.assertEqual(self.code(spec), 'schema_invalid')

    def test_secret_inputs_unsupported(self):
        spec = demo_spec()
        spec['inputs'] = [{'name': 'tok', 'type': 'string', 'secret': True}]
        self.assertEqual(self.code(spec), 'secret_inputs_unsupported')

    def test_cost_must_be_zero_without_provider(self):
        spec = demo_spec()
        spec['limits']['cost_usd_cents'] = 5
        self.assertEqual(self.code(spec), 'schema_invalid')

    def test_unapproved_capability_rejected(self):
        for cap in ('shell.host', 'ssh.exec', 'docker.socket', 'provider.cloud', 'wake.node', 'nonsense', 'net.egress.allowlist'):
            spec = demo_spec()
            spec['capabilities'] = ['demo.run', cap]
            self.assertEqual(self.code(spec), 'capability_not_allowed', cap)

    def test_provider_local_needs_local_provider(self):
        spec = demo_spec()
        spec['capabilities'] = ['demo.run', 'provider.local']
        self.assertEqual(self.code(spec, provider='none'), 'capability_not_allowed')
        validate_spec(spec, provider='local')

    def test_network_allowlist_requires_capability(self):
        spec = demo_spec()
        spec['network_allowlist'] = ['example.com:443']
        self.assertEqual(self.code(spec), 'capability_not_allowed')

    def test_shell_capability_absent_in_ce(self):
        for provider in ('none', 'local'):
            for cap in capabilities.REGISTRY:
                self.assertFalse(capabilities.forbidden(cap))
                self.assertFalse(cap.startswith(('shell', 'ssh', 'wake', 'docker', 'systemd', 'tmux')))
            self.assertNotIn('net.egress.allowlist', capabilities.available(provider))
        for cap in ('shell.host', 'ssh.any', 'wake.x', 'docker.socket', 'systemd.start', 'tmux.send', 'provider.cloud'):
            self.assertTrue(capabilities.forbidden(cap))
            with self.assertRaises(capabilities.CapabilityError):
                capabilities.check([cap])

    def test_stage_capability_must_be_subset(self):
        spec = with_stages(stage('a', capabilities=['workspace.read']))
        self.assertEqual(self.code(spec), 'capability_not_allowed')

    def test_rw_workspace_requires_write_capability(self):
        spec = demo_spec()
        spec['workspace'] = {'kind': 'mounted', 'mount': 'ws1', 'access': 'rw'}
        self.assertEqual(self.code(spec), 'capability_not_allowed')

    def test_cycle_rejected(self):
        self.assertEqual(self.code(with_stages(stage('a', ['b']), stage('b', ['a']))), 'cycle_detected')
        self.assertEqual(self.code(with_stages(stage('a', ['a']))), 'cycle_detected')

    def test_unknown_dependency_rejected(self):
        self.assertEqual(self.code(with_stages(stage('a', ['ghost']))), 'schema_invalid')

    def test_duplicate_stage_key_rejected(self):
        self.assertEqual(self.code(with_stages(stage('a'), stage('a'))), 'schema_invalid')

    def test_max_stages_64(self):
        validate_spec(with_stages(*[stage(f's{i}') for i in range(64)]))
        self.assertEqual(self.code(with_stages(*[stage(f's{i}') for i in range(65)])), 'schema_invalid')

    def test_depth_limit_16(self):
        chain = lambda n: [stage(f's{i}', [f's{i - 1}'] if i else []) for i in range(n)]
        validate_spec(with_stages(*chain(16)))
        self.assertEqual(self.code(with_stages(*chain(17))), 'schema_invalid')

    def test_topo_order_respects_needs(self):
        order = schema.topo_order(with_stages(stage('c', ['a', 'b']), stage('b', ['a']), stage('a'))['stages'])
        self.assertEqual(order, ['a', 'b', 'c'])

    def test_overrides_only_declared_inputs(self):
        spec = demo_spec()
        spec['inputs'] = [{'name': 'who', 'type': 'string', 'required': False, 'default': 'a'}]
        spec = validate_spec(spec)
        snap = schema.apply_overrides(spec, {'who': 'b'})
        self.assertEqual(snap['inputs'][0]['default'], 'b')
        self.assertEqual(spec['inputs'][0]['default'], 'a')
        with self.assertRaises(SchemaError) as ctx:
            schema.apply_overrides(spec, {'other': 1})
        self.assertEqual(ctx.exception.code, 'override_not_allowed')

    def test_overrides_may_only_lower_limits(self):
        spec = validate_spec(demo_spec(wall_seconds=100))
        self.assertEqual(schema.apply_overrides(spec, {'limits': {'wall_seconds': 50}})['limits']['wall_seconds'], 50)
        with self.assertRaises(SchemaError) as ctx:
            schema.apply_overrides(spec, {'limits': {'wall_seconds': 101}})
        self.assertEqual(ctx.exception.code, 'override_not_allowed')

    def test_ids_are_ulid_shaped(self):
        for prefix in ('hd', 'hv', 'run', 'stg', 'apr', 'art'):
            self.assertRegex(schema.new_id(prefix), schema.ID_RE.pattern)
