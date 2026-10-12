#!/usr/bin/env python3
"""Minimal OAuth2 authorization server for the MCP gateway.

Auth-code + PKCE (S256 preferred) flow. Static confidential client from
oauth.conf (typically client_id=grok), RFC 7591 Dynamic Client Registration
for public PKCE clients (token_endpoint_auth_method=none), and Client ID
Metadata Documents (CIMD): HTTPS URL client_ids are fetched, validated, and
cached per HTTP cache headers (not persisted).
HS256 JWT access + refresh tokens (stateless). Stdlib only.
Listens on 127.0.0.1:PORT; nginx on 443 proxies:

  GET  /authorize  -> auto-approve, 302 to redirect_uri with ?code=...
  POST /token      -> authorization_code | refresh_token -> access token
  POST /register   -> RFC 7591 DCR (public clients only; no client_secret)
  GET/POST /validate -> nginx auth_request (200 if Bearer JWT valid, else 401
       JSON invalid_token + RFC 9728 WWW-Authenticate). nginx must use
       error_page 401 = @mcp_oauth_401 so clients see JSON, not HTML.
  GET  /.well-known/oauth-authorization-server -> discovery doc
       (registration_endpoint + client_id_metadata_document_supported)
  GET  /.well-known/oauth-protected-resource[/<path>] -> RFC 9728 PRM
       (path-scoped resource = issuer + suffix)

Config: JSON at $GLAMA_OAUTH_CONF (default ~/.config/mcp-gateway/oauth.conf).
DCR client store: $MCP_OAUTH_CLIENTS
  (default ~/.state/mcp-gateway/oauth-clients.json, mode 600). Host state only;
  never commit this file. Public DCR records have no client_secret.
CIMD is in-memory only (HTTP cache headers); it is not written to the DCR store.
Rate limit: $MCP_OAUTH_REGISTER_LIMIT per IP per $MCP_OAUTH_REGISTER_WINDOW
  seconds (defaults 10 / 60). CIMD fetches: $MCP_OAUTH_CIMD_LIMIT /
  $MCP_OAUTH_CIMD_WINDOW (defaults 20 / 60). Honors X-Real-IP / X-Forwarded-For.
"""
import ipaddress, json, os, socket, ssl, sys, time, hmac, hashlib, base64, secrets, urllib.parse, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Populated by apply_conf() / load_conf_from_env(). Not loaded at import so
# tests can inject a temp conf without touching the host oauth.conf.
CONF = {}
CLIENT_ID = ""
CLIENT_SECRET = ""
JWT_SECRET = b""
TOKEN_TTL = 3600
REFRESH_TTL = 2592000
CODE_TTL = 300
# Public HTTPS base for this deployment. Host-specific by design, so it comes
# from the environment (`.env.local` / `.env`) rather than being hardcoded —
# keeps the repo publishable. Falls back to the historical host.
ISSUER = os.getenv("MCP_OAUTH_ISSUER") or os.getenv("MCP_PUBLIC_URL") or "https://glama-bridge.example.com"
PORT = 8780
REDIRECT_PREFIXES = []
STORE = None
RATE = None
CIMD_CACHE = None
CIMD_RATE = None
_CIMD_FETCH_HOOK = None
_CIMD_RESOLVE_HOOK = None
_loaded = False

MAX_REGISTER_BODY = 32 * 1024
MAX_REDIRECT_URIS = 10
MAX_CLIENTS = 5000
MAX_CLIENT_NAME = 200
DISALLOWED_URI_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "about"})
WILDCARD_CHARS = frozenset("*?[]")
CIMD_TIMEOUT = 5.0
CIMD_MAX_BYTES = 5 * 1024
CIMD_HEADER_LIMIT = 8192
CIMD_DEFAULT_TTL = 300
CIMD_MAX_TTL = 3600
# RFC 6890 special-use / non-global destinations we refuse to fetch (SSRF).
_CIMD_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96",
    "fc00::/7", "fe80::/10", "ff00::/8", "2001:db8::/32",
))


def default_conf_path():
    return os.environ.get(
        "GLAMA_OAUTH_CONF",
        os.path.expanduser("~/.config/mcp-gateway/oauth.conf"),
    )


def default_clients_path():
    return os.environ.get(
        "MCP_OAUTH_CLIENTS",
        os.path.expanduser("~/.state/mcp-gateway/oauth-clients.json"),
    )


def default_register_limit():
    return int(os.environ.get("MCP_OAUTH_REGISTER_LIMIT", "10"))


def default_register_window():
    return float(os.environ.get("MCP_OAUTH_REGISTER_WINDOW", "60"))


def default_cimd_limit():
    return int(os.environ.get("MCP_OAUTH_CIMD_LIMIT", "20"))


