# GNU coreutils — the profile everything is written against.
#
# The control, not the interesting case. Its value is that a failure here is a
# CLV bug rather than a portability gap, which is what makes the Alpine run's
# failures readable.
FROM docker.io/library/debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && useradd -m -s /bin/sh clvtest

RUN mkdir -p /home/clvtest/.ssh \
 && chmod 700 /home/clvtest/.ssh \
 && chown clvtest:clvtest /home/clvtest/.ssh \
 && printf '%s\n' \
      'PermitRootLogin no' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PubkeyAuthentication yes' \
      'AllowUsers clvtest' \
    >> /etc/ssh/sshd_config

COPY authorized_keys /home/clvtest/.ssh/authorized_keys
RUN chmod 600 /home/clvtest/.ssh/authorized_keys \
 && chown clvtest:clvtest /home/clvtest/.ssh/authorized_keys \
 && mkdir -p /srv/logs \
 && chown clvtest:clvtest /srv/logs

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
