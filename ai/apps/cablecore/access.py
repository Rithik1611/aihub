"""What the user may do on their active plant, as cableCORE's own access rules decide it (GET /auth/access).

The backend refuses every call the user's licence or restrictions do not allow; this module applies the same
checks before the model plans one, so a request the API would refuse is answered plainly instead of being
attempted, worked around, or shown as an approval card. Direct database reads (cc_get, cc_related, cc_formula)
do not pass the API, so they are gated here by table. The API stays the enforcement point.
"""

import re
import time
from typing import Any

from pydantic import BaseModel

from ai.apps.cablecore import api
from ai.context import TurnContext

# The backend's own refusal messages (AccessInterceptor), so the user reads the same words the app shows.
NO_LICENCE = 'Your licence does not include this feature on the active plant.'
NO_PERMISSION = 'You do not have permission to do this on the active plant.'
READ_ONLY = 'Your access on the active plant is read-only.'
NOT_YOUR_PLANT = 'This record belongs to plant {plant}, which you have no access to.'

TTL_S = 60
_SAFE = ('GET', 'HEAD', 'OPTIONS')
_METHOD_ACTION = {'POST': 'CREATE', 'PUT': 'UPDATE', 'PATCH': 'UPDATE', 'DELETE': 'DELETE'}

# Plain words for the access keys and restrictions, for the per-turn context line.
_AREAS = {'design': 'designs', 'enquiry': 'enquiries and quotations', 'cost': 'cost figures', 'integration': 'ERP integration',
          'production': 'production', 'tenderCrawler': 'tenders', 'electrical': 'electrical calculations',
          'tables': 'lookup tables', 'requestDesk': 'the request desk', 'admin': 'administration'}
_PERMISSIONS = {'readDesignFormula': 'read design formulas', 'editDesignFormula': 'edit design formulas',
                'readEnquiryFormula': 'read enquiry formulas', 'editEnquiryFormula': 'edit enquiry formulas',
                'readQuotationFormula': 'read quotation formulas', 'editQuotationFormula': 'edit quotation formulas',
                'editLookupTable': 'edit lookup tables', 'deleteLookupTable': 'delete lookup tables',
                'updateProcessAttribute': 'change process attributes', 'downloadReport': 'download reports'}


class Plant(BaseModel):
    id: str
    name: str
    modules: list[str] = []


class Access(BaseModel):
    activePlantId: str | None = None
    plants: list[Plant] = []
    role: str = ''
    modules: list[str] = []
    access: dict[str, bool] = {}
    write: dict[str, bool] = {}
    permissions: dict[str, bool] = {}
    crud: dict[str, list[str]] = {}

    @property
    def active_plant(self) -> Plant | None:
        return next((p for p in self.plants if p.id == self.activePlantId), None)

    def plant(self, name: str) -> Plant | None:
        """A plant the user holds, by name (case-insensitive) or id."""
        name = name.strip().lower()
        return next((p for p in self.plants if name in (p.name.lower(), p.id)), None)

    def allows(self, *keys: str) -> bool:
        return any(self.access.get(k) for k in keys)

    def permits(self, *keys: str) -> bool:
        return any(self.permissions.get(k) for k in keys)


_cache: dict[str, tuple[float, Access | None]] = {}


async def of(token: str) -> Access | None:
    """The user's access, cached for TTL_S. None when the backend has no /auth/access (older build): the API
    alone then enforces."""
    hit = _cache.get(token)
    if hit and hit[0] > time.time():
        return hit[1]
    status, data = await api.call('GET', '/auth/access', token, timeout_s=20)
    found = Access.model_validate(api.unwrap(data)) if status == 200 and isinstance(api.unwrap(data), dict) else None
    if len(_cache) > 2000:
        _cache.clear()
    _cache[token] = (time.time() + TTL_S, found)
    return found


