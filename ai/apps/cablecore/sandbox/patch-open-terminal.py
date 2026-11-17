"""Build-time patch of Open Terminal's ensure_os_user (run with python3-ot in the sandbox Dockerfile).

When the user's Linux account already exists it returns at once, without checking that the home folder is the
account's. A folder can be another uid's: the account was re-created with a new uid while the folder (on the
volume) kept the old one. Every upload then fails (mkdir as the user: 500). Re-own such a folder on the spot.
Fails the build if the line it patches is not there exactly once, so a new Open Terminal cannot skip it silently.
"""

import pathlib

import open_terminal.utils.user_isolation as isolation

path = pathlib.Path(isolation.__file__)
source = path.read_text(encoding='utf-8')
old = '        return pw.pw_dir\n'
new = (
    '        # cableAI patch: a home folder left from an earlier uid of this account is re-owned\n'
    '        if os.path.isdir(pw.pw_dir) and os.stat(pw.pw_dir).st_uid != pw.pw_uid:\n'
    '            _run_privileged(["chown", "-R", f"{username}:{username}", pw.pw_dir])\n'
    '            _run_privileged(["chmod", "2770", pw.pw_dir])\n'
    '        return pw.pw_dir\n'
)
if source.count(old) != 1 or 'def _run_privileged' not in source or 'import os' not in source:
    raise SystemExit('open_terminal.utils.user_isolation changed: review patch-open-terminal.py')
path.write_text(source.replace(old, new), encoding='utf-8')
for cached in path.parent.joinpath('__pycache__').glob('user_isolation.*.pyc'):
    cached.unlink()
print('patched', path)
