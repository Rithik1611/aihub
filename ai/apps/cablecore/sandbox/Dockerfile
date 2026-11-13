# Open Terminal plus what the cableCORE assistant needs to reach its data.
FROM ghcr.io/open-webui/open-terminal:latest
RUN sudo apt-get update -qq && sudo apt-get install -y -qq postgresql-client >/dev/null \
 && sudo rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -q "psycopg[binary]"
