"""Authenticated, fail-closed OCI manifest existence checks for release writers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LIMIT = 4 * 1024 * 1024
# This ACR endpoint delegates authentication to its separately hosted service.
# Keep the exception exact and host-bound; never trust an arbitrary challenge URL.
EXTERNAL_TOKEN_REALMS = {
    "registry.cn-beijing.aliyuncs.com": "https://dockerauth.cn-hangzhou.aliyuncs.com/auth",
}
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class RegistryError(ValueError):
    """Registry identity, authorization or response was not proven."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("Duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def http_get(url, headers):
    """Never forward credentials through a redirect or expose response bodies."""
    request = Request(url, headers=headers)
    try:
        response = build_opener(NoRedirect).open(request, timeout=30)
    except HTTPError as error:
        response = error
    except (URLError, TimeoutError, OSError):
        raise RegistryError("Registry connection outcome unknown") from None
    with response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise RegistryError("Registry response too large")
        return response.code, dict(response.headers.items()), raw


class Registry:
    def __init__(self, image, username, password, *, get=http_get):
        if not re.fullmatch(
            r"(?:ghcr\.io|[a-z0-9.-]+\.aliyuncs\.com)/[a-z0-9._/-]+", image
        ):
            raise RegistryError("Invalid registry image")
        if (
            not username
            or not password
            or any(c in username + password for c in "\r\n")
            or ":" in username
        ):
            raise RegistryError("Registry credentials required")
        self.host, self.repository = image.split("/", 1)
        self.base = f"https://{self.host}"
        self.get = get
        self.basic = (
            "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        )
        self.authorization = self.basic
        self.authorized = False

    def request(self, path):
        return self.get(
            self.base + path, {"Accept": ACCEPT, "Authorization": self.authorization}
        )

    def authenticate(self):
        path = f"/v2/{self.repository}/tags/list?n=1"
        status, headers, _ = self.get(self.base + "/v2/", {"Accept": ACCEPT})
        if status != 401:
            raise RegistryError(
                "Registry did not establish an authentication challenge"
            )
        challenge = next(
            (v for k, v in headers.items() if k.lower() == "www-authenticate"), ""
        )
        if challenge.startswith("Bearer "):
            pairs = re.findall(r'([a-z]+)="([^"\r\n]*)"', challenge[7:])
            params = unique_object(pairs)
            realm = urlsplit(params.get("realm", ""))
            service = params.get("service", "")
            if (
                realm.scheme != "https"
                or (
                    realm.netloc != self.host
                    and realm.geturl() != EXTERNAL_TOKEN_REALMS.get(self.host)
                )
                or realm.query
                or realm.fragment
                or not realm.path
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", service)
            ):
                raise RegistryError("Untrusted registry token challenge")
            query = urlencode(
                {"service": service, "scope": f"repository:{self.repository}:pull,push"}
            )
            code, _, token_raw = self.get(
                realm.geturl() + "?" + query,
                {"Authorization": self.basic, "Accept": "application/json"},
            )
            if code != 200:
                raise RegistryError("Registry token request failed")
            token_result = decode(token_raw)
            if not isinstance(token_result, dict):
                raise RegistryError("Invalid registry token response")
            token = token_result.get("token", token_result.get("access_token"))
            if (
                not isinstance(token, str)
                or not token
                or any(c.isspace() for c in token)
            ):
                raise RegistryError("Invalid registry token response")
            if token_result.get("access_token", token) != token:
                raise RegistryError("Conflicting registry tokens")
            self.authorization = "Bearer " + token
        elif challenge.startswith("Basic "):
            status, _, _ = self.request("/v2/")
            if status != 200:
                raise RegistryError("Registry credentials rejected")
        else:
            raise RegistryError("Unsupported registry authentication challenge")
        status, _, raw = self.request(path)
        if status != 200:
            raise RegistryError(f"Repository authorization unproven (HTTP {status})")
        listing = decode(raw)
        if (
            not isinstance(listing, dict)
            or listing.get("name") != self.repository
            or not isinstance(listing.get("tags"), (list, type(None)))
        ):
            raise RegistryError("Invalid authenticated repository listing")
        self.authorized = True

    def manifest(self, reference):
        if not re.fullmatch(
            r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|sha256:[0-9a-f]{64})", reference
        ):
            raise RegistryError("Invalid manifest reference")
        if not self.authorized:
            self.authenticate()
        status, headers, raw = self.request(
            f"/v2/{self.repository}/manifests/{reference}"
        )
        if status == 404:
            payload = decode(raw)
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if (
                isinstance(errors, list)
                and errors
                and all(
                    isinstance(e, dict) and e.get("code") == "MANIFEST_UNKNOWN"
                    for e in errors
                )
            ):
                return None
            raise RegistryError("Manifest absence unproven")
        if status != 200:
            raise RegistryError(f"Manifest state unknown (HTTP {status})")
        payload = decode(raw)
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
            raise RegistryError("Invalid manifest response")
        declared = next(
            (v for k, v in headers.items() if k.lower() == "docker-content-digest"),
            digest(raw),
        )
        if declared != digest(raw):
            raise RegistryError("Registry manifest digest header mismatch")
        return raw


def credentials(host):
    prefix = "GHCR" if host == "ghcr.io" else "ACR"
    username, password = (
        os.environ.get(prefix + "_USERNAME"),
        os.environ.get(prefix + "_PASSWORD"),
    )
    if username and password:
        return username, password
    auth_path = os.environ.get("REGISTRY_AUTH_FILE")
    if auth_path:
        raw = Path(auth_path).read_bytes()
        if len(raw) > LIMIT:
            raise RegistryError("Registry auth file too large")
        entry = decode(raw).get("auths", {}).get(host, {}).get("auth", "")
        try:
            decoded = base64.b64decode(entry, validate=True).decode()
            username, password = decoded.split(":", 1)
            return username, password
        except (ValueError, UnicodeError):
            pass
    raise RegistryError("Registry credentials unavailable")


def client(image):
    return Registry(image, *credentials(image.split("/", 1)[0]))


def probe_digest(image, tag):
    raw = client(image).manifest(tag)
    return None if raw is None else digest(raw)