def default_cimd_window():
    return float(os.environ.get("MCP_OAUTH_CIMD_WINDOW", "60"))


def cimd_allow_loopback():
    return os.environ.get("MCP_OAUTH_CIMD_ALLOW_LOOPBACK", "").strip() in ("1", "true", "yes")


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64url_dec(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def make_jwt(claims):
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
           b64url(json.dumps(claims, separators=(",", ":")).encode()))
    sig = b64url(hmac.new(JWT_SECRET, seg.encode(), hashlib.sha256).digest())
    return seg + "." + sig


def verify_jwt(token):
    try:
        seg_h, seg_p, seg_s = token.split(".")
        expected = b64url(hmac.new(JWT_SECRET, f"{seg_h}.{seg_p}".encode(),
                                   hashlib.sha256).digest())
        if not hmac.compare_digest(expected, seg_s):
            return None
        claims = json.loads(b64url_dec(seg_p))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None


# In-memory one-time authorization codes: code -> dict
CODES = {}


def prune_codes():
    now = time.time()
    for c in [k for k, v in CODES.items() if v["exp"] < now]:
        CODES.pop(c, None)


def _safe_stderr(msg: str) -> None:
    """Never let logging kill the process (ENOSPC / broken stderr redirect)."""
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


class RateLimiter:
    """Simple in-memory N-per-key-per-window limiter (registration abuse)."""

    def __init__(self, limit=10, window=60.0):
        self.limit = int(limit)
        self.window = float(window)
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


