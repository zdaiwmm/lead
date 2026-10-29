FROM postgres:18@sha256:979bccfde35445db618c65107d27d08c6c21339d8dc3ad4c830c6633947be0e4
RUN --mount=type=secret,id=lead_ca,required=false,mode=0444 \
    rm -f /etc/apt/sources.list.d/pgdg.* && \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    if [ -s /run/secrets/lead_ca ]; then echo 'Acquire::https::CaInfo "/run/secrets/lead_ca";' > /etc/apt/apt.conf.d/99lead; fi && \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update && \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 install -y --no-install-recommends python3 bubblewrap && \
    rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99lead
RUN useradd --uid 1000 --create-home browser
COPY scripts/browser_namespace.py /opt/lead/browser_namespace.py
COPY tests/namespace_probe.py /opt/lead/namespace_probe.py
USER 1000:1000
WORKDIR /home/browser
ENTRYPOINT ["/usr/bin/python3", "/opt/lead/namespace_probe.py"]
