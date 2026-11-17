"""The capability descriptor comes from the OpenAPI operation's x-ai extension (no /ai/capabilities call)."""

import asyncio

from ai.apps.cablecore import api, index

SPEC = {
    'components': {'schemas': {
        'BulkAttributeUpdateDto': {'type': 'object', 'required': ['attributeValues'],
                                   'properties': {'attributeValues': {'type': 'array', 'items': {'$ref': '#/components/schemas/Value'}}}},
        'Value': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'value': {}}},
    }},
    'paths': {
        '/attribute-value/plant-update/{designId}': {'patch': {
            'operationId': 'design.update_plant_attributes',
            'parameters': [{'name': 'designId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
            'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/BulkAttributeUpdateDto'}}}},
            'x-ai': {'summary': 'Set design header values.', 'returns': 'data.{count}', 'risk': 'write',
                     'fields': {'note': {'type': 'string', 'description': 'JSON text'}}},
        }},
        '/design/{id}/execute-bottom-up': {'post': {
            'operationId': 'design.apply_latest_changes',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
            'x-ai': {'summary': 'Recalculate.', 'returns': 'data', 'risk': 'write',
                     'job': {'status': 'design.execution_status', 'idPath': 'data.jobId', 'idParam': 'jobId',
                             'statusPath': 'status', 'done': ['COMPLETED'], 'failed': ['FAILED']}},
        }},
        '/plant': {'get': {'operationId': 'PlantController_list', 'description': 'All plants'}},
    },
}


def test_x_ai_becomes_the_capability_and_capabilities_is_not_called(monkeypatch):
    async def spec(token):
        return SPEC

    async def capabilities(token):
        raise AssertionError('/ai/capabilities must not be called when the spec carries x-ai')

    monkeypatch.setattr(api, 'spec', spec)
    monkeypatch.setattr(api, 'capabilities', capabilities)
    asyncio.run(index._build('t'))

    update = index.get('design.update_plant_attributes')
    cap = update.capability
    assert update.risk == 'write' and update.summary == 'Set design header values.'
    assert cap['input']['path']['required'] == ['designId']
    body = cap['input']['body']['properties']
    assert body['attributeValues']['items']['properties']['id'] == {'type': 'string'}  # $ref resolved
    assert body['note']['description'] == 'JSON text'  # fields override merged
    assert 'validate' not in cap

    run = index.get('design.apply_latest_changes').capability
    assert run['job']['idPath'] == 'data.jobId'

    assert index.get('PlantController_list').capability is None  # undescribed routes stay plain
    assert index.validate(update, {}, {}, {'attributeValues': [{'id': 'x', 'value': 1}]}) == ['path.designId: required']
