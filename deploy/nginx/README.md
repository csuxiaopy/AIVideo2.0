# Server Nginx entry

Public entry: http://172.16.166.229 (port 80). Python remains on port 8100.
The existing BaoTa Nginx loads `aivideo.conf` from
`/www/server/panel/vhost/nginx/`. Other virtual hosts are unchanged.

Static builds are copied from the running app container, preserving the exact
deployed frontend rather than using the developer checkout. After each future
container update, run:

```sh
sh /data/yolo_vlm_monitor/deploy/nginx/publish-static.sh
```

Hashed assets have a one-year immutable cache; HTML revalidates and the brand
icon has a one-hour cache. API, evidence and WebSocket requests go through the
original authentication and are never proxy-cached. The metrics endpoint is
restricted to local access at this entry.

The WebSocket location checks an explicit public Origin allowlist, then maps
approved origins to the app's existing trusted localhost origin. This avoids a
video-service restart and retains cookie authentication. When adding a public
hostname, update the Nginx origin map as well as `server_name`, validate with
`/www/server/nginx/sbin/nginx -t`, and reload Nginx.

Rollback: move `aivideo.conf` out of the included vhost directory, validate and
reload Nginx, then use the original port-8100 entry. To undo a static release,
atomically repoint `/www/wwwroot/aivideo/current` to a previous release.