def route_refusal(a: Access, method: str, rule: dict[str, Any] | None) -> str | None:
    """The AccessInterceptor's decision for a route's `x-access`, or None when the call may go ahead."""
    if not rule:
        return None
    keys = rule.get('keys') or []
    if keys and not a.allows(*keys):
        return NO_LICENCE
    if (permissions := rule.get('permissions')) and not a.permits(*permissions):
        return NO_PERMISSION
    if method.upper() in _SAFE or rule.get('allowReadOnly'):
        return None
    write = rule.get('write') or keys
    if write and not a.allows(*write):
        return NO_LICENCE
    if write and not any(a.write.get(k) for k in write):
        return READ_ONLY
    crud = rule.get('crud') or {}
    action = crud.get('action', _METHOD_ACTION.get(method.upper())) if crud else None
    if action and action not in a.crud.get(crud.get('resource', ''), []):
        return NO_PERMISSION
    return None


# Tables read straight from the database, by Prisma model name: first match wins. 'never' = not readable through
# the assistant at all (credentials, other users' chats and searches); otherwise the access keys (any one) or the
# 'perm:' restrictions (any one) the user needs, as for the API routes over the same data.
_TABLES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [(re.compile(p), keys) for p, keys in [
    ((r'^(Account|Mfa\w*|PasswordHistory|LoginLog|ErpToken|AiMonitorApiKey|TenderMailConfig|ChatSession|ChatMessage|'
      r'SearchHistory|AppConfiguration|Organization)$'), ('never',)),
    ((r'^(AccountPlantModule|TabsPermission|AttributesPermission|DesignPermission|EnquiryPermission|UserGroups|ActivityLog|'
      r'AiTokenUsageLog|AiUsage\w*|AiMonitor\w*|User\w*Activity|ModuleLoggingConfig|Company)$'), ('admin',)),
    (r'^(Carbon\w*|ManufacturedEntry)$', ('design', 'core')),
    (r'^(Cost\w*|Costing\w*|EnquiryCost\w*|QuotationCost\w*|\w*Rollup\w*|\w*PriceDetails?)$', ('cost',)),
    (r'^Formula(Cable|Plant|Process|Bom|Resource)\w*$', ('perm:readDesignFormula',)),
    (r'^Formula(Enquiry|LineType)\w*$', ('perm:readEnquiryFormula',)),
    (r'^FormulaQuotation\w*$', ('perm:readQuotationFormula',)),
    (r'^Formula(Library\w*|History)$', ('perm:readDesignFormula', 'perm:readEnquiryFormula', 'perm:readQuotationFormula')),
    (r'^(Enquiry\w*|Quotation\w*|LineItem\w*|LineType\w*|Customer\w*|Project|ContactPerson|DesignDeskEnquiry|Comment)$', ('enquiry',)),
    (r'^Tender\w*$', ('tenderCrawler',)),
    (r'^Erp\w*$', ('integration',)),
    (r'^(CurrentRating\w*|BuriedConfiguration|AirConfiguration|DuctConfiguration)$', ('electrical', 'design')),
    (r'^DesignRequest$', ('requestDesk', 'design')),
]]


def table_refusal(a: Access, model: str) -> str | None:
    """Why the user may not read this table through the assistant, or None. Unlisted tables are design data."""
    keys = next((k for p, k in _TABLES if p.match(model)), ('design', 'core'))
    if keys == ('never',):
        return f'{model} is not readable through the assistant.'
    perms = [k.removeprefix('perm:') for k in keys if k.startswith('perm:')]
    if perms:
        return None if a.permits(*perms) else NO_PERMISSION
    return None if a.allows(*keys) else NO_LICENCE


def plant_ids(a: Access, plant: str) -> tuple[list[str], str]:
    """Plants a read covers, and how to say it: '' = the active plant, 'all' = every plant the user holds, or one
    plant by name. Raises ValueError for a plant the user does not hold."""
    if plant.strip().lower() == 'all':
        return [p.id for p in a.plants], 'all your plants'
    if plant.strip():
        found = a.plant(plant)
        if not found:
            raise ValueError(f'You have no access to plant {plant!r}. Your plants: {", ".join(p.name for p in a.plants)}.')
        return [found.id], f'plant {found.name}'
    active = a.active_plant
    return ([active.id], f'plant {active.name} (your active plant)') if active else ([p.id for p in a.plants], 'all your plants')


