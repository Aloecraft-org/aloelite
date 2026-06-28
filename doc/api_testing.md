``` sh
# Create and mount a volume
curl -s -X POST http://localhost:8080/volumes \
  -H 'Content-Type: application/json' \
  -d '{"name":"test"}' | tee /tmp/vol.json

VID=$(jq -r .id /tmp/vol.json)

curl -s -X POST http://localhost:8080/volumes/$VID/mount \
  -H 'Content-Type: application/json' -d '{}'

# Confirm the FUSE mount is visible on the host
ls /mnt/aloelite/$VID

# Write something via FUSE, read it back
echo "hello" | sudo tee /mnt/aloelite/$VID/hello.txt
sudo cat /mnt/aloelite/$VID/hello.txt

# Backup endpoints
curl -s http://localhost:8080/volumes/$VID/stat | jq
curl -s -X POST http://localhost:8080/volumes/$VID/checkpoint | jq
curl -s http://localhost:8080/volumes/$VID/export -o /tmp/snap.sqlite
sqlite3 /tmp/snap.sqlite .tables   # should open clean

# Unmount and delete
curl -s -X DELETE http://localhost:8080/volumes/$VID/mount
curl -s -X DELETE http://localhost:8080/volumes/$VID
```

``` sh
mgodf@mgodf-X1-Nano-Gen-2:~/dpub/sqlite$ sudo podman run --rm --privileged   -v /aloelite-root:/aloelite-root   -v /mnt/aloelite:/mnt:rshared   --device /dev/fuse   -p 8080:8080   aloelite-manager
[preflight] OK   /dev/fuse present
[preflight] OK   CAP_SYS_ADMIN available
[preflight] OK   /aloelite-root writable
[preflight] OK   /mnt rshared
[preflight] OK   fusermount3 present
[preflight] OK   allow_other permitted
[preflight] OK   VolumeStore readable/writable
[preflight] all fatal checks passed.
 * Serving Flask app 'manager.api'
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:8080
 * Running on http://10.88.0.5:8080
Press CTRL+C to quit
10.88.0.1 - - [28/Jun/2026 20:31:51] "POST /volumes HTTP/1.1" 201 -
10.88.0.1 - - [28/Jun/2026 20:31:52] "POST /volumes/f8901b0f7c714264ae5bd1ab24383a7d/mount HTTP/1.1" 200 -
10.88.0.1 - - [28/Jun/2026 20:31:59] "GET /volumes/f8901b0f7c714264ae5bd1ab24383a7d/stat HTTP/1.1" 200 -
10.88.0.1 - - [28/Jun/2026 20:31:59] "POST /volumes/f8901b0f7c714264ae5bd1ab24383a7d/checkpoint HTTP/1.1" 200 -
10.88.0.1 - - [28/Jun/2026 20:31:59] "GET /volumes/f8901b0f7c714264ae5bd1ab24383a7d/export HTTP/1.1" 200 -
10.88.0.1 - - [28/Jun/2026 20:31:59] "DELETE /volumes/f8901b0f7c714264ae5bd1ab24383a7d/mount HTTP/1.1" 204 -
10.88.0.1 - - [28/Jun/2026 20:31:59] "DELETE /volumes/f8901b0f7c714264ae5bd1ab24383a7d HTTP/1.1" 204 -
10.88.0.1 - - [28/Jun/2026 20:32:51] "DELETE /volumes/f8901b0f7c714264ae5bd1ab24383a7d HTTP/1.1" 404 -
```