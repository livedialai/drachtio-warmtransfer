"""Warmtransfer im Adapter selbst - ohne rtpengine, ohne drachtio.

Der Adapter haelt das Anrufer-Bein bereits (RTP + SIP). Fuer den Transfer baut
er ueber den Trunk ein zweites Bein zum Mitarbeiter auf und mischt intern:

  moh       Anrufer hoert Wartemusik, KI-Audio wird verworfen
  briefing  KI spricht mit dem Mitarbeiter (Anrufer hoert weiter MOH)
  bridge    Anrufer und Mitarbeiter sind direkt verbunden, KI ist raus

Rueckweg: bricht der Mitarbeiter ab (Reject/Timeout/Auflegen), geht es ohne
SIP-Aktion zurueck in den KI-Dialog - der Anrufer merkt nur, dass die Musik
aufhoert.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from collections import deque
import wave
from typing import Any, Optional

import structlog

from app.sip_async.trunk import TrunkClient, TrunkConfig, build_sdp
from app.utils.codec import convert_g711_to_pcm16, convert_pcm16_to_g711

logger = structlog.get_logger(__name__)

FRAME_BYTES = 320


def parse_sdp_payload_type(sdp: str) -> int:
    """Waehlt den Payload-Typ aus dem SDP (PCMA bevorzugt, sonst PCMU)."""
    for line in sdp.splitlines():
        line = line.strip()
        if not line.startswith("m=audio "):
            continue
        parts = line.split()
        pts = []
        for token in parts[3:]:
            try:
                pts.append(int(token))
            except ValueError:
                continue
        if 8 in pts:
            return 8
        if 0 in pts:
            return 0
        if pts:
            return pts[0]
    return 8


def parse_sdp_media(sdp: str) -> tuple[str, int] | None:
    """Liest c= und m=audio aus einem SDP."""
    ip = None
    port = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio "):
            try:
                port = int(line.split()[1])
            except (IndexError, ValueError):
                port = None
    if ip and port:
        return (ip, port)
    return None  # 160 Samples * 2 Byte = 20 ms @ 8 kHz


class TransferController:
    """Steuert das zweite Bein und die Audio-Wege waehrend eines Transfers."""

    def __init__(self, rtp: Any, adapter: Any, moh_path: str) -> None:
        self.rtp = rtp
        self.adapter = adapter
        self.moh_path = moh_path

        self.mode = "ai"          # ai | moh | briefing | bridge
        self._trunk: Optional[TrunkClient] = None
        self._dialog: Optional[dict] = None
        self._agent_sock: Optional[socket.socket] = None
        self._agent_addr: Optional[tuple[str, int]] = None
        self._tasks: list[asyncio.Task[None]] = []

        self._moh_frames: list[bytes] = []
        self._moh_idx = 0

        self._agent_pt = 8          # aus dem SDP der Agent-Leg-Antwort
        self._last_agent_send = 0.0
        self._last_ai_audio = 0.0
        self._size_counts = 0
        self._mute_ai_until = 0.0
        self._silence = b""
        self._agent_tx = deque(maxlen=150)
        self._seq = 0
        self._ts = 0
        self._ssrc = 0x5A17C0DE

        self._caller_frames = 0
        self._ai_frames = 0
        self._agent_frames = 0

    # ------------------------------------------------------------------ Setup

    def _load_moh(self) -> None:
        if self._moh_frames or not os.path.exists(self.moh_path):
            return
        with wave.open(self.moh_path, "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                logger.warning("MOH nicht 8 kHz mono s16le", path=self.moh_path)
                return
            data = w.readframes(w.getnframes())
        self._moh_frames = [
            data[i:i + FRAME_BYTES] for i in range(0, len(data) - FRAME_BYTES, FRAME_BYTES)
        ]
        logger.info("MOH geladen", frames=len(self._moh_frames))

    def _trunk_config(self) -> TrunkConfig:
        return TrunkConfig(
            domain=os.getenv("TRUNK_DOMAIN", "voip.easybell.de"),
            user=os.getenv("TRUNK_USER", ""),
            password=os.getenv("TRUNK_PASS", ""),
            caller_id=os.getenv("TRUNK_CLI", ""),
            # ephemerer Port: feste Ports kollidieren, wenn ein Transfer noch
            # nicht abgeraeumt ist ([Errno 98] Address already in use)
            local_port=int(os.getenv("TRANSFER_LOCAL_PORT", "0")),
        )

    # --------------------------------------------------------------- Steuerung

    async def start(self, target: str) -> dict[str, Any]:
        """Parkt den Anrufer (MOH) und baut das Bein zum Mitarbeiter auf."""
        if self.mode != "ai":
            return {"ok": True, "status": "laeuft_bereits", "mode": self.mode}

        self._load_moh()
        self.mode = "moh"
        self._start_task(self._moh_task())

        try:
            self._trunk = TrunkClient(self._trunk_config())
            await self._trunk.open()
            # Kein REGISTER: der Anruf kommt ueber den bestehenden Kontakt rein.
            # Eine zweite Registrierung wuerde den Inbound-Kontakt ueberschreiben.
            if os.getenv("TRANSFER_REGISTER", "0") == "1":
                if not await self._trunk.register():
                    logger.warning("Trunk-Registrierung fuer Transfer fehlgeschlagen")

            self._agent_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._agent_sock.bind(("0.0.0.0", 0))
            self._agent_sock.setblocking(False)
            agent_port = self._agent_sock.getsockname()[1]

            cfg = self._trunk.cfg
            sdp = build_sdp(cfg.local_ip, agent_port)
            dialog = await self._trunk.invite(target, sdp, timeout=35.0)
            status = int(dialog.get("status") or 0)

            if status != 200:
                logger.info("Transfer-Ziel nicht erreicht", status=status, reason=dialog.get("reason"))
                await self._reset()
                return {"ok": False, "status": status, "reason": dialog.get("reason", "")}

            self._dialog = dialog
            self._agent_addr = parse_sdp_media(dialog.get("sdp", ""))
            self._agent_pt = parse_sdp_payload_type(dialog.get("sdp", ""))
            logger.info("Transfer-Bein steht", target=target, agent_rtp=self._agent_addr)

            # Restpuffer der KI verwerfen: der Satz an den Anrufer darf nicht
            # beim Mitarbeiter landen
            try:
                self.adapter.clear_downlink()
            except Exception:  # noqa: BLE001
                pass
            # Nachhall der Ankuendigung an den Anrufer nicht an den Mitarbeiter
            self._mute_ai_until = time.monotonic() + 0.6
            self.mode = "briefing"

            # Kurz vorfuellen, damit der Start des Briefings nicht stockt
            prime_deadline = time.monotonic() + 1.5
            while time.monotonic() < prime_deadline and len(self._agent_tx) < 10:
                await asyncio.sleep(0.05)
            self._start_task(self._agent_recv_task())
            self._start_task(self._agent_keepalive_task())
            return {"ok": True, "status": "briefing", "agent_rtp": list(self._agent_addr)}

        except Exception as e:  # noqa: BLE001
            logger.error("Transfer-Start fehlgeschlagen", error=str(e), exc_info=True)
            await self._reset()
            return {"ok": False, "status": "fehler", "reason": str(e)}

    async def complete(self) -> dict[str, Any]:
        """Durchstellen: Anrufer und Mitarbeiter direkt verbinden."""
        if self.mode != "briefing" or self._agent_addr is None:
            return {"ok": False, "status": "kein_briefing"}
        self.mode = "bridge"
        logger.info("Transfer durchgestellt")
        return {"ok": True, "status": "bridge"}

    async def cancel(self) -> dict[str, Any]:
        """Rueckweg: Mitarbeiter auflegen, Anrufer landet wieder bei der KI."""
        logger.info("Transfer abgebrochen - Rueckweg zur KI", mode=self.mode)
        await self._reset()
        return {"ok": True, "status": "zurueck_bei_ki"}

    async def _reset(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self.mode = "ai"

        if self._dialog and self._trunk:
            try:
                await self._trunk.bye(self._dialog)
            except Exception:  # noqa: BLE001
                pass
        self._dialog = None

        if self._trunk:
            self._trunk.close()
            self._trunk = None
        if self._agent_sock:
            self._agent_sock.close()
            self._agent_sock = None
        self._agent_addr = None

    # ---------------------------------------------------------------- Audio

    def _silence_payload(self) -> bytes:
        if not self._silence:
            mute = b"\x00" * FRAME_BYTES
            self._silence = convert_pcm16_to_g711(mute, "alaw" if self._agent_pt == 8 else "ulaw")
        return self._silence

    def _rtp_packet(self, g711: bytes) -> bytes:
        """Baut ein RTP-Paket (12-Byte-Header) fuer das Agent-Bein."""
        header = struct.pack(
            "!BBHII",
            0x80,                 # Version 2
            self._agent_pt & 0x7F,
            self._seq & 0xFFFF,
            self._ts & 0xFFFFFFFF,
            self._ssrc,
        )
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + len(g711)) & 0xFFFFFFFF
        return header + g711

    def _send_to_agent(self, pcm: bytes) -> None:
        if not (self._agent_sock and self._agent_addr):
            return
        self._last_agent_send = time.monotonic()
        payload = convert_pcm16_to_g711(pcm, "alaw" if self._agent_pt == 8 else "ulaw")
        try:
            self._agent_sock.sendto(self._rtp_packet(payload), self._agent_addr)
        except OSError:
            return

    def note_ai_audio(self) -> None:
        """Merkt sich, wann die KI zuletzt Audio geliefert hat."""
        self._last_ai_audio = time.monotonic()

    def on_caller_pcm(self, pcm: bytes) -> None:
        """Audio des Anrufers (Uplink) waehrend eines Transfers."""
        # MOH und Briefing: Anrufer ist geparkt, sein Audio wird nicht weitergegeben
        if self.mode != "bridge":
            return
        if self._agent_sock and self._agent_addr:
            self._agent_tx.append(pcm)
            self._caller_frames += 1

    def _log_sizes(self, pcm: bytes, quelle: str) -> None:
        self._size_counts += 1
        if self._size_counts <= 12:
            logger.info("audio frame", quelle=quelle, bytes=len(pcm), mode=self.mode)

    def on_ai_pcm(self, pcm: bytes) -> bool:
        """KI-Audio (Downlink). True = selbst verarbeitet, nicht an den Anrufer."""
        if self.mode == "ai":
            return False
        if self.mode == "briefing" and time.monotonic() < self._mute_ai_until:
            return True
        if self.mode == "briefing" and self._agent_sock and self._agent_addr:
            self._log_sizes(pcm, "ki")
            self._agent_tx.append(pcm)
            self._ai_frames += 1
        return True

    def should_send_moh(self) -> bool:
        return self.mode in ("moh", "briefing")

    async def _moh_task(self) -> None:
        """Schickt Wartemusik an den Anrufer, solange er geparkt ist.

        Die RTP-Session taktet selbst mit 20 ms; deshalb werden 200-ms-Bloecke
        eingespeist, damit Jitter nicht als Luecke hoerbar wird.
        """
        loop = asyncio.get_running_loop()
        block = 10
        while True:
            if not self.should_send_moh() or not self._moh_frames:
                await asyncio.sleep(0.05)
                continue
            if self._moh_idx and self._moh_idx % 150 == 0:
                logger.info("transfer stats", **self.stats(), agent_rtp=self._agent_addr, pt=self._agent_pt)
            for _ in range(block):
                frame = self._moh_frames[self._moh_idx % len(self._moh_frames)]
                self._moh_idx += 1
                try:
                    await self.rtp.send_audio(frame)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.2)
            _ = loop  # keep reference for clarity

    async def _agent_keepalive_task(self) -> None:
        """Haelt den RTP-Strom zum Mitarbeiter offen (Stille, wenn die KI schweigt)."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            next_tick += 0.02
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = loop.time()
            if self.mode in ("briefing", "bridge") and self._agent_sock and self._agent_addr:
                # genau ein Frame pro 20 ms: echter Audioframe, sonst Stille
                pcm = self._agent_tx.popleft() if self._agent_tx else None
                if pcm is None:
                    payload = self._silence_payload()
                else:
                    payload = convert_pcm16_to_g711(
                        pcm, "alaw" if self._agent_pt == 8 else "ulaw"
                    )
                try:
                    self._agent_sock.sendto(self._rtp_packet(payload), self._agent_addr)
                except OSError:
                    pass

    async def _agent_recv_task(self) -> None:
        """Audio des Mitarbeiters: Briefing -> KI, Bridge -> Anrufer."""
        assert self._agent_sock is not None
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, _addr = await asyncio.wait_for(
                    loop.sock_recvfrom(self._agent_sock, 2048), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except OSError:
                return
            if len(data) < 13:
                continue
            payload = data[12:]
            if not payload:
                continue
            pcm = convert_g711_to_pcm16(payload, "alaw" if self._agent_pt == 8 else "ulaw")
            self._agent_frames += 1
            if self.mode == "briefing":
                self.adapter.on_rx_pcm16_8k(pcm)
            elif self.mode == "bridge":
                try:
                    await self.rtp.send_audio(pcm)
                except Exception:  # noqa: BLE001
                    pass

    def _start_task(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append(task)

    def stats(self) -> dict[str, int | str]:
        return {
            "mode": self.mode,
            "caller_frames": self._caller_frames,
            "ai_frames": self._ai_frames,
            "agent_frames": self._agent_frames,
        }


ACTIVE: dict[str, "TransferController"] = {}


def get(call_id: str) -> "TransferController | None":
    """Liefert den Transfer-Controller zu einer Call-ID."""
    return ACTIVE.get(str(call_id))


__all__ = ["TransferController", "ACTIVE", "get", "FRAME_BYTES"]
