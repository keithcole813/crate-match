"""HTTPS helper with an explicit CA bundle.

Why this exists: inside the PyInstaller-frozen macOS app, Python's
ssl module can't locate a CA store, so EVERY urllib HTTPS call died
with CERTIFICATE_VERIFY_FAILED — silently, because callers catch
exceptions. That is why MusicBrainz tag fill and the Deezer
similarity lookup worked from a dev shell but never on Keith's Mac
(v1.4.0/v1.4.1: identical book lists, pockets stuck on year-only).
certifi ships a CA bundle we can point ssl at explicitly; the build
collects it with --collect-all certifi.
"""
import http.client
import json
import ssl
import time
import urllib.error
import urllib.request

_CTX = None


def ssl_context():
    global _CTX
    if _CTX is None:
        try:
            import certifi
            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _CTX = ssl.create_default_context()
    return _CTX


def get_json(url, ua, timeout=10, attempts=3):
    """GET JSON with retries — MusicBrainz in particular drops
    connections mid-body (IncompleteRead) often enough that a single
    attempt made the tag fill look broken."""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (http.client.IncompleteRead, urllib.error.URLError,
                TimeoutError, ConnectionError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(0.6 * (i + 1))
    raise last
