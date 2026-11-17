"""A large response is trimmed to the fields its capability's `returns` names (cc_action results)."""

import json

from ai.apps.cablecore import actions, shape
from ai.apps.cablecore.index import Route

RETURNS = ('data.{designName, isLocked, isLatest, tabs[].subTabs[].attributeValues[].{id (value row id), '
           'plantAttribute.{name, attributeType}, valueString|valueDecimal|valueInt|valueBoolean|valueDateTime|valueDropdown, '
           'dropDownValues}}')


def _row(i, name, dropdown=None):
    return {'id': f'row-{i}', 'designVersionId': 'v', 'valueString': None, 'valueDecimal': None, 'valueInt': None,
            'valueBoolean': None, 'valueDateTime': None, 'valueDropdown': dropdown, 'isLockValue': False,
            'dropDownValues': ['95', '240'] if dropdown else None,
            'plantAttribute': {'id': f'def-{i}', 'name': name, 'attributeType': 'Dropdown' if dropdown else 'String',
                               'attributeDesc': 'x' * 300, 'helpText': 'y' * 300, 'displayOrder': i}}


DESIGN = {'designId': 'd', 'designName': 'MV-22655', 'isLocked': False, 'isLatest': True, 'plantId': 'p',
          'tabs': [{'id': 't', 'tabName': 'General', 'subTabs': [
              {'id': 's', 'subTabName': 'Conductor', 'attributeValues': [_row(i, f'attr{i}') for i in range(40)]
               + [_row(99, 'conductorCSA', '{"valueDropdown":"240"}')]}]}]}


def test_trim_keeps_exactly_what_returns_names():
    out = shape.trim(DESIGN, RETURNS)  # unwrapped response: the leading `data` is matched too
    assert out['designName'] == 'MV-22655' and out['isLatest'] is True
    rows = out['tabs'][0]['subTabs'][0]['attributeValues']
    csa = rows[-1]
    assert csa == {'id': 'row-99', 'plantAttribute': {'name': 'conductorCSA', 'attributeType': 'Dropdown'},
                   'valueDropdown': '{"valueDropdown":"240"}', 'dropDownValues': ['95', '240']}
    assert 'helpText' not in json.dumps(out) and 'designVersionId' not in json.dumps(out)  # definitions and noise gone
    assert len(json.dumps(out)) < len(json.dumps(DESIGN)) / 5


def test_or_alternatives_and_misses():
    returns = 'data.{totalDesigns, designResults[].{designName, status}} or data.{status: queued, jobId}'
    assert shape.trim({'status': 'queued', 'jobId': 'j1', 'noise': 1}, returns) == {'status': 'queued', 'jobId': 'j1'}
    assert shape.trim({'other': 1}, 'data.{id, name}') is None  # names nothing that is there: caller keeps the raw clip


def test_cc_action_result_is_trimmed_only_when_large():
    route = Route(id='design.plant_attributes', method='GET', path='/x', summary='', risk='read',
                  capability={'returns': RETURNS})
    big = actions._result(route, DESIGN)
    assert big.startswith('[trimmed to the fields `returns` names') and 'conductorCSA' in big and 'row-99' in big
    small = actions._result(route, {'designName': 'MV-1'})
    assert small == json.dumps({'designName': 'MV-1'})  # under the clip: untouched
    plain = Route(id='GET /x', method='GET', path='/x', summary='', risk='read')
    assert not actions._result(plain, DESIGN).startswith('[trimmed')  # undescribed routes: plain clip


def test_mcp_bridge_routes_are_not_in_the_catalogue(monkeypatch):
    import asyncio

    from ai.apps.cablecore import api, index

    async def spec(token):
        return {'paths': {'/mcp/edit-plant-design-attribute': {'post': {'operationId': 'McpBridgeController_edit'}},
                          '/design': {'post': {'operationId': 'design.create', 'x-ai': {'summary': 's', 'returns': 'r', 'risk': 'write'}}}}}

    monkeypatch.setattr(api, 'spec', spec)
    asyncio.run(index._build('t'))
    assert index.get('design.create') is not None
    assert index.get('McpBridgeController_edit') is None
