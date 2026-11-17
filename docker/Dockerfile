# Based on the official AWS Emulator Docker instructions:
# https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-docker.html
# No prebuilt image is published; this installs the LTS RPM into Amazon Linux 2023.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

# x86_64 or aarch64 (Apple silicon / Graviton)
ARG EMULATOR_ARCH=x86_64

ADD https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator-${EMULATOR_ARCH}.rpm /tmp/aws-emulator.rpm
RUN rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator

USER 1000
ENV HOME=/home/emulator
# Telemetry off by default for every run of this image (the emulator's own default is on).
# The emulator treats this env var as an opt-out that `--telemetry on` can't override; to opt in, run the
# container with `-e EMULATOR_TELEMETRY=on` (scripts/emulator-up.sh: `EMULATOR_TELEMETRY=on make up`).
ENV EMULATOR_TELEMETRY=off
EXPOSE 9297
# -b 0.0.0.0 so Docker can forward the port; the host side is bound to 127.0.0.1 only.
ENTRYPOINT ["aws-emulator", "-b", "0.0.0.0"]
