"""Bundle export/import without secrets (INTERFACE_SPEC §10).

Bundle: ``{format, exported_utc, definitions:[{name, description, versions:[{spec_sha256, spec}]}], sha256}``.
``sha256`` covers ``{format, definitions}`` only (no ids, no timestamps), so export -> import -> export is hash-equal.
"""
from __future__ import annotations

from aether.studio import scanner, schema
from aether.studio.schema import SchemaError, canonical_json, sha256_hex

FORMAT = 'aether.studio.bundle/1'
MAX_DEFINITIONS = 100


def bundle_hash(definitions) -> str:
    return sha256_hex(canonical_json({'format': FORMAT, 'definitions': definitions}))


def export_bundle(store, definition_ids=None) -> dict:
    ids = definition_ids
    if ids is None:
        ids = [d['id'] for d in store.list_definitions()]
    definitions = []
    for def_id in ids:
        definition = store.get_definition(def_id)
        versions = [{'spec_sha256': v['spec_sha256'], 'spec': scanner.strip_secret_fields(v['spec'])}
                    for v in store.definition_versions(def_id)]
        definitions.append({'name': definition['name'], 'description': definition['description'], 'versions': versions})
    return {'format': FORMAT, 'exported_utc': store.now(), 'definitions': definitions,
            'sha256': bundle_hash(definitions)}


def _check(obj, field, required, allowed=()):
    if not isinstance(obj, dict):
        raise SchemaError('schema_invalid', field, 'object required')
    unknown = set(obj) - set(required) - set(allowed)
    if unknown:
        raise SchemaError('unknown_field', f'{field}/{sorted(unknown)[0]}', 'unknown field')
    missing = set(required) - set(obj)
    if missing:
        raise SchemaError('schema_invalid', f'{field}/{sorted(missing)[0]}', 'required field missing')


def import_bundle(store, bundle, *, provider='none') -> dict:
    """Validate everything first, then insert all-or-nothing. New ids; archived=false; idempotent by (name, sha)."""
    _check(bundle, '', ('format', 'definitions', 'sha256'), ('exported_utc',))
    if bundle['format'] != FORMAT:
        raise SchemaError('schema_invalid', '/format', 'unsupported bundle format')
    hit = scanner.find_in_value(bundle)
    if hit:
        raise SchemaError('import_secret_detected', hit['field'], f'secret-like content ({hit["rule"]}); value not shown')
    definitions = bundle['definitions']
    if not isinstance(definitions, list) or not 1 <= len(definitions) <= MAX_DEFINITIONS:
        raise SchemaError('schema_invalid', '/definitions', f'1..{MAX_DEFINITIONS} definitions required')
    items = []
    for i, entry in enumerate(definitions):
        field = f'/definitions/{i}'
        _check(entry, field, ('name', 'versions'), ('description',))
        name = schema._str(entry['name'], f'{field}/name', 1, 80)
        description = schema._str(entry.get('description', ''), f'{field}/description', 0, 2000)
        versions = entry['versions']
        if not isinstance(versions, list) or not 1 <= len(versions) <= 100:
            raise SchemaError('schema_invalid', f'{field}/versions', '1..100 versions required')
        specs = []
        for j, version in enumerate(versions):
            vfield = f'{field}/versions/{j}'
            _check(version, vfield, ('spec', 'spec_sha256'))
            spec = schema.validate_spec(version['spec'], provider=provider)
            if schema.spec_sha256(spec) != version['spec_sha256']:
                raise SchemaError('schema_invalid', f'{vfield}/spec_sha256', 'spec hash mismatch')
            specs.append(spec)
        items.append((name, description, specs))
    if bundle_hash(definitions) != bundle['sha256']:
        raise SchemaError('schema_invalid', '/sha256', 'bundle hash mismatch')
    created, skipped = store.import_definitions(items)
    return {'created': created, 'skipped': skipped, 'sha256': bundle['sha256']}
