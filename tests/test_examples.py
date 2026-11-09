"""Example harnesses must validate against the shipped schema and be DEMO-labelled mock specs."""
import json
from pathlib import Path
import unittest

from aether.studio import schema

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / 'examples').glob('*.json'))


class ExampleTests(unittest.TestCase):
    def test_examples_exist(self):
        names = {p.name for p in EXAMPLES}
        self.assertTrue({'demo-harness.json', 'demo-harness-failing.json', 'demo-harness-cancellable.json',
                 'demo-harness-dag.json'} <= names)

    def test_each_example_is_valid_demo_mock(self):
        for path in EXAMPLES:
            with self.subTest(example=path.name):
                body = json.loads(path.read_text())
                self.assertEqual(set(body), {'name', 'description', 'spec'})
                self.assertIn('DEMO', body['name'])
                self.assertIn('DEMO', body['description'])
                spec = schema.validate_spec(body['spec'])
                self.assertEqual(spec['executor']['kind'], 'mock')
                self.assertTrue(spec['executor']['demo'])
                self.assertIn('DEMO', spec['mission'])
                self.assertEqual(spec['limits']['tokens'], 0)

    def test_failing_and_cancellable_variants(self):
        steps = lambda name: [s['executor_step'] for s in json.loads((EXAMPLES[0].parent / name).read_text())['spec']['stages']]
        self.assertTrue(any(s.startswith('mock:fail:') for s in steps('demo-harness-failing.json')))
        self.assertIn('mock:hang', steps('demo-harness-cancellable.json'))

    def test_main_example_round_trips_through_store(self):
        from tests.studio import support
        body = json.loads((EXAMPLES[0].parent / 'demo-harness.json').read_text())
        store = support.store(self)
        created = store.create_definition(body['name'], body['description'], schema.validate_spec(body['spec']))
        self.assertEqual(created['latest_version'], 1)


if __name__ == '__main__':
    unittest.main()

    def test_dag_example_has_planner_workers_reviewer(self):
        spec = schema.validate_spec(json.loads((EXAMPLES[0].parent / 'demo-harness-dag.json').read_text())['spec'])
        roles = {s['key']: schema.stage_role(s) for s in spec['stages']}
        self.assertEqual(sorted(roles.values()), ['planner', 'reviewer', 'worker', 'worker'])
        reviewer = [s for s in spec['stages'] if schema.stage_role(s) == 'reviewer'][0]
        self.assertEqual(len(reviewer['needs']), 2)
        self.assertTrue(reviewer['executor_step'].startswith('mock:verdict:'))

    def test_dag_example_runs_to_success_with_a_recorded_verdict(self):
        from tests.studio import support
        body = json.loads((EXAMPLES[0].parent / 'demo-harness-dag.json').read_text())
        store = support.store(self)
        support.runner(self, store)
        run_id = support.submit(store, spec=body['spec'])
        self.assertEqual(support.wait_status(store, run_id, 'succeeded', 'failed')['status'], 'succeeded')
        types = [e['type'] for e in support.all_events(store, run_id)]
        self.assertIn('review.verdict', types)
