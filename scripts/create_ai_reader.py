"""Create or refresh the read-only `ai_reader` login that aihub's DesignCenter tools use (CC_DB_URL).

Idempotent: run it again after a migration adds tables (new tables are NOT readable until granted; that is deliberate).

    PGHOST=... PGPORT=... PGUSER=<superuser> PGPASSWORD=... AI_READER_PASSWORD=<32+ hex chars> \\
        python scripts/create_ai_reader.py [--db taisin] [--verify]

Least privilege:
  * LOGIN only: no superuser/createdb/createrole/replication/bypassrls, every transaction read-only, 30 s statement timeout.
  * SELECT on every table and view of the public schema EXCEPT the deny-list below (credentials, secrets, chat and audit
    payloads) and, on "Account", every column except the secret ones.
  * Any other table whose name or columns look secret-like is NOT granted automatically and is reported for review.
--verify connects AS ai_reader and checks what it can and cannot do (exit code 1 if anything is wrong). It prints no secrets.
"""

import argparse
import asyncio
import os
import re
import sys

import asyncpg

ROLE = 'ai_reader'
DENY_TABLES = {t.lower() for t in (
    # credentials and secrets
    'ErpToken', 'MfaBackupCode', 'MfaMethod', 'PasswordHistory', 'ai_monitor_api_keys', 'AppConfiguration',
    # chat, audit and log tables: personal data and request payloads
    'ChatSession', 'ChatMessage', 'ActivityLog', 'LoginLog', 'AiAgentAudit', 'AiTokenUsageLog',
    'ai_monitor_request_logs', 'ai_monitor_daily_usage', 'Notification', 'ErpJobStatus',
)}
ACCOUNT_SECRET_COLUMNS = {c.lower() for c in (
    'password', 'refreshToken', 'previousRefreshToken', 'previousRefreshTokenValidUntil',
    'resetPasswordVersion', 'lastActiveSessionId', 'sessionVersion', 'tempPasswordExpiresAt',
)}
# mfa alone would also catch settings such as Company.mfaPolicy; the MFA secret tables are on the deny-list anyway
SECRET_LIKE = re.compile(r'password|passwd|secret|credential|api_?key|otp|mfa_?(secret|code|hash|backup)|salt|private|vault|signature|cert|key_hash|token_hash', re.IGNORECASE)


def q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def grant(db: str, password: str) -> None:
    admin = await asyncpg.connect(database='postgres')
    literal = await admin.fetchval('select quote_literal($1::text)', password)
    exists = await admin.fetchval('select 1 from pg_roles where rolname = $1', ROLE)
    flags = 'LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
    await admin.execute(f"{'ALTER' if exists else 'CREATE'} ROLE {ROLE} {flags} PASSWORD {literal}")
    for setting in ("default_transaction_read_only = on", "statement_timeout = '30s'", "lock_timeout = '5s'",
                    "idle_in_transaction_session_timeout = '60s'"):
        await admin.execute(f'ALTER ROLE {ROLE} SET {setting}')
    await admin.execute(f'GRANT CONNECT ON DATABASE {q(db)} TO {ROLE}')
    await admin.close()

    con = await asyncpg.connect(database=db)
    await con.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}')
    await con.execute(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}')
    await con.execute(f'GRANT USAGE ON SCHEMA public TO {ROLE}')
    relations = await con.fetch("select c.relname, c.relkind from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                "where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm') order by 1")
    granted, denied, review = 0, [], []
    for rel in relations:
        name = rel['relname']
        if name.lower() in DENY_TABLES:
            denied.append(name)
            continue
        if name.lower() == 'account':
            columns = [r['column_name'] for r in await con.fetch(
                "select column_name from information_schema.columns where table_schema = 'public' and table_name = $1 order by ordinal_position", name)]
            allowed = [c for c in columns if c.lower() not in ACCOUNT_SECRET_COLUMNS]
            await con.execute(f"GRANT SELECT ({', '.join(q(c) for c in allowed)}) ON public.{q(name)} TO {ROLE}")
            granted += 1
            continue
        secret_columns = [r['column_name'] for r in await con.fetch(
            "select column_name from information_schema.columns where table_schema = 'public' and table_name = $1", name)
            if SECRET_LIKE.search(r['column_name'])]
        if SECRET_LIKE.search(name) or secret_columns:
            review.append(f'{name} {secret_columns}')  # not granted: a person decides
            continue
        await con.execute(f'GRANT SELECT ON TABLE public.{q(name)} TO {ROLE}')
        granted += 1
    await con.close()
    print(f'{ROLE} on {db}: SELECT granted on {granted} relations; denied (deny-list) {len(denied)}; held back for review {len(review)}')
    for r in review:
        print('  review:', r)


async def verify(db: str, password: str) -> bool:
    ok = True

    def report(label: str, passed: bool) -> None:
        nonlocal ok
        ok &= passed
        print(('PASS ' if passed else 'FAIL ') + label)

    async def refused(con: asyncpg.Connection, sql: str) -> bool:
        try:
            await con.execute(sql)
        except (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError):
            return True
        except asyncpg.PostgresError as e:
            print('   unexpected', type(e).__name__)
        return False

    con = await asyncpg.connect(user=ROLE, password=password, database=db)
    report('can read Design', (await con.fetchval('select count(*) from "Design"')) >= 0)
    report('can read non-secret Account columns', (await con.fetchval('select count(id) from "Account"')) >= 0)
    report('cannot read Account.password', await refused(con, 'select password from "Account" limit 1'))
    report('cannot read Account.refreshToken', await refused(con, 'select "refreshToken" from "Account" limit 1'))
    for table in ('AppConfiguration', 'ErpToken', 'MfaMethod', 'PasswordHistory', 'ChatMessage', 'LoginLog'):
        report(f'cannot read {table}', await refused(con, f'select * from {q(table)} limit 1'))
    report('cannot write (insert)', await refused(con, 'insert into "AppConfiguration"(key, value) values (\'x\', \'y\')'))
    report('cannot write (update)', await refused(con, 'update "Design" set "id" = "id" where false'))
    report('cannot create objects', await refused(con, 'create table public.ai_reader_probe(x int)'))
    report('statement timeout is 30 s', (await con.fetchval('show statement_timeout')) == '30s')
    report('transactions are read-only', (await con.fetchval('show default_transaction_read_only')) == 'on')
    report('not a superuser', not await con.fetchval('select rolsuper from pg_roles where rolname = current_user'))
    await con.close()
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='taisin')
    parser.add_argument('--verify', action='store_true', help='only check what ai_reader can and cannot do')
    args = parser.parse_args()
    password = os.environ['AI_READER_PASSWORD']
    if not re.fullmatch(r'[0-9a-f]{32,}', password):
        sys.exit('AI_READER_PASSWORD must be at least 32 lowercase hex characters')
    if not args.verify:
        await grant(args.db, password)
    return 0 if await verify(args.db, password) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
