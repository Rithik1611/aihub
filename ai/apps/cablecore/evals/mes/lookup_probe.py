"""For jobs that exist: what do mes_get_job and mes_list_jobs(JobCodeContains) each return?"""

import asyncio
import os

from ai import http
from ai.apps.mes import client as mes
from ai.context import TurnContext


async def main() -> None:
    ctx = TurnContext(user_id='p', user_email=os.environ['EVAL_USER_EMAIL'], cc_token='', chat_id='p', wait_answer=None)  # type: ignore[arg-type]
    tools = {t.name: t for t in await mes.tools(ctx)}
    for code in ('POT5246', 'PO007679'):
        e = await tools['mes_list_jobs'].fn(ctx, Status='', JobCodeContains=code, Machine='', ProductionOrder='', Shift='')
        print('EMPTY-FILTERS', code, repr(e[:120]))
        got = await tools['mes_get_job'].fn(ctx, JobCode=code)
        listed = await tools['mes_list_jobs'].fn(ctx, JobCodeContains=code)
        print(f'--- {code}\n get_job : {got[:160]!r}\n list(contains): {listed[:160]!r}')
        for status in ('InProgress', 'Ready', 'New'):
            text = await tools['mes_list_jobs'].fn(ctx, Status=status)
            print(f'   in list[{status}]:', code in text)
    await http.close()


asyncio.run(main())
