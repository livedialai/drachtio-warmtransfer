"""Minimaler SIP-Trunk-Client (REGISTER + ausgehender INVITE mit Digest-Auth).

Wird vom Warmtransfer gebraucht: der Adapter selbst baut das zweite Bein zum
Mitarbeiter auf, ohne rtpengine und ohne drachtio.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


def _rand(n: int = 8) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _sdp_of(msg: str) -> str:
    """Liefert den SDP-Body einer SIP-Nachricht."""
    parts = msg.split("\r\n\r\n", 1)
    return parts[1] if len(parts) > 1 else ""


def _parse_headers(msg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in msg.split("\r\n")[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        if k in out:
            out[k] += ", " + v.strip()
        else:
            out[k] = v.strip()
    return out


def _digest_authorization(
    method: str,
    uri: str,
    challenge: str,
    username: str,
    password: str,
) -> str:
    """Baut den Authorization-Header aus einer WWW-Authenticate/Proxy-Authenticate-Zeile."""
    params: dict[str, str] = {}
    body = challenge.split(" ", 1)[1] if " " in challenge else ""
    for part in body.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        params[k.strip().lower()] = v.strip().strip('"')

    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop", "")
    opaque = params.get("opaque")
    algorithm = params.get("algorithm", "MD5")

    def h(x: str) -> str:
        return hashlib.md5(x.encode()).hexdigest()

    ha1 = h(f"{username}:{realm}:{password}")
    ha2 = h(f"{method}:{uri}")
    if "auth" in qop:
        nc = "00000001"
        cnonce = _rand(8)
        response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
        extra = ""
    hdr = (
        f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{response}", algorithm={algorithm}'
    )
    if opaque:
        hdr += f', opaque="{opaque}"'
    return hdr + extra


@dataclass
class TrunkConfig:
    domain: str = os.getenv("TRUNK_DOMAIN", "g.vsip.eu")
    user: str = os.getenv("TRUNK_USER", "")
    password: str = os.getenv("TRUNK_PASS", "")
    local_ip: str = os.getenv("TRUNK_LOCAL_IP", "0.0.0.0")
    local_port: int = int(os.getenv("TRUNK_LOCAL_PORT", "5065"))
    caller_id: str = os.getenv("TRUNK_CLI", "")
    from_user: str = os.getenv("TRUNK_FROM", "")  # From/Contact-User (oft die DID)
    contact_port: int = 0  # 0 = local_port; sonst z. B. 5060 (drachtio)


class TrunkClient:
    """SIP-Client für einen registrierenden Trunk (UDP, Digest-Auth)."""

    def __init__(self, cfg: TrunkConfig) -> None:
        self.cfg = cfg
        self._sock: Optional[socket.socket] = None
        self._last_challenge: Optional[str] = None
        self._last_challenge_is_proxy: bool = False
        self._remote: Optional[tuple[str, int]] = None

    # ------------------------------------------------------------------ Setup

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        info = await loop.getaddrinfo(self.cfg.domain, 5060, type=socket.SOCK_DGRAM)
        self._remote = info[0][4][:2]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.cfg.local_ip, self.cfg.local_port))
        s.setblocking(False)
        self._sock = s
        logger.info("trunk socket offen", remote=self._remote, local=self.cfg.local_port)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    # ------------------------------------------------------------------ Senden

    def _from_user(self) -> str:
        # drachtio nutzt den Auth-User im From; die DID steht nur in P-Preferred-Identity
        return self.cfg.user

    def _aor(self) -> str:
        return f"sip:{self._from_user()}@{self.cfg.domain}"

    def _contact(self) -> str:
        port = self.cfg.contact_port or self.cfg.local_port
        return f"sip:{self._from_user()}@{self.cfg.local_ip}:{port}"

    async def _send(self, data: bytes) -> None:
        assert self._sock and self._remote
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self._sock, data, self._remote)

    async def _recv(self, timeout: float) -> Optional[str]:
        assert self._sock
        loop = asyncio.get_running_loop()
        try:
            data = await asyncio.wait_for(loop.sock_recv(self._sock, 65535), timeout)
        except asyncio.TimeoutError:
            return None
        text = data.decode(errors="replace")
        if not text.startswith("SIP/2.0"):
            # eingehende Requests (z. B. INVITE) ignorieren - das ist keine Antwort
            return None
        return text

    # ------------------------------------------------------------------ REGISTER

    async def register(self) -> bool:
        call_id = f"{_rand(12)}@{self.cfg.local_ip}"
        uri = f"sip:{self.cfg.domain}"
        cseq = 1
        auth_hdr = ""
        for attempt in range(2):
            msg = (
                f"REGISTER {uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.cfg.local_ip}:{self.cfg.local_port};branch=z9hG4bK{_rand()}\r\n"
                "Max-Forwards: 70\r\n"
                f"From: <{self._aor()}>;tag={_rand()}\r\n"
                f"To: <{self._aor()}>\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq} REGISTER\r\n"
                f"Contact: <{self._contact()}>\r\n"
                "Expires: 300\r\n"
                + (f"{auth_hdr}\r\n" if auth_hdr else "")
                + "User-Agent: sip-to-ai\r\nContent-Length: 0\r\n\r\n"
            )
            await self._send(msg.encode())
            resp = None
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                candidate = await self._recv(max(1.0, deadline - time.monotonic()))
                if candidate is None:
                    break
                # 1xx sind Zwischenmeldungen — auf die finale Antwort warten
                status_line = candidate.split("\r\n")[0]
                parts = status_line.split(" ")
                if len(parts) > 1 and parts[1].isdigit() and parts[1].startswith("1"):
                    continue
                resp = candidate
                break
            if resp is None:
                logger.warning("REGISTER ohne Antwort")
                return False
            status = resp.split("\r\n")[0]
            logger.info("REGISTER Antwort", status=status)
            if " 200 " in status:
                return True
            headers = _parse_headers(resp)
            challenge = headers.get("www-authenticate") or headers.get("proxy-authenticate")
            if not challenge:
                return False
            auth_hdr = "Authorization: " + _digest_authorization(
                "REGISTER", uri, challenge, self.cfg.user, self.cfg.password
            )
            cseq += 1
        return False

    # ------------------------------------------------------------------ INVITE

    async def invite(self, target: str, sdp_body: str, timeout: float = 35.0) -> dict:
        """Baut einen ausgehenden Anruf auf.

        Jeder Versuch ist ein eigener Dialog (neue Call-ID, neuer From-Tag,
        neuer branch). Nach einer 401/407 wird die Antwort mit ACK bestaetigt
        und der naechste Versuch mit den Zugangsdaten aus der Challenge
        geschickt - so entstehen keine haengenden Transaktionen (491).
        """
        target_user = target.strip().replace(" ", "")
        uri = f"sip:{target_user}@{self.cfg.domain}"
        auth_hdr = ""
        last_status = 0
        last_reason = "kein Versuch"

        for _attempt in range(2):
            call_id = f"{_rand(16)}@{self.cfg.local_ip}"
            from_tag = _rand()
            branch = "z9hG4bK" + _rand()
            cseq = 1
            body = sdp_body

            extra = (auth_hdr + "\r\n") if auth_hdr else ""
            msg = (
                f"INVITE {uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.cfg.local_ip}:{self.cfg.local_port};branch={branch}\r\n"
                "Max-Forwards: 70\r\n"
                f"From: <{self._aor()}>;tag={from_tag}\r\n"
                f"To: <{uri}>\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq} INVITE\r\n"
                f"Contact: <{self._contact()}>\r\n"
                + (f"P-Preferred-Identity: <sip:{self.cfg.caller_id}@{self.cfg.domain}>\r\n"
                   if self.cfg.caller_id else "")
                + extra
                + "User-Agent: sip-to-ai\r\nContent-Type: application/sdp\r\n"
                f"Content-Length: {len(body)}\r\n\r\n{body}"
            )
            await self._send(msg.encode())

            challenge = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                resp = await self._recv(min(10.0, max(1.0, deadline - time.monotonic())))
                if resp is None:
                    continue
                status_line = resp.split("\r\n")[0]
                try:
                    code = int(status_line.split(" ")[1])
                except (IndexError, ValueError):
                    continue
                headers = _parse_headers(resp)
                logger.info("INVITE Antwort", code=code, reason=status_line[:80])
                last_status, last_reason = code, status_line

                if 401 <= code <= 407:
                    await self._ack(uri, call_id, from_tag, branch, cseq)
                    challenge = headers.get("www-authenticate") or headers.get("proxy-authenticate")
                    if not challenge:
                        return {"ok": False, "status": code, "reason": status_line}
                    hdr_name = "Authorization" if code == 401 else "Proxy-Authorization"
                    auth_hdr = hdr_name + ": " + _digest_authorization(
                        "INVITE", uri, challenge, self.cfg.user, self.cfg.password
                    )
                    self._last_challenge = challenge
                    self._last_challenge_is_proxy = code != 401
                    break  # neuer Dialog mit Auth

                if 100 <= code < 200:
                    # 100/180/183: weiter auf die finale Antwort warten
                    continue

                if code == 200:
                    to_hdr = headers.get("to", "")
                    to_tag = to_hdr.split("tag=")[-1].strip() if "tag=" in to_hdr else ""
                    await self._ack(uri, call_id, from_tag, branch, cseq, to_tag)
                    return {
                        "ok": True, "status": 200, "reason": status_line,
                        "call_id": call_id, "from_tag": from_tag, "to_tag": to_tag,
                        "uri": uri, "sdp": _sdp_of(resp),
                    }

                await self._ack(uri, call_id, from_tag, branch, cseq)
                return {"ok": False, "status": code, "reason": status_line}

            if challenge is None:
                return {"ok": False, "status": last_status, "reason": last_reason or "timeout"}

        return {"ok": False, "status": last_status, "reason": last_reason or "auth fehlgeschlagen"}

    async def _ack(self, uri: str, call_id: str, from_tag: str, branch: str,
                   cseq: int, to_tag: str = "") -> None:
        """Bestaetigt eine finale Antwort (verhindert haengende Transaktionen)."""
        to = f"<{uri}>" + (f";tag={to_tag}" if to_tag else "")
        msg = (
            f"ACK {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.cfg.local_ip}:{self.cfg.local_port};branch={branch}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <{self._aor()}>;tag={from_tag}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        try:
            await self._send(msg.encode())
        except OSError:
            pass

    async def bye(self, dialog: dict) -> None:
        uri, cid = dialog["uri"], dialog["call_id"]
        await self._send((
            f"BYE {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.cfg.local_ip}:{self.cfg.local_port};branch=z9hG4bK{_rand()}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <{self._aor()}>;tag={dialog['from_tag']}\r\n"
            f"To: <{uri}>;tag={dialog['to_tag']}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {dialog['cseq'] + 1} BYE\r\nContent-Length: 0\r\n\r\n"
        ).encode())


def build_sdp(local_ip: str, rtp_port: int, codec: int = 0) -> str:
    return (
        "v=0\r\n"
        f"o=- {int(time.time())} 1 IN IP4 {local_ip}\r\n"
        "s=sip-to-ai\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP {codec}\r\n"
        f"a=rtpmap:{codec} {'PCMU' if codec == 0 else 'PCMA'}/8000\r\n"
        "a=sendrecv\r\n"
    )


async def _self_test() -> None:
    """Kleiner Funktionstest: registriert und ruft eine Nummer an."""
    cfg = TrunkConfig(local_ip=os.getenv("TRUNK_LOCAL_IP", "212.87.214.9"))
    t = TrunkClient(cfg)
    await t.open()
    ok = await t.register()
    print("REGISTER:", ok)
    target = os.getenv("TEST_TARGET", "")
    if ok and target:
        sdp = build_sdp(cfg.local_ip, 40200)
        res = await t.invite(target, sdp)
        print("INVITE:", {k: v for k, v in res.items() if k != "remote_sdp"})
        if res.get("ok"):
            await asyncio.sleep(2)
            await t.bye(res)
            print("BYE gesendet")
    t.close()


if __name__ == "__main__":
    asyncio.run(_self_test())
