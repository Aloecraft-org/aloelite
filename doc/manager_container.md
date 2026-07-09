make sure you run as root so Fuse can propagate easily

start

``` bash
sudo podman run --rm -d --privileged \
  --device /dev/fuse \
  -v /aloelite-root:/aloelite-root \
  -v /mnt/aloelite:/mnt:rshared \
  -p 8080:8080 \
  --name aloelite aloecraft/aloelite:latest
```

If you need to force unmount stale fuse mount from host

``` bash
sudo fusermount3 -uz /mnt/aloelite/dms0
```

Check mounts inside manager container

``` bash
sudo podman exec aloelite cat /proc/self/mountinfo | grep mnt
```
prints something like:
``` bash
659 654 259:1 /mnt/aloelite /mnt rw,relatime shared:1 - ext4 /dev/root rw,discard,errors=remount-ro,commit=30
517 659 0:67 / /mnt/dms0 rw,nosuid,nodev,relatime shared:423 - fuse aloefuse rw,user_id=0,group_id=0,default_permissions,allow_other
```

# TODO: systemd unit
- need to make volume location shared