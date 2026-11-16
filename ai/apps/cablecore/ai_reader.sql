-- The assistant's database login (ai_reader): read-only, and never credentials, secrets or other users' chats.
-- Run by the database owner (not ai_reader) once per database and again after new sensitive tables appear;
-- safe to re-run. Creating the role (production) is separate, with a password from the secret store:
--   CREATE ROLE ai_reader LOGIN PASSWORD '...';
--   GRANT USAGE ON SCHEMA public TO ai_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_reader;
--   ALTER ROLE ai_reader SET statement_timeout = '30s';
-- Our code already refuses these tables (access._TABLES 'never'); this makes the database refuse them too,
-- whoever holds the login.
DO $$
DECLARE
  t text;
  safe text;
BEGIN
  FOREACH t IN ARRAY ARRAY['PasswordHistory', 'MfaMethod', 'MfaBackupCode', 'ErpToken', 'LoginLog',
                           'ai_monitor_api_keys', 'AiMonitorApiKey', 'TenderMailConfig', 'AppConfiguration',
                           'ChatSession', 'ChatMessage', 'SearchHistory', 'Organization'] LOOP
    IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
      EXECUTE format('REVOKE ALL ON public.%I FROM ai_reader', t);
    END IF;
  END LOOP;

  -- Account: names and ids stay readable (who created what); passwords, tokens and reset fields do not.
  SELECT string_agg(format('%I', column_name), ', ') INTO safe
    FROM information_schema.columns
   WHERE table_schema = 'public' AND table_name = 'Account'
     AND column_name !~* '(passw|token|secret|reset|otp|mfa)';
  EXECUTE 'REVOKE ALL ON public."Account" FROM ai_reader';
  EXECUTE format('GRANT SELECT (%s) ON public."Account" TO ai_reader', safe);
END $$;
