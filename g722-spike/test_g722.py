"""Selbsttest G.722: sendet einen 6-kHz-Ton als G.722 an den Adapter.

Erwartung im Adapter-Log: "HD-Messung (G.722)" mit deutlich hoeherer
Energie oberhalb 4 kHz als im Sprachband. Damit ist bewiesen, dass der
Decoder wirklich 16 kHz liefert (und nicht nur 8 kHz hochgerechnet).
"""
import math
import socket
import struct
import time

import av
import numpy as np

TARGET = ("212.87.214.9", 5060)
LOCAL = "212.87.214.9"
SIP_PORT = 5095
RTP_PORT = 41100
RTP_TARGET = ["212.87.214.9", 0]
CID = f"g722test-{int(time.time())}@127.0.0.1"

# --- G.722 Encoder -----------------------------------------------------------
enc = av.CodecContext.create("g722", "w")
enc.sample_rate = 16000
enc.format = "s16"
enc.layout = "mono"
enc.open()


def tone_payload(freq: float = 6000.0, ms: int = 20) -> bytes:
    n = int(16000 * ms / 1000)
    t = np.arange(n) / 16000.0
    pcm = (0.4 * 32767 * np.sin(2 * math.pi * freq * t)).astype("<i2")
    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = 16000
    out = b"".join(bytes(p) for p in enc.encode(frame))
    return out


PAYLOAD = tone_payload()
print(f"G.722 Payload: {len(PAYLOAD)} Byte pro 20 ms (6 kHz Ton)")

sdp = (
    "v=0\r\n"
    f"o=- 1 1 IN IP4 {LOCAL}\r\n"
    "s=probe\r\n"
    f"c=IN IP4 {LOCAL}\r\n"
    "t=0 0\r\n"
    f"m=audio {RTP_PORT} RTP/AVP 9 101\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=ptime:20\r\n"
    "a=sendrecv\r\n"
)

invite = (
    f"INVITE sip:4942143818790@{TARGET[0]}:{TARGET[1]} SIP/2.0\r\n"
    f"Via: SIP/2.0/UDP {LOCAL}:{SIP_PORT};branch=z9hG4bKg722\r\n"
    "Max-Forwards: 70\r\n"
    f"From: <sip:g722@{LOCAL}>;tag=g722tag\r\n"
    f"To: <sip:4942143818790@{TARGET[0]}>\r\n"
    f"Call-ID: {CID}\r\n"
    "CSeq: 1 INVITE\r\n"
    f"Contact: <sip:g722@{LOCAL}:{SIP_PORT}>\r\n"
    "Content-Type: application/sdp\r\n"
    f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
)

sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sip.bind((LOCAL, SIP_PORT))
sip.settimeout(8)
sip.sendto(invite.encode(), TARGET)

resp = b""
deadline = time.time() + 8
while time.time() < deadline:
    try:
        data, addr = sip.recvfrom(4096)
    except socket.timeout:
        break
    text = data.decode(errors="replace")
    code = text.split("\r\n")[0]
    print("<<", code)
    if code.startswith("SIP/2.0 200"):
        # RTP-Ziel aus dem SDP der Antwort lesen
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        for line in body.split("\r\n"):
            if line.startswith("m=audio "):
                RTP_TARGET[1] = int(line.split()[1])
        to_tag = ""
        for line in text.split("\r\n"):
            if line.lower().startswith("to:") and "tag=" in line:
                to_tag = line.split("tag=")[-1].strip()
        ack = (
            f"ACK sip:4942143818790@{TARGET[0]}:{TARGET[1]} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {LOCAL}:{SIP_PORT};branch=z9hG4bKg722ack\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:g722@{LOCAL}>;tag=g722tag\r\n"
            f"To: <sip:4942143818790@{TARGET[0]}>;tag={to_tag}\r\n"
            f"Call-ID: {CID}\r\n"
            "CSeq: 1 ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        sip.sendto(ack.encode(), TARGET)
        resp = "ok"
        break
    if code.startswith("SIP/2.0 4") or code.startswith("SIP/2.0 5"):
        break

if resp != "ok":
    print("kein 200 OK - Abbruch")
    raise SystemExit(1)

# --- 8 Sekunden G.722 senden -------------------------------------------------
rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp.bind((LOCAL, RTP_PORT))
seq = 0
ts = 0
ssrc = 0x1234ABCD
start = time.time()
print(f"sende 8 s 6-kHz-Ton als G.722 -> {RTP_TARGET}")
while time.time() - start < 8:
    header = struct.pack("!BBHII", 0x80, 9, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    rtp.sendto(header + PAYLOAD, (RTP_TARGET[0], RTP_TARGET[1]))
    seq += 1
    ts += 320
    time.sleep(0.02)

bye = (
    f"BYE sip:4942143818790@{TARGET[0]}:{TARGET[1]} SIP/2.0\r\n"
    f"Via: SIP/2.0/UDP {LOCAL}:{SIP_PORT};branch=z9hG4bKg722bye\r\n"
    "Max-Forwards: 70\r\n"
    f"From: <sip:g722@{LOCAL}>;tag=g722tag\r\n"
    f"To: <sip:4942143818790@{TARGET[0]}>\r\n"
    f"Call-ID: {CID}\r\n"
    "CSeq: 2 BYE\r\n"
    "Content-Length: 0\r\n\r\n"
)
sip.sendto(bye.encode(), TARGET)
print("fertig")
