# Adapted from zulip/docker-zulip (https://github.com/zulip/docker-zulip),
# Copyright the Zulip contributors, Apache License 2.0. Unlike upstream, the
# release tarball is built from this checkout instead of a fresh git clone,
# so the image carries the fork's commits.
#
# Build from the repository root, with a clean committed tree:
#   docker build -f docker/Dockerfile -t foss-devstack/zulip:dev .
FROM ubuntu:24.04 AS base

ENV LANG="C.UTF-8"

# hadolint ignore=DL3005,DL3008,DL3009
RUN apt-get -q update && \
    apt-get -q dist-upgrade -y && \
    DEBIAN_FRONTEND=noninteractive \
    apt-get -q install --no-install-recommends -y ca-certificates git locales python3 sudo tzdata && \
    touch /var/mail/ubuntu && chown ubuntu /var/mail/ubuntu && userdel -r ubuntu && \
    useradd -d /home/zulip -m zulip -u 1000

FROM base AS build

RUN echo 'zulip ALL=(ALL:ALL) NOPASSWD:ALL' >> /etc/sudoers

USER zulip
WORKDIR /home/zulip/zulip

COPY --chown=zulip:zulip . .

RUN SKIP_VENV_SHELL_WARNING=1 ./tools/provision --build-release-tarball-only && \
    uv run --no-sync ./tools/build-release-tarball docker && \
    mv /tmp/tmp.*/zulip-server-docker.tar.gz /tmp/zulip-server-docker.tar.gz

FROM base

ENV DATA_DIR="/data"

LABEL org.opencontainers.image.source="https://github.com/Pressingly/zulip"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.title="Zulip Server (Moneta FOSS bundle)"

COPY --from=build /tmp/zulip-server-docker.tar.gz /root/
COPY docker/bake-zulip-conf.sh /root/bake-zulip-conf.sh

WORKDIR /root
RUN dpkg-divert --add --rename /etc/init.d/nginx && \
    ln -s /bin/true /etc/init.d/nginx && \
    mkdir -p "$DATA_DIR" && \
    tar -xf zulip-server-docker.tar.gz && \
    rm -f zulip-server-docker.tar.gz && \
    mv zulip-server-docker zulip && \
    /root/zulip/scripts/setup/install --hostname="docker-zulip.local" --email="docker-zulip" \
      --puppet-classes="zulip::profile::docker" --postgresql-version=17 && \
    /root/bake-zulip-conf.sh && \
    rm -f /etc/zulip/zulip-secrets.conf /etc/zulip/settings.py /root/bake-zulip-conf.sh && \
    touch /.dockerenv && \
    apt-get -qq autoremove --purge -y && \
    apt-get -qq clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY docker/entrypoint.sh /sbin/entrypoint.sh

VOLUME ["$DATA_DIR"]
EXPOSE 80

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=300s \
    CMD ["curl", "-sfL", "http://localhost/health"]

ENTRYPOINT ["/sbin/entrypoint.sh"]
CMD ["app:run"]
