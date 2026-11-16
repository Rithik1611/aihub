# Open Terminal for the cableCORE assistant. No database client or login: data comes through the backend.
FROM ghcr.io/open-webui/open-terminal:latest
# Multi-user mode runs each user's commands with sudo, which drops the environment: keep only the
# helpers' settings (never OPEN_TERMINAL_API_KEY).
RUN echo 'Defaults env_keep += "CC_API_BASE CC_ROWS_URL ZOEKT_URL PYTHONPATH"' | sudo tee /etc/sudoers.d/cablecore >/dev/null \
 && sudo chmod 0440 /etc/sudoers.d/cablecore
# The per-user Linux accounts are re-created after a restart, possibly with another user's old uid, while the
# home folders persist: lock every home to root until Open Terminal re-provisions its owner (it re-owns the
# folder then), so nobody inherits someone else's files.
RUN printf '#!/bin/bash\nfor d in /home/*/; do [ "$d" = /home/user/ ] || { sudo chown root:root "$d"; sudo chmod 700 "$d"; }; done\nexec /app/entrypoint.sh "$@"\n' \
  | sudo tee /app/cablecore-entry.sh >/dev/null && sudo chmod 755 /app/cablecore-entry.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/app/cablecore-entry.sh"]
CMD ["run"]