class CimdCache:
    """In-memory CIMD records. TTL from HTTP cache headers; never persisted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def get(self, url):
        now = time.time()
        with self._lock:
            item = self._items.get(url)
            if not item:
                return None
            if item["exp"] <= now:
                self._items.pop(url, None)
                return None
            return dict(item["rec"])

    def put(self, url, rec, ttl):
        ttl = float(ttl)
        if ttl <= 0:
            return
        with self._lock:
            self._items[url] = {"rec": dict(rec), "exp": time.time() + ttl}

    def clear(self):
        with self._lock:
            self._items.clear()


class ClientStore:
    """Durable DCR client records (JSON file, mode 600). No secrets for `none`."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.clients = {}
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            self.clients = {}
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            raw = data.get("clients") if isinstance(data, dict) else None
            self.clients = dict(raw) if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            self.clients = {}

    def get(self, cid):
        with self._lock:
            rec = self.clients.get(cid)
            return dict(rec) if rec else None

    def put(self, rec):
        with self._lock:
            if rec["client_id"] not in self.clients and len(self.clients) >= MAX_CLIENTS:
                raise OverflowError("too many registered clients")
            self.clients[rec["client_id"]] = rec
            self._save_unlocked()

    def _save_unlocked(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".oauth-clients.", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 1, "clients": self.clients}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def as_metadata():
    """RFC 8414 AS metadata: DCR + CIMD advertised together."""
    return {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/oauth/authorize",
        "token_endpoint": ISSUER + "/oauth/token",
        "registration_endpoint": ISSUER + "/oauth/register",
        "client_id_metadata_document_supported": True,
        "token_endpoint_auth_methods_supported":
            ["client_secret_basic", "client_secret_post", "none"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "scopes_supported": ["mcp"],
    }


PRM_WELL_KNOWN = "/.well-known/oauth-protected-resource"


def _issuer_base():
    return ISSUER.rstrip("/")


def prm_resource_path(request_uri=""):
    """Return the resource-path suffix ('' or '/oauth/ex/name/mcp[?q]')."""
    if not request_uri:
        return ""
    path = request_uri.strip().split(" ", 1)[0]
    if path.startswith(PRM_WELL_KNOWN):
        path = path[len(PRM_WELL_KNOWN):]
    if not path.startswith("/"):
        return ""
    return path


def prm_document(request_uri=""):
    """RFC 9728 Protected Resource Metadata (path-scoped)."""
    base = _issuer_base()
    suffix = prm_resource_path(request_uri)
    return {
        "resource": base + suffix if suffix else base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


def prm_metadata_url(request_uri=""):
    """WWW-Authenticate resource_metadata URL for a request URI."""
    suffix = prm_resource_path(request_uri)
    return _issuer_base() + PRM_WELL_KNOWN + suffix


def bearer_www_authenticate(request_uri=""):
    """Notion-shaped Bearer challenge (realm + path-scoped PRM + invalid_token)."""
    return (
        'Bearer realm="OAuth", '
        f'resource_metadata="{prm_metadata_url(request_uri)}", '
        'error="invalid_token"'
    )


def validate_redirect_uris(uris, application_type="web"):
    """Return an error string, or None if the list is acceptable.

    Exact-match list only: no wildcards, no empty entries. web requires https
    (http://localhost / 127.0.0.1 / ::1 allowed). native allows custom schemes.
    """
    if not isinstance(uris, list) or not uris:
        return "redirect_uris must be a non-empty array"
    if len(uris) > MAX_REDIRECT_URIS:
        return "too many redirect_uris"
    seen = set()
    for u in uris:
        if not isinstance(u, str) or not u or u.strip() != u:
            return "redirect_uri must be a non-empty string"
        if any(ch in u for ch in WILDCARD_CHARS):
            return "wildcard redirect_uris are not allowed"
        parsed = urllib.parse.urlparse(u)
        scheme = (parsed.scheme or "").lower()
        if not scheme:
            return "redirect_uri must be an absolute URI"
        if scheme in DISALLOWED_URI_SCHEMES:
            return "disallowed redirect_uri scheme"
        if scheme in ("http", "https"):
            if not parsed.netloc:
                return "redirect_uri host required"
        elif not parsed.netloc and not parsed.path:
            return "redirect_uri must be an absolute URI"
        if application_type == "web":
            if scheme == "http":
                host = (parsed.hostname or "").lower()
                if host not in ("localhost", "127.0.0.1", "::1"):
                    return "web clients require https redirect_uris (http localhost allowed)"
            elif scheme != "https":
                return "web clients require https or localhost http redirect_uris"
        if u in seen:
            return "duplicate redirect_uris"
        seen.add(u)
    return None


def lookup_client(cid):
    """Static oauth.conf client, persisted DCR record, or cached CIMD."""
    if cid and cid == CLIENT_ID:
        return {
            "client_id": CLIENT_ID,
            "static": True,
            "token_endpoint_auth_method": "client_secret_basic",
        }
    if STORE and cid:
        rec = STORE.get(cid)
        if rec:
            return rec
    if CIMD_CACHE and cid:
        rec = CIMD_CACHE.get(cid)
        if rec:
            return rec
    return None


def is_cimd_client_id(cid):
    """True if cid is a Client Identifier URL (https + path, no userinfo/fragment)."""
    if not isinstance(cid, str) or not cid.startswith("https://"):
        return False
    if any(ch.isspace() for ch in cid):
        return False
    parsed = urllib.parse.urlparse(cid)
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.fragment:
        return False
    if not parsed.hostname:
        return False
    if not parsed.path:
        return False
    parts = parsed.path.split("/")
    if "." in parts or ".." in parts:
        return False
    return True


def looks_like_url_client_id(cid):
    return isinstance(cid, str) and cid.startswith(("https://", "http://"))


def is_blocked_ip(addr):
    """True if addr is special-use / private / link-local (RFC 6890-ish)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if cimd_allow_loopback() and ip.is_loopback:
        return False
    for net in _CIMD_BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def parse_ip_literal(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def resolve_host_ips(hostname):
    if _CIMD_RESOLVE_HOOK is not None:
        ips = _CIMD_RESOLVE_HOOK(hostname)
        return list(ips) if ips is not None else []
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    out = []
    seen = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def cimd_destination_ips(url):
    """Resolved IPs for a CIMD URL, or [] if the destination is blocked/unresolvable."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        return []
    literal = parse_ip_literal(host)
    ips = [host] if literal is not None else resolve_host_ips(host)
    if not ips:
        return []
    if any(is_blocked_ip(ip) for ip in ips):
        return []
    return ips


def parse_cache_ttl(headers):
    """Seconds to cache from Cache-Control / Expires. 0 means do not cache."""
    cc = (headers.get("cache-control") or "").lower()
    directives = [d.strip() for d in cc.split(",") if d.strip()]
    if any(d in ("no-store", "no-cache") or d.startswith("no-store")
           or d.startswith("no-cache") for d in directives):
        return 0
    max_age = None
    for d in directives:
        if d.startswith("s-maxage="):
            try:
                max_age = int(d.split("=", 1)[1].strip())
                break
            except ValueError:
                pass
        elif d.startswith("max-age=") and max_age is None:
            try:
                max_age = int(d.split("=", 1)[1].strip())
            except ValueError:
                pass
    if max_age is not None:
        if max_age < 0:
            max_age = 0
        age_hdr = headers.get("age")
        if age_hdr:
            try:
                max_age = max(0, max_age - int(age_hdr.strip()))
            except ValueError:
                pass
        return min(max_age, CIMD_MAX_TTL)
    expires = headers.get("expires")
    if expires:
        try:
            from email.utils import parsedate_to_datetime
            exp = parsedate_to_datetime(expires).timestamp()
            ttl = int(exp - time.time())
            if ttl <= 0:
                return 0
            return min(ttl, CIMD_MAX_TTL)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return CIMD_DEFAULT_TTL


def _json_content_type(ctype):
    ctype = (ctype or "").split(";")[0].strip().lower()
    if not ctype:
        return True
    if ctype in ("application/json", "text/json"):
        return True
    return ctype.startswith("application/") and ctype.endswith("+json")


def validate_cimd_document(url, data):
    """Return (record, error_string). Record is a public PKCE client view."""
    if not isinstance(data, dict):
        return None, "CIMD must be a JSON object"
    if data.get("client_id") != url:
        return None, "CIMD client_id must exactly match the request URL"
    name = data.get("client_name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_CLIENT_NAME:
        return None, "CIMD client_name required"
    if "client_secret" in data or "client_secret_expires_at" in data:
        return None, "CIMD must not include client_secret"
    auth_method = data.get("token_endpoint_auth_method") or "none"
    if auth_method in ("client_secret_post", "client_secret_basic", "client_secret_jwt"):
        return None, "CIMD must not use a shared-secret auth method"
    if auth_method != "none":
        return None, "only token_endpoint_auth_method=none is supported"
    app_type = data.get("application_type") or "web"
    if app_type not in ("native", "web"):
        return None, "application_type must be native or web"
    uri_err = validate_redirect_uris(data.get("redirect_uris"), application_type=app_type)
    if uri_err:
        return None, uri_err
    rec = {
        "client_id": url,
        "client_name": name,
        "redirect_uris": list(data["redirect_uris"]),
        "token_endpoint_auth_method": "none",
        "application_type": app_type,
        "cimd": True,
        "static": False,
    }
    return rec, None


def _read_chunked(sock, initial, max_bytes):
    buf = initial
    body = b""

    def _more():
        nonlocal buf
        chunk = sock.recv(1024)
        if not chunk:
            raise OSError("truncated chunked body")
        buf += chunk

    while True:
        while b"\r\n" not in buf:
            _more()
        line, buf = buf.split(b"\r\n", 1)
        size_s = line.split(b";", 1)[0].strip()
        try:
            size = int(size_s, 16)
        except ValueError:
            raise OSError("bad chunk size")
        if size == 0:
            return body
        if len(body) + size > max_bytes:
            raise OSError("body too large")
        while len(buf) < size + 2:
            _more()
        body += buf[:size]
        buf = buf[size:]
        if buf.startswith(b"\r\n"):
            buf = buf[2:]
        else:
            raise OSError("bad chunk trailer")


def _read_http_response(sock, max_bytes):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > CIMD_HEADER_LIMIT:
            raise OSError("headers too large")
    if b"\r\n\r\n" not in buf:
        raise OSError("incomplete headers")
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise OSError("bad status line")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    te = (headers.get("transfer-encoding") or "").lower()
    if "chunked" in te:
        body = _read_chunked(sock, rest, max_bytes)
    else:
        cl = headers.get("content-length")
        if cl is not None:
            try:
                n = int(cl)
            except ValueError:
                raise OSError("bad content-length")
            if n > max_bytes:
                raise OSError("body too large")
            body = rest
            while len(body) < n:
                chunk = sock.recv(min(8192, n - len(body)))
                if not chunk:
                    break
                body += chunk
            if len(body) != n:
                raise OSError("incomplete body")
        else:
            body = rest
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                body += chunk
                if len(body) > max_bytes:
                    raise OSError("body too large")
    if len(body) > max_bytes:
        raise OSError("body too large")
    return status, headers, body


def fetch_cimd_http(url):
    """GET url over HTTPS, pinned to a pre-checked IP. Does not follow redirects.

    Returns (status, headers, body_bytes). Test hook: _CIMD_FETCH_HOOK(url).
    """
    if _CIMD_FETCH_HOOK is not None:
        return _CIMD_FETCH_HOOK(url)
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 443
    ips = cimd_destination_ips(url)
    if not ips:
        raise OSError("blocked or unresolvable CIMD host")
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    if not path.startswith("/"):
        path = "/" + path
    ip = ips[0]
    raw = socket.create_connection((ip, port), timeout=CIMD_TIMEOUT)
    try:
        raw.settimeout(CIMD_TIMEOUT)
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        try:
            host_hdr = host if port == 443 else "%s:%s" % (host, port)
            req = (
                "GET %s HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Accept: application/json\r\n"
                "Connection: close\r\n"
                "User-Agent: mcp-gateway-oauth-cimd\r\n"
                "\r\n"
            ) % (path, host_hdr)
            ssock.sendall(req.encode("ascii"))
            return _read_http_response(ssock, CIMD_MAX_BYTES)
        finally:
            try:
                ssock.close()
            except OSError:
                pass
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise


def _cimd_err(status, error, description, retry_after=None):
    rec = {"status": status, "error": error, "error_description": description}
    if retry_after is not None:
        rec["retry_after"] = str(int(retry_after))
    return rec


def fetch_and_cache_cimd(url, rate_key=None):
    """Fetch, validate, and optionally cache a CIMD document. Never persists."""
    if not is_cimd_client_id(url):
        return None, _cimd_err(400, "unauthorized_client",
                               "client_id URL must be https with a path")
    if CIMD_CACHE:
        cached = CIMD_CACHE.get(url)
        if cached:
            return cached, None
    if CIMD_RATE is not None and rate_key:
        if not CIMD_RATE.allow(rate_key):
            window = CIMD_RATE.window if CIMD_RATE else 60
            return None, _cimd_err(429, "slow_down", "CIMD fetch rate limit",
                                   retry_after=window)
    if not cimd_destination_ips(url):
        return None, _cimd_err(400, "unauthorized_client",
                               "CIMD host is not fetchable")
    try:
        status, headers, body = fetch_cimd_http(url)
    except Exception:
        return None, _cimd_err(400, "unauthorized_client", "CIMD fetch failed")
    if status != 200:
        return None, _cimd_err(400, "unauthorized_client",
                               "CIMD document HTTP %s" % status)
    if not _json_content_type(headers.get("content-type")):
        return None, _cimd_err(400, "unauthorized_client",
                               "CIMD document must be JSON")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, _cimd_err(400, "unauthorized_client",
                               "CIMD document is not well-formed JSON")
    rec, verr = validate_cimd_document(url, data)
    if verr:
        return None, _cimd_err(400, "unauthorized_client", verr)
    ttl = parse_cache_ttl(headers)
    if CIMD_CACHE is not None:
        CIMD_CACHE.put(url, rec, ttl)
    return rec, None


def resolve_client(cid, rate_key=None):
    """Static, DCR, cached CIMD, or freshly fetched CIMD. Returns (rec, err)."""
    rec = lookup_client(cid)
    if rec:
        return rec, None
    if looks_like_url_client_id(cid):
        if not is_cimd_client_id(cid):
            return None, _cimd_err(
                400, "unauthorized_client",
                "client_id URL must be https with a path (no userinfo/fragment)",
            )
        return fetch_and_cache_cimd(cid, rate_key=rate_key)
    return None, None


def apply_conf(conf, clients_path=None, register_limit=None, register_window=None,
               cimd_limit=None, cimd_window=None):
    """Install config + DCR store + CIMD cache. Safe to call from tests."""
    global CONF, CLIENT_ID, CLIENT_SECRET, JWT_SECRET, TOKEN_TTL, REFRESH_TTL
    global CODE_TTL, ISSUER, PORT, REDIRECT_PREFIXES, STORE, RATE, _loaded, CODES
    global CIMD_CACHE, CIMD_RATE, _CIMD_FETCH_HOOK, _CIMD_RESOLVE_HOOK
    CONF = conf
    CLIENT_ID = conf["client_id"]
    CLIENT_SECRET = conf["client_secret"]
    JWT_SECRET = conf["jwt_secret"].encode() if isinstance(conf["jwt_secret"], str) else conf["jwt_secret"]
    TOKEN_TTL = int(conf.get("token_ttl", 3600))
    REFRESH_TTL = int(conf.get("refresh_ttl", 2592000))
    CODE_TTL = int(conf.get("code_ttl", 300))
    ISSUER = conf.get("issuer") or os.getenv("MCP_OAUTH_ISSUER") or os.getenv("MCP_PUBLIC_URL") or "https://glama-bridge.example.com"
    PORT = int(conf.get("port", 8780))
    REDIRECT_PREFIXES = list(conf.get("redirect_prefixes") or [])
    STORE = ClientStore(clients_path or default_clients_path())
    RATE = RateLimiter(
        limit=default_register_limit() if register_limit is None else register_limit,
        window=default_register_window() if register_window is None else register_window,
    )
    CIMD_CACHE = CimdCache()
    CIMD_RATE = RateLimiter(
        limit=default_cimd_limit() if cimd_limit is None else cimd_limit,
        window=default_cimd_window() if cimd_window is None else cimd_window,
    )
    _CIMD_FETCH_HOOK = None
    _CIMD_RESOLVE_HOOK = None
    CODES = {}
    _loaded = True


def set_cimd_test_hooks(fetch=None, resolve=None):
    """Test-only transport/DNS injection. Production never calls this."""
    global _CIMD_FETCH_HOOK, _CIMD_RESOLVE_HOOK
    _CIMD_FETCH_HOOK = fetch
    _CIMD_RESOLVE_HOOK = resolve


def load_conf_from_env():
    with open(default_conf_path()) as f:
        apply_conf(json.load(f))


def ensure_loaded():
    if not _loaded:
        load_conf_from_env()


def _safe_compare(a, b):
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a), str(b))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _safe_stderr("[oauth-as] %s - %s\n" % (self.address_string(), fmt % args))

    def log_error(self, fmt, *args):
        # BaseHTTPRequestHandler logs client disconnects as errors; keep them soft.
        _safe_stderr("[oauth-as] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client/nginx went away mid-response — not a server fault.
            pass

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _client_ip(self):
        xri = (self.headers.get("X-Real-IP") or "").strip()
        if xri:
            return xri
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    # ---- routing ----
    def do_GET(self):
        ensure_loaded()
        u = urllib.parse.urlparse(self.path)
        if u.path == "/authorize":
            return self.handle_authorize(urllib.parse.parse_qs(u.query))
        if u.path == "/validate":
            return self.handle_validate()
        if u.path == "/.well-known/oauth-authorization-server":
            return self._json(200, as_metadata())
        if u.path == PRM_WELL_KNOWN or u.path.startswith(PRM_WELL_KNOWN + "/"):
            uri = u.path
            if u.query:
                uri = uri + "?" + u.query
            return self._json(200, prm_document(uri))
        if u.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        ensure_loaded()
        u = urllib.parse.urlparse(self.path)
        if u.path == "/token":
            return self.handle_token()
        if u.path in ("/register", "/oauth/register"):
            return self.handle_register()
        if u.path == "/validate":
            return self.handle_validate()
        return self._json(404, {"error": "not_found"})

    # ---- /register (RFC 7591, public PKCE clients only) ----
    def handle_register(self):
        # Always drain the body first so HTTP/1.1 keep-alive is not desynced
        # when we 429 (rate limit must not leave unread POST bytes on the socket).
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_REGISTER_BODY:
            leftover = length
            while leftover > 0:
                chunk = self.rfile.read(min(leftover, 8192))
                if not chunk:
                    break
                leftover -= len(chunk)
            return self._json(400, {"error": "invalid_request",
                                    "error_description": "body too large"})
        raw = self.rfile.read(length) if length else b""
        if RATE is None or not RATE.allow(self._client_ip()):
            retry = str(int(RATE.window)) if RATE else "60"
            return self._json(429, {
                "error": "slow_down",
                "error_description": "registration rate limit",
            }, extra={"Retry-After": retry})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype not in ("application/json", "text/json"):
            return self._json(400, {"error": "invalid_request",
                                    "error_description": "application/json required"})
        try:
            body = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid_request",
                                    "error_description": "JSON body required"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "invalid_request",
                                    "error_description": "JSON object required"})

        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "only token_endpoint_auth_method=none is supported",
            })
        app_type = body.get("application_type") or "web"
        if app_type not in ("native", "web"):
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "application_type must be native or web",
            })
        grant_types = body.get("grant_types")
        if grant_types is None:
            grant_types = ["authorization_code"]
        if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "grant_types must include authorization_code",
            })
        extra_grants = set(grant_types) - {"authorization_code", "refresh_token"}
        if extra_grants:
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "unsupported grant_types",
            })
        response_types = body.get("response_types")
        if response_types is None:
            response_types = ["code"]
        if not isinstance(response_types, list) or "code" not in response_types:
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "response_types must include code",
            })
        if set(response_types) - {"code"}:
            return self._json(400, {
                "error": "invalid_client_metadata",
                "error_description": "unsupported response_types",
            })
        uris = body.get("redirect_uris")
        uri_err = validate_redirect_uris(uris, application_type=app_type)
        if uri_err:
            return self._json(400, {"error": "invalid_redirect_uri",
                                    "error_description": uri_err})
        client_name = body.get("client_name")
        if client_name is not None:
            if not isinstance(client_name, str) or len(client_name) > MAX_CLIENT_NAME:
                return self._json(400, {
                    "error": "invalid_client_metadata",
                    "error_description": "invalid client_name",
                })

        cid = secrets.token_urlsafe(24)
        while lookup_client(cid):
            cid = secrets.token_urlsafe(24)
        issued = int(time.time())
        rec = {
            "client_id": cid,
            "client_id_issued_at": issued,
            "client_name": client_name,
            "redirect_uris": list(uris),
            "grant_types": list(grant_types),
            "response_types": list(response_types),
            "token_endpoint_auth_method": "none",
            "application_type": app_type,
        }
        try:
            STORE.put(rec)
        except OverflowError:
            return self._json(503, {"error": "temporarily_unavailable",
                                    "error_description": "registration full"})
        except OSError:
            return self._json(500, {"error": "server_error"})

        resp = {
            "client_id": cid,
            "client_id_issued_at": issued,
            "redirect_uris": list(uris),
            "grant_types": list(grant_types),
            "response_types": list(response_types),
            "token_endpoint_auth_method": "none",
            "application_type": app_type,
        }
        if client_name is not None:
            resp["client_name"] = client_name
        return self._json(201, resp)

    # ---- /authorize (auto-approve) ----
    def handle_authorize(self, q):
        def g(k):
            v = q.get(k)
            return v[0] if v else None
        client_id = g("client_id")
        redirect_uri = g("redirect_uri")
        state = g("state")
        resp_type = g("response_type")
        cc = g("code_challenge")
        ccm = g("code_challenge_method")
        scope = g("scope") or ""
        client, cerr = resolve_client(client_id, rate_key=self._client_ip())
        if cerr:
            extra = {"Retry-After": cerr["retry_after"]} if cerr.get("retry_after") else None
            return self._json(cerr["status"], {
                "error": cerr["error"],
                "error_description": cerr.get("error_description"),
            }, extra=extra)
        if not client:
            return self._json(400, {"error": "unauthorized_client"})
        if not redirect_uri:
            return self._json(400, {"error": "invalid_request",
                                    "error_description": "redirect_uri required"})
        if client.get("static"):
            if REDIRECT_PREFIXES and not any(redirect_uri.startswith(p)
                                             for p in REDIRECT_PREFIXES):
                return self._json(400, {"error": "invalid_request",
                                        "error_description": "redirect_uri not allowed"})
            if ccm is None:
                ccm = "plain"
        else:
            if redirect_uri not in (client.get("redirect_uris") or []):
                return self._json(400, {"error": "invalid_request",
                                        "error_description": "redirect_uri not allowed"})
            # Public DCR clients: PKCE required; S256 preferred (default if omitted).
            if client.get("token_endpoint_auth_method") == "none":
                if not cc:
                    return self._json(400, {
                        "error": "invalid_request",
                        "error_description": "PKCE code_challenge required",
                    })
                if ccm is None:
                    ccm = "S256"
                if ccm not in ("S256", "plain"):
                    return self._json(400, {
                        "error": "invalid_request",
                        "error_description": "code_challenge_method must be S256 or plain",
                    })
            elif ccm is None:
                ccm = "plain"
        if resp_type != "code":
            return self._redirect_err(redirect_uri, state, "unsupported_response_type")
        prune_codes()
        code = secrets.token_urlsafe(32)
        CODES[code] = {"client_id": client_id, "redirect_uri": redirect_uri,
                       "code_challenge": cc, "code_challenge_method": ccm,
                       "scope": scope, "exp": time.time() + CODE_TTL,
                       "public": (not client.get("static")
                                  and client.get("token_endpoint_auth_method") == "none")}
        sep = "&" if "?" in redirect_uri else "?"
        loc = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
        if state is not None:
            loc += "&state=" + urllib.parse.quote(state)
        self._send(302, b"", "text/plain", {"Location": loc})

    def _redirect_err(self, redirect_uri, state, err):
        sep = "&" if "?" in redirect_uri else "?"
        loc = f"{redirect_uri}{sep}error={err}"
        if state:
            loc += "&state=" + urllib.parse.quote(state)
        self._send(302, b"", "text/plain", {"Location": loc})

    # ---- client auth helper ----
    def _client_auth(self, params):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                dec = base64.b64decode(auth[6:]).decode()
                cid, _, csec = dec.partition(":")
                return urllib.parse.unquote(cid), urllib.parse.unquote(csec)
            except Exception:
                return None, None
        return (params.get("client_id", [None])[0],
                params.get("client_secret", [None])[0])

    def _client_ok(self, cid, csec, *, public_ok=False):
        """Static confidential client (secret) and optional public PKCE (no secret)."""
        client = lookup_client(cid)
        if not client:
            return False
        if client.get("static"):
            if csec and _safe_compare(csec, CLIENT_SECRET):
                return True
            # Public-style token exchange for the static client (existing grok PKCE).
            if public_ok and (csec in (None, "")):
                return True
            return False
        if client.get("token_endpoint_auth_method") == "none":
            return public_ok and (csec in (None, ""))
        return False

    # ---- /token ----
    def handle_token(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode() if length else ""
        params = urllib.parse.parse_qs(body)

        def g(k):
            v = params.get(k)
            return v[0] if v else None

        cid, csec = self._client_auth(params)
        grant = g("grant_type")
        client, cerr = resolve_client(cid, rate_key=self._client_ip())
        if cerr and cerr.get("error") == "slow_down":
            return self._json(429, {
                "error": "slow_down",
                "error_description": cerr.get("error_description"),
            }, extra={"Retry-After": cerr.get("retry_after", "60")})
        dcr_public = bool(client and not client.get("static")
                          and client.get("token_endpoint_auth_method") == "none")
        # authorization_code with PKCE may omit client_secret (public client).
        # DCR public clients may also refresh without a secret (they have none).
        public_ok = grant == "authorization_code" or (grant == "refresh_token" and dcr_public)
        if not self._client_ok(cid, csec, public_ok=public_ok):
            return self._json(401, {"error": "invalid_client"},
                              extra={"WWW-Authenticate": "Basic"})
        if grant == "authorization_code":
            return self._grant_code(g, cid)
        if grant == "refresh_token":
            # refresh still requires confidential client secret for the static client
            if client and client.get("static"):
                if not self._client_ok(cid, csec, public_ok=False):
                    return self._json(401, {"error": "invalid_client"},
                                      extra={"WWW-Authenticate": "Basic"})
            return self._grant_refresh(g, cid)
        return self._json(400, {"error": "unsupported_grant_type"})

    def _issue(self, scope, client_id):
        now = int(time.time())
        access = make_jwt({"iss": ISSUER, "sub": client_id, "client_id": client_id,
                           "typ": "access", "scope": scope,
                           "iat": now, "exp": now + TOKEN_TTL})
        refresh = make_jwt({"iss": ISSUER, "sub": client_id, "client_id": client_id,
                            "typ": "refresh", "scope": scope,
                            "iat": now, "exp": now + REFRESH_TTL})
        self._json(200, {"access_token": access, "token_type": "Bearer",
                         "expires_in": TOKEN_TTL, "refresh_token": refresh,
                         "scope": scope})

    def _grant_code(self, g, cid):
        code = g("code")
        redirect_uri = g("redirect_uri")
        verifier = g("code_verifier")
        prune_codes()
        rec = CODES.pop(code, None) if code else None
        if not rec:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "bad or used code"})
        if rec.get("client_id") != cid:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "client_id mismatch"})
        if redirect_uri and rec["redirect_uri"] != redirect_uri:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "redirect_uri mismatch"})
        public = bool(rec.get("public"))
        if public or rec.get("code_challenge"):
            if public and not rec.get("code_challenge"):
                return self._json(400, {"error": "invalid_grant",
                                        "error_description": "PKCE required"})
            if rec.get("code_challenge"):
                if not verifier:
                    return self._json(400, {"error": "invalid_grant",
                                            "error_description": "code_verifier required"})
                if rec.get("code_challenge_method") == "S256":
                    calc = b64url(hashlib.sha256(verifier.encode()).digest())
                else:
                    calc = verifier
                if not hmac.compare_digest(calc, rec["code_challenge"]):
                    return self._json(400, {"error": "invalid_grant",
                                            "error_description": "PKCE verification failed"})
        self._issue(rec["scope"], rec["client_id"])

    def _grant_refresh(self, g, cid):
        rt = g("refresh_token")
        claims = verify_jwt(rt) if rt else None
        if not claims or claims.get("typ") != "refresh":
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "bad refresh_token"})
        if claims.get("client_id") and cid and claims["client_id"] != cid:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "client_id mismatch"})
        self._issue(claims.get("scope", ""), cid)

    # ---- /validate (nginx auth_request) ----
    def handle_validate(self):
        # JSON body only here. nginx auth_request copies WWW-Authenticate onto
        # the client; @mcp_oauth_401 is the single client-facing challenge so
        # Spark does not see two Bearer headers. Direct :8780 hits still get
        # invalid_token JSON (internal).
        auth = (self.headers.get("Authorization", "") or
                self.headers.get("X-Original-Authorization", ""))
        if not auth.startswith("Bearer "):
            return self._json(401, {"error": "invalid_token"})
        claims = verify_jwt(auth[7:])
        if not claims or claims.get("typ") != "access":
            return self._json(401, {"error": "invalid_token"})
        self._send(200, b'{"active":true}')


if __name__ == "__main__":
    # ThreadingHTTPServer worker threads can raise BrokenPipeError after the
    # handler returns; silence noisy excepthook spam when stderr is a full disk.
    def _thread_hook(args):
        if isinstance(args.exc_value, (BrokenPipeError, ConnectionResetError)):
            return
        sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)

    try:
        import threading as _threading
        _threading.excepthook = _thread_hook  # type: ignore[attr-defined]
    except Exception:
        pass

    load_conf_from_env()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    _safe_stderr(f"[oauth-as] listening on 127.0.0.1:{PORT} issuer={ISSUER} "
                 f"dcr={STORE.path if STORE else ''} cimd=1\n")
    srv.serve_forever()
