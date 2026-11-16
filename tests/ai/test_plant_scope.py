"""Plant scope of the direct database reads: every row is tied to its plant through the record that owns it,
and tables without a plant follow the licence of the backend routes that show them."""

import pytest

from ai.apps.cablecore import access, related

pytestmark = pytest.mark.skipif(not related.SCHEMA_DIR.is_dir(), reason='needs the cableCORE Prisma schema')


def route(name: str) -> list[str]:
    path = related._plant_path(name)
    assert path is not None, f'{name} has no plant route'
    return [rel.name for _, rel in path]


def test_rows_follow_the_record_that_owns_them():
    assert route('DesignVersion') == ['config']  # as the backend's PlantScopeService, not via the workflow
    assert route('DesignHistory') == ['design', 'config']
    assert route('ProcessAttributeValue') == ['design', 'config']  # the design's plant, not the attribute definition's
    assert route('PlantAttributeValue') == ['design', 'config']
    assert route('LineItem') == ['enquiryVersion', 'enquiry']
    assert route('Design') == [] and route('Catalogue') == []


def test_condition_is_one_parameter_and_names_only_for_name_tables():
    cond, kind = related.plant_condition('DesignHistory')
    assert kind == 'ids' and cond.count('%s') == 1 and cond.startswith('t.')
    assert related.plant_condition('LookupTable')[1] == 'names'
    assert related.plant_condition('DesignImportRun') is None


def test_tables_without_a_plant_need_a_rule():
    a = access.Access(activePlantId='p1', plants=[access.Plant(id='p1', name='1300')],
                      access={'design': True, 'core': True}, permissions={})
    assert access.table_refusal(a, 'UnitsGlobal', related.has_plant('UnitsGlobal')) is None
    assert access.table_refusal(a, 'DesignImportRun', related.has_plant('DesignImportRun'))  # internal: refused
    assert access.table_refusal(a, 'FileRepository', related.has_plant('FileRepository')) == access.NO_LICENCE  # admin only
    assert access.table_refusal(a, 'Account', True)  # never, whatever the route
