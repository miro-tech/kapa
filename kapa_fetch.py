#!/usr/bin/env python3
"""
Kapa VPN — temp-mail → email OTP → connection-plan → profiles / URIs

Flow:
  1) create temp mailbox (mail.tm) or use --email
  2) POST /v1/auth/email/start
  3) wait OTP (auto from inbox, or --code / prompt)
  4) POST /v1/auth/email/verify → accessToken
  5) GET  /v1/countries
  6) GET  /v1/connection-plan?country=XX  for each country
  7) save JSON + best-effort VLESS URI extraction

Install:
  pip install requests

Examples:
  python3 kapa_fetch.py -o ./kapa_out
  python3 kapa_fetch.py -o ./kapa_out --email you@gmail.com --code 123456
  python3 kapa_fetch.py -o ./kapa_out --email you@gmail.com          # prompt for code
  python3 kapa_fetch.py -o ./kapa_out --countries NL,US --token eyJ...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://api.kapavpn.com"
UA = "KapaVPN/1"
APP_VER = "0.1.7.10"
TIMEOUT = 30


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    s.verify = False
    return s


def api_headers(device_id: str, token: str | None = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Device-Id": device_id,
        "X-Platform": "android",
        "X-Client-Version": APP_VER,
        "X-Platform-Version": "34",
        "X-Client-Locale": "en",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------- temp mail (mail.tm) ----------

def mailtm_create(s: requests.Session) -> tuple[str, str, dict]:
    """Return (email, password, auth_headers)."""
    r = s.get("https://api.mail.tm/domains", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        domain = data[0]["domain"]
    else:
        domain = data["hydra:member"][0]["domain"]

    email = f"kapa{int(time.time())}{uuid.uuid4().hex[:5]}@{domain}"
    password = f"Kapa{uuid.uuid4().hex[:10]}!"
    r = s.post(
        "https://api.mail.tm/accounts",
        json={"address": email, "password": password},
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"mail.tm account failed: {r.status_code} {r.text[:200]}")
    r = s.post(
        "https://api.mail.tm/token",
        json={"address": email, "password": password},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    token = r.json()["token"]
    return email, password, {"Authorization": f"Bearer {token}"}


def mailtm_wait_otp(s: requests.Session, mail_headers: dict, timeout: int = 120) -> str | None:
    deadline = time.time() + timeout
    seen: set[str] = set()
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            r = s.get("https://api.mail.tm/messages", headers=mail_headers, timeout=TIMEOUT)
            if r.status_code != 200 or not r.text.strip():
                print(f"  [mail] poll {n}: empty/http {r.status_code}")
                time.sleep(4)
                continue
            data = r.json()
            members = data.get("hydra:member") if isinstance(data, dict) else data
            members = members or []
        except Exception as e:
            print(f"  [mail] poll {n}: {e}")
            time.sleep(4)
            continue

        print(f"  [mail] poll {n}: {len(members)} message(s)")
        for m in members:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            try:
                full = s.get(
                    f"https://api.mail.tm/messages/{mid}",
                    headers=mail_headers,
                    timeout=TIMEOUT,
                ).json()
            except Exception as e:
                print(f"  [mail] fetch {mid}: {e}")
                continue
            subject = str(full.get("subject") or "")
            text = "\n".join(
                [
                    str(full.get("text") or ""),
                    str(full.get("html") or ""),
                    subject,
                    json.dumps(full.get("from") or {}),
                ]
            )
            text = re.sub(r"<[^>]+>", " ", text)
            codes = re.findall(r"\b(\d{6})\b", text)
            print(f"  [mail] from={full.get('from')} subj={subject!r} codes={codes}")
            # prefer Kapa-looking messages
            blob = (subject + " " + text).lower()
            if codes and ("kapa" in blob or "sign" in blob or "code" in blob):
                return codes[0]
            if codes and "guerrilla" not in blob and "welcome" not in blob:
                return codes[0]
        time.sleep(4)
    return None


# ---------- guerrilla fallback (subject often only; body may be empty) ----------

def guerrilla_create(s: requests.Session) -> tuple[str, str]:
    r = s.get(
        "https://api.guerrillamail.com/ajax.php",
        params={"f": "get_email_address", "lang": "en"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data["email_addr"], data["sid_token"]


def guerrilla_wait_otp(s: requests.Session, sid: str, timeout: int = 90) -> str | None:
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        n += 1
        r = s.get(
            "https://api.guerrillamail.com/ajax.php",
            params={"f": "check_email", "sid_token": sid, "seq": 0},
            timeout=TIMEOUT,
        )
        box = r.json()
        lst = box.get("list") or []
        print(f"  [guerrilla] poll {n}: {len(lst)} message(s)")
        for m in lst:
            fr = (m.get("mail_from") or "").lower()
            subj = m.get("mail_subject") or ""
            if "kapa" not in fr and "kapavpn" not in fr and "kapa" not in subj.lower():
                continue
            mid = m.get("mail_id")
            full = s.get(
                "https://api.guerrillamail.com/ajax.php",
                params={"f": "fetch_email", "sid_token": sid, "email_id": mid},
                timeout=TIMEOUT,
            ).json()
            body = "\n".join(
                [
                    str(full.get("mail_body") or ""),
                    str(full.get("mail_excerpt") or ""),
                    str(full.get("mail_subject") or ""),
                ]
            )
            body = re.sub(r"<[^>]+>", " ", body)
            codes = re.findall(r"\b(\d{6})\b", body)
            print(f"  [guerrilla] subj={subj!r} codes={codes} body_len={len(body)}")
            if codes:
                return codes[0]
            # Guerrilla often strips Kapa SES body → fall through to manual
        time.sleep(4)
    return None


# ---------- Kapa API ----------

def email_start(s: requests.Session, email: str, device_id: str) -> None:
    r = s.post(
        f"{API}/v1/auth/email/start",
        headers=api_headers(device_id),
        json={"email": email},
        timeout=TIMEOUT,
    )
    if r.status_code == 429:
        raise RuntimeError("rate_limited on email/start — wait and retry")
    if not r.ok:
        raise RuntimeError(f"email/start HTTP {r.status_code}: {r.text[:300]}")
    print(f"[+] email/start → {r.text.strip()}")


def email_verify(s: requests.Session, email: str, code: str, device_id: str) -> dict:
    r = s.post(
        f"{API}/v1/auth/email/verify",
        headers=api_headers(device_id),
        json={"email": email, "code": code},
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"email/verify HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def extract_token(payload: dict) -> str:
    """Best-effort access token from verify response."""
    if not isinstance(payload, dict):
        raise RuntimeError("verify response is not an object")

    candidates: list[Any] = [
        payload.get("accessToken"),
        payload.get("access_token"),
        payload.get("token"),
        payload.get("authToken"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("accessToken"),
                data.get("access_token"),
                data.get("token"),
                data.get("authToken"),
            ]
        )
        tokens = data.get("tokens") or data.get("tokenSet")
        if isinstance(tokens, dict):
            candidates.extend(
                [
                    tokens.get("accessToken"),
                    tokens.get("access_token"),
                    tokens.get("token"),
                ]
            )
    tokens = payload.get("tokens") or payload.get("tokenSet")
    if isinstance(tokens, dict):
        candidates.extend(
            [
                tokens.get("accessToken"),
                tokens.get("access_token"),
                tokens.get("token"),
            ]
        )

    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()

    # nested search
    blob = json.dumps(payload)
    for m in re.finditer(r'"(?:access_token|accessToken|authToken|token)"\s*:\s*"([^"]+)"', blob):
        val = m.group(1)
        if len(val) > 20:
            return val

    raise RuntimeError(f"No access token in verify response: {json.dumps(payload)[:500]}")


def get_countries(s: requests.Session, device_id: str, token: str | None = None) -> list[str]:
    r = s.get(
        f"{API}/v1/countries",
        headers=api_headers(device_id, token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    countries = data.get("countries") or []
    codes = []
    for c in countries:
        if isinstance(c, dict) and c.get("code"):
            codes.append(str(c["code"]))
        elif isinstance(c, str):
            codes.append(c)
    return codes


def get_connection_plan(
    s: requests.Session, device_id: str, token: str, country: str
) -> dict:
    r = s.get(
        f"{API}/v1/connection-plan",
        params={"country": country},
        headers=api_headers(device_id, token),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"connection-plan {country} HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def get_account(s: requests.Session, device_id: str, token: str) -> dict | None:
    r = s.get(f"{API}/v1/account", headers=api_headers(device_id, token), timeout=TIMEOUT)
    if r.ok:
        return r.json()
    print(f"[!] account HTTP {r.status_code}: {r.text[:200]}")
    return None


# ---------- profile → URI ----------

def walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for i in obj:
            yield from walk(i)


def find_vless_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        if obj.startswith("vless://"):
            found.append(obj.strip())
        else:
            found.extend(re.findall(r"vless://[^\s\"'<>]+", obj))
        return found
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(find_vless_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(find_vless_strings(v))
    return found


def profile_to_uris(profile: dict, country: str, index: int) -> list[str]:
    """Best-effort build VLESS URIs from a connection-plan profile object."""
    uris = find_vless_strings(profile)
    if uris:
        return uris

    # Common field guesses from DEX (Xray / Reality)
    uuid_ = (
        profile.get("uuid")
        or profile.get("id")
        or profile.get("userId")
        or profile.get("vlessUuid")
        or profile.get("password")
    )
    host = (
        profile.get("host")
        or profile.get("hostname")
        or profile.get("server")
        or profile.get("address")
        or profile.get("endpoint")
    )
    port = profile.get("port") or profile.get("serverPort") or 443
    name = profile.get("name") or profile.get("title") or f"{country}-{index}"
    security = (profile.get("security") or profile.get("streamSecurity") or "reality").lower()
    network = (profile.get("network") or profile.get("type") or profile.get("transport") or "tcp").lower()
    sni = profile.get("sni") or profile.get("serverName") or profile.get("host") or host
    pbk = (
        profile.get("pbk")
        or profile.get("publicKey")
        or profile.get("public_key")
        or profile.get("realityPublicKey")
        or (profile.get("realitySettings") or {}).get("publicKey")
        if isinstance(profile.get("realitySettings"), dict)
        else None
    )
    sid = (
        profile.get("sid")
        or profile.get("shortId")
        or profile.get("short_id")
        or (profile.get("realitySettings") or {}).get("shortId")
        if isinstance(profile.get("realitySettings"), dict)
        else None
    )
    flow = profile.get("flow") or ""
    path = profile.get("path") or ""
    service_name = profile.get("serviceName") or profile.get("grpcServiceName") or ""
    fp = profile.get("fp") or profile.get("fingerprint") or "chrome"

    # nested raw xray config
    for key in ("rawConfig", "config", "xray", "outbound", "outbounds"):
        node = profile.get(key)
        if node:
            uris.extend(find_vless_strings(node))
            if isinstance(node, dict):
                # try vnext
                try:
                    vnext = node.get("settings", {}).get("vnext", [])
                    if vnext and not uuid_:
                        uuid_ = vnext[0].get("users", [{}])[0].get("id")
                        host = host or vnext[0].get("address")
                        port = port or vnext[0].get("port")
                except Exception:
                    pass

    if uris:
        return uris

    if not (uuid_ and host):
        return []

    query = {
        "encryption": "none",
        "security": security if security in ("reality", "tls", "none") else "reality",
        "type": network,
        "fp": fp,
    }
    if sni:
        query["sni"] = str(sni)
    if pbk:
        query["pbk"] = str(pbk)
    if sid is not None and str(sid) != "":
        query["sid"] = str(sid)
    if flow:
        query["flow"] = str(flow)
    if path:
        query["path"] = str(path)
    if service_name:
        query["serviceName"] = str(service_name)

    q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query.items() if v is not None)
    return [f"vless://{uuid_}@{host}:{port}?{q}#{quote(str(name))}"]


def extract_profiles(plan: dict) -> list[dict]:
    if not isinstance(plan, dict):
        return []
    for key in ("profiles", "servers", "nodes", "data", "items"):
        val = plan.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            # data: { profiles: [...] }
            for k2 in ("profiles", "servers", "nodes", "items"):
                v2 = val.get(k2)
                if isinstance(v2, list):
                    return [x for x in v2 if isinstance(x, dict)]
    # single profile object
    if any(k in plan for k in ("uuid", "host", "rawConfig", "vless")):
        return [plan]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Kapa VPN config fetcher")
    ap.add_argument("-o", "--output", default="", help="output dir (default kapa_YYYYMMDD_HHMMSS)")
    ap.add_argument("--email", default="", help="email for OTP (skip temp-mail)")
    ap.add_argument("--code", default="", help="6-digit OTP (skip inbox poll)")
    ap.add_argument("--token", default="", help="existing Bearer access token (skip auth)")
    ap.add_argument(
        "--countries",
        default="",
        help="comma-separated country codes (default: all from /v1/countries)",
    )
    ap.add_argument("--otp-timeout", type=int, default=120, help="seconds to wait for OTP mail")
    ap.add_argument(
        "--mail",
        choices=("mail.tm", "guerrilla", "manual"),
        default="mail.tm",
        help="temp-mail provider (manual = print email and wait for --code/prompt)",
    )
    args = ap.parse_args()

    out = Path(args.output) if args.output else Path(time.strftime("kapa_%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"[*] output: {out.resolve()}")

    s = session()
    device_id = str(uuid.uuid4())
    account: dict[str, Any] = {"device_id": device_id}

    token = args.token.strip() or None
    email = args.email.strip() or None

    if not token:
        mail_headers = None
        guerrilla_sid = None

        if not email:
            if args.mail == "mail.tm":
                try:
                    email, password, mail_headers = mailtm_create(s)
                    account["email"] = email
                    account["mail_password"] = password
                    account["mail_provider"] = "mail.tm"
                    print(f"[+] temp mail: {email}")
                except Exception as e:
                    print(f"[!] mail.tm failed: {e}")
                    print("[*] falling back to guerrilla…")
                    email, guerrilla_sid = guerrilla_create(s)
                    account["email"] = email
                    account["mail_provider"] = "guerrilla"
                    print(f"[+] temp mail: {email}")
            elif args.mail == "guerrilla":
                email, guerrilla_sid = guerrilla_create(s)
                account["email"] = email
                account["mail_provider"] = "guerrilla"
                print(f"[+] temp mail: {email}")
            else:
                raise SystemExit("--email required when --mail manual")
        else:
            account["email"] = email
            account["mail_provider"] = "manual"
            print(f"[*] using email: {email}")

        print("[*] POST /v1/auth/email/start")
        email_start(s, email, device_id)

        code = args.code.strip() or None
        if not code:
            if mail_headers is not None:
                print(f"[*] waiting OTP in mail.tm (timeout {args.otp_timeout}s)…")
                code = mailtm_wait_otp(s, mail_headers, timeout=args.otp_timeout)
            elif guerrilla_sid is not None:
                print(f"[*] waiting OTP in guerrilla (timeout {args.otp_timeout}s)…")
                code = guerrilla_wait_otp(s, guerrilla_sid, timeout=min(args.otp_timeout, 90))
                if not code:
                    print(
                        "[!] Guerrilla often receives Kapa subject but empty body.\n"
                        "    Open the mailbox in browser or use a real inbox."
                    )

            if not code:
                # interactive fallback
                try:
                    code = input(f"[?] Enter 6-digit code for {email}: ").strip()
                except EOFError:
                    code = ""
            if not code:
                save_json(out / "account.json", account)
                raise SystemExit(
                    "No OTP received. Re-run with:\n"
                    f"  python3 kapa_fetch.py -o {out} --email {email} --code XXXXXX"
                )

        print(f"[*] POST /v1/auth/email/verify code={code}")
        verify_payload = email_verify(s, email, code, device_id)
        save_json(out / "verify.json", verify_payload)
        token = extract_token(verify_payload)
        account["access_token"] = token
        account["verify"] = verify_payload
        print(f"[+] access_token={token[:24]}…")
    else:
        account["access_token"] = token
        print("[*] using provided --token")

    save_json(out / "account.json", account)

    # account info
    acc = get_account(s, device_id, token)
    if acc:
        save_json(out / "account_info.json", acc)
        print(f"[+] account: {json.dumps(acc, ensure_ascii=False)[:200]}")

    # countries
    if args.countries.strip():
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    else:
        countries = get_countries(s, device_id, token)
        if not countries:
            countries = ["NL", "US", "GB", "LT", "IE", "IL"]
    print(f"[*] countries: {countries}")
    save_json(out / "countries.json", countries)

    all_plans: dict[str, Any] = {}
    all_uris: list[str] = []
    all_profiles: list[dict] = []

    for country in countries:
        print(f"[*] GET /v1/connection-plan?country={country}")
        try:
            plan = get_connection_plan(s, device_id, token, country)
        except Exception as e:
            print(f"  [!] {e}")
            all_plans[country] = {"error": str(e)}
            continue

        save_json(out / f"plan_{country}.json", plan)
        all_plans[country] = plan
        profiles = extract_profiles(plan)
        print(f"  profiles: {len(profiles)}  keys={list(plan.keys())[:12]}")

        # also harvest any embedded vless strings in whole plan
        embedded = find_vless_strings(plan)
        for u in embedded:
            if u not in all_uris:
                all_uris.append(u)

        for i, prof in enumerate(profiles, start=1):
            prof_out = dict(prof)
            prof_out["_country"] = country
            all_profiles.append(prof_out)
            for u in profile_to_uris(prof, country, i):
                if u not in all_uris:
                    all_uris.append(u)

        # if no profiles list, dump top-level strings
        if not profiles and not embedded:
            # save raw for manual inspection
            print(f"  [!] no profiles parsed — see plan_{country}.json")

    save_json(out / "plans_all.json", all_plans)
    save_json(out / "profiles.json", all_profiles)
    (out / "uris.txt").write_text(
        ("\n".join(all_uris) + ("\n" if all_uris else "")), encoding="utf-8"
    )

    print()
    print("=" * 56)
    print(f"[+] OUTPUT: {out.resolve()}")
    print(f"[+] email:  {account.get('email')}")
    print(f"[+] token:  {(token or '')[:28]}…")
    print(f"[+] plans:  {len(all_plans)}  profiles: {len(all_profiles)}  uris: {len(all_uris)}")
    if all_uris:
        print(f"[+] sample: {all_uris[0][:160]}")
    else:
        print("[!] URI list empty — check plan_*.json for actual schema and adjust parser")
    print("=" * 56)
    return 0 if all_plans else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        sys.exit(130)
