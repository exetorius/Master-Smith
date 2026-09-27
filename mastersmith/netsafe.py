"""Fetching a URL someone else chose (a customer's reference link, a web-search result). The service runs next to
your own network, so such a URL must not reach it: the host has to resolve to public addresses only, every redirect
hop is checked again, and the body is capped. URLs fal hands back for its own results do not come through here.
Known limit: the name is resolved here and again by requests on connect, so a DNS server that answers differently
the second time (rebinding) is not stopped; that needs an attacker's own DNS and a victim's machine running this."""
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

import requests

MAX_REDIRECTS = 3


class UnsafeURL(ValueError):
    pass


def check_public(url):
    """Raise UnsafeURL unless `url` is http(s) and its host resolves to public addresses only."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("only http(s) links to a named host can be fetched: %s" % url[:120])
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURL("cannot resolve %s (%s)" % (p.hostname, exc))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL("%s resolves to a private or reserved address (%s)" % (p.hostname, ip))


def get_public(url, headers=None, timeout=20):
    """A streamed GET of a public URL, redirects followed by hand so every hop is checked. Close the response."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public(url)
        r = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
        if not r.is_redirect:
            return r
        nxt = r.headers.get("location") or ""
        r.close()
        url = urljoin(url, nxt)
    raise UnsafeURL("more than %d redirects" % MAX_REDIRECTS)


def download_public(url, path, max_bytes=40 * 1024 * 1024, headers=None, timeout=60):
    """A public URL to `path`, refused past `max_bytes`."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with get_public(url, headers=headers, timeout=timeout) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    os.remove(path)
                    raise UnsafeURL("%s is larger than %d MB" % (url[:120], max_bytes // (1024 * 1024)))
                f.write(chunk)
    return path
