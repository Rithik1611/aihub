# Open Terminal for the cableCORE assistant. No database client or login: data comes through the backend.
FROM ghcr.io/open-webui/open-terminal:latest
# Multi-user mode runs each user's commands with sudo, which drops the environment: keep only the
# helpers' settings (never OPEN_TERMINAL_API_KEY).
RUN echo 'Defaults env_keep += "CC_API_BASE CC_ROWS_URL ZOEKT_URL PYTHONPATH"' | sudo tee /etc/sudoers.d/cablecore >/dev/null \
 && sudo chmod 0440 /etc/sudoers.d/cablecore
# Home folders persist on a volume, Linux accounts may not: the entry script gives each folder back to its
# existing account and locks orphaned ones until Open Terminal provisions that name (see the script).
# Open Terminal writes an upload through a 600 temp file, then only adds group WRITE: the server (which reaches
# users' files through their group) could not read back what it stored (PermissionError in /files/view). Add
# group read too. The grep fails the build if a new Open Terminal changes that line, so the patch never silently lapses.
RUN FS="$(python3-ot -c 'import open_terminal.utils.fs as f; print(f.__file__)')" \
 && sudo grep -q '"chmod", "g+w", path' "$FS" \
 && sudo sed -i 's/"chmod", "g+w", path/"chmod", "g+rw", path/' "$FS" \
 && sudo find "$(dirname "$FS")/__pycache__" -name 'fs.*.pyc' -delete
COPY cablecore-entry.sh /tmp/cablecore-entry.sh
RUN sudo sh -c "sed 's/\r$//' /tmp/cablecore-entry.sh > /app/cablecore-entry.sh && chmod 755 /app/cablecore-entry.sh"
ENTRYPOINT ["/usr/bin/tini", "--", "/app/cablecore-entry.sh"]
CMD ["run"]
