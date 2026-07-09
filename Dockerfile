FROM python:3.12-slim

# FUSE userspace + the fusermount3 binary the manager shells out to.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fuse3 libfuse3-dev pkg-config gcc\
 && rm -rf /var/lib/apt/lists/*

# Allow non-root (consumer) UIDs to access manager-owned FUSE mounts. Without
# this, consumer containers cannot read the bind-mounted volume.
RUN echo "user_allow_other" >> /etc/fuse.conf

WORKDIR /app
COPY pyproject.toml ./
COPY aloelite/ ./aloelite/
COPY manager/ ./manager/
COPY config/ ./config/
COPY sql/ ./sql/

RUN pip install --no-cache-dir ".[fuse]"

# Backing files + volume metadata live here (mount a volume over it).
VOLUME ["/aloelite-root"]
EXPOSE 8080

# Must be run --privileged (or --cap-add SYS_ADMIN), with --device /dev/fuse
# and -v /mnt/aloelite:/mnt:rshared. See doc/volume_manager.md.
CMD ["python3", "-m", "manager"]