def plant_name(a: Access, plant: str) -> str:
    """The plant name a search filters on: '' = the active plant, 'all' = none (every plant the user can access)."""
    if plant.strip().lower() == 'all':
        return ''
    if plant.strip():
        found = a.plant(plant)
        if not found:
            raise ValueError(f'You have no access to plant {plant!r}. Your plants: {", ".join(p.name for p in a.plants)}.')
        return found.name
    return a.active_plant.name if a.active_plant else ''


def context_line(a: Access, email: str) -> str:
    """What the model needs to know about the user this turn, in one bracketed line (~60 tokens)."""
    active = a.active_plant
    others = [p.name for p in a.plants if p.id != a.activePlantId]
    parts = [f'Acting for {email} on plant {active.name if active else "(none set)"}'
             + (f'; other plants they hold: {", ".join(others[:12])}' if others else '')]
    missing = [w for k, w in _AREAS.items() if not a.access.get(k)]
    read_only = [w for k, w in _AREAS.items() if a.access.get(k) and not a.write.get(k)]
    denied = [w for k, w in _PERMISSIONS.items() if not a.permissions.get(k)]
    if missing:
        parts.append(f'Not licensed here: {", ".join(missing)}')
    if read_only:
        parts.append(f'Read-only here: {", ".join(read_only)}')
    if denied:
        parts.append(f'Not permitted: {", ".join(denied)}')
    if not (missing or read_only or denied):
        parts.append('Full access on this plant')
    return '[' + '. '.join(parts) + '.]'


async def turn_context(ctx: TurnContext) -> str:
    """The app's per-turn context, added to the user's message (not the system prompt, which stays cached)."""
    if not ctx.cc_token:
        return ''
    try:
        found = await of(ctx.cc_token)
    except Exception:  # noqa: BLE001 - the turn goes on without it; the API still enforces
        return ''
    return context_line(found, ctx.user_email) if found else ''


if __name__ == '__main__':
    full = Access(activePlantId='p1', plants=[Plant(id='p1', name='1300'), Plant(id='p2', name='Demo')], role='USER',
                  access={'design': True, 'enquiry': False, 'anyModule': True}, write={'design': False},
                  permissions={'readDesignFormula': True}, crud={'DESIGN': ['UPDATE']})
    assert route_refusal(full, 'GET', {'keys': ['enquiry']}) == NO_LICENCE
    assert route_refusal(full, 'GET', {'keys': ['design']}) is None
    assert route_refusal(full, 'POST', {'keys': ['design']}) == READ_ONLY
    assert route_refusal(full, 'POST', {'keys': ['design'], 'allowReadOnly': True}) is None
    assert route_refusal(full, 'GET', {'keys': []}) is None
    assert route_refusal(full, 'GET', {'keys': ['design'], 'permissions': ['editDesignFormula']}) == NO_PERMISSION
    full.write['design'] = True
    assert route_refusal(full, 'POST', {'keys': ['design'], 'crud': {'resource': 'DESIGN'}}) == NO_PERMISSION
    assert route_refusal(full, 'PATCH', {'keys': ['design'], 'crud': {'resource': 'DESIGN'}}) is None
    assert route_refusal(full, 'POST', {'keys': ['design'], 'crud': {'resource': 'DESIGN', 'action': None}}) is None
    assert table_refusal(full, 'Account') and table_refusal(full, 'ChatMessage')
    assert table_refusal(full, 'Quotation') == NO_LICENCE and table_refusal(full, 'CostingVersion') == NO_LICENCE
    assert table_refusal(full, 'CarbonRollupBreakdown') is None and table_refusal(full, 'DesignHistory') is None and table_refusal(full, 'FormulaPlantHeaders') is None
    assert table_refusal(full, 'FormulaEnquiryHeaders') == NO_PERMISSION
    assert plant_ids(full, '') == (['p1'], 'plant 1300 (your active plant)')
    assert plant_ids(full, 'demo')[0] == ['p2'] and plant_ids(full, 'all')[0] == ['p1', 'p2']
    assert plant_name(full, '') == '1300' and plant_name(full, 'all') == ''
    try:
        plant_ids(full, '9999')
        raise AssertionError
    except ValueError:
        pass
    assert 'Not licensed here: enquiries and quotations' in context_line(full, 'u@x')
    print('access self-check OK')
