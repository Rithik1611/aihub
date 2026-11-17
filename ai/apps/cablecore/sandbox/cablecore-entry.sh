#!/bin/bash
# Runs before Open Terminal on every container start. Home folders persist on a volume; Linux accounts do not
# always (a re-created container starts with none, a restarted one keeps them). So, per /home/<name>:
#   - the account exists  -> the folder is its own: owner <name>, mode 2770 (Open Terminal's layout: the server
#                            user reaches it through the user's group). Re-asserted on every start, so a folder
#                            can never be left unusable (uploads then failed with 500/403).
#   - no account          -> the folder is from an earlier container whose uids may be reused by someone else:
#                            lock it to root until Open Terminal provisions that name (it re-owns it with chown -R).
for d in /home/*/; do
  name="$(basename "$d")"
  [ "$name" = user ] && continue
  if id -u "$name" >/dev/null 2>&1; then
    sudo chown "$name:$name" "$d" && sudo chmod 2770 "$d"
  else
    sudo chown root:root "$d" && sudo chmod 700 "$d"
  fi
done
exec /app/entrypoint.sh "$@"
