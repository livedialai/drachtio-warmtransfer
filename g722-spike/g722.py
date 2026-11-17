"""G.722 (HD-Telefonie, 16 kHz) ueber libavcodec/PyAV.

G.722 ist ein Subband-ADPCM mit 64 kbit/s: 160 Byte RTP-Payload = 20 ms.
Dekodiert ergibt das 320 Samples bei 16 kHz (PCM16, 640 Byte).

Der Adapter arbeitet intern mit 8 kHz. Fuer die HD-Messung wird das 16-kHz-Signal
zusaetzlich im Original ausgewertet (Bandenergie ueber 4 kHz), bevor es fuer die
KI-Kette auf 8 kHz heruntergerechnet wird.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import numpy as np

try:  # PyAV ist optional - ohne av gibt es kein G.722
    import av
except Exception:  # noqa: BLE001
    av = None  # type: ignore

PT_G722 = 9


class G722Codec:
    """Decoder/Encoder fuer G.722 (eine Instanz pro RTP-Session)."""

    def __init__(self) -> None:
        self._dec = None
        self._enc = None
        if av is None:
            return
        try:
            self._dec = av.CodecContext.create("g722", "r")
        except Exception:  # noqa: BLE001
            self._dec = None
        try:
            enc = av.CodecContext.create("g722", "w")
            enc.sample_rate = 16000
            enc.format = "s16"
            enc.layout = "mono"
            enc.open()
            self._enc = enc
        except Exception:  # noqa: BLE001
            self._enc = None

    @property
    def available(self) -> bool:
        return self._dec is not None and self._enc is not None

    def decode(self, payload: bytes) -> bytes:
        """RTP-Payload (160 Byte) -> PCM16 @ 16 kHz."""
        if self._dec is None:
            return b""
        out = bytearray()
        try:
            packet = av.Packet(payload)
            for frame in self._dec.decode(packet):
                out.extend(bytes(frame.to_ndarray().astype("<i2").tobytes()))
        except Exception:  # noqa: BLE001
            return b""
        return bytes(out)

    def encode(self, pcm16_16k: bytes) -> bytes:
        """PCM16 @ 16 kHz -> RTP-Payload."""
        if self._enc is None:
            return b""
        out = bytearray()
        try:
            arr = np.frombuffer(pcm16_16k, dtype="<i2")
            if arr.size == 0:
                return b""
            frame = av.AudioFrame.from_ndarray(
                arr.reshape(1, -1), format="s16", layout="mono"
            )
            frame.sample_rate = 16000
            for packet in self._enc.encode(frame):
                out.extend(bytes(packet))
        except Exception:  # noqa: BLE001
            return b""
        return bytes(out)


def band_energy_db(pcm16_16k: bytes) -> tuple[float, float]:
    """Energie (dB) im Sprachband und oberhalb von 4 kHz.

    Returns: (db_sprache_300_3400, db_hoch_4000_7800)
    """
    if not pcm16_16k:
        return -120.0, -120.0
    samples = np.frombuffer(pcm16_16k, dtype="<i2").astype(np.float32)
    if samples.size < 128:
        return -120.0, -120.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size))) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1.0 / 16000.0)
    sprach = spectrum[(freqs >= 300) & (freqs <= 3400)].sum()
    hoch = spectrum[(freqs >= 4000) & (freqs <= 7800)].sum()
    eps = 1e-9
    return (
        float(10 * np.log10(sprach + eps)),
        float(10 * np.log10(hoch + eps)),
    )

def hf_flatness(pcm16_16k: bytes) -> float:
    """Spektrale Flachheit im Band 4-7,8 kHz (0 = klanglich, 1 = Rauschen).

    Damit laesst sich unterscheiden, ob oberhalb von 4 kHz echte Sprache
    liegt oder nur das Rauschen eines hochgerechneten Schmalbandsignals.
    """
    if len(pcm16_16k) < 512:
        return 0.0
    samples = np.frombuffer(pcm16_16k, dtype="<i2").astype(np.float64)
    if samples.size < 512:
        return 0.0
    spec = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    freqs = np.fft.rfftfreq(samples.size, d=1 / 16000.0)
    band = spec[(freqs >= 4000) & (freqs <= 7800)]
    if band.size < 4 or band.max() <= 0:
        return 0.0
    band = band[band > 1e-9]
    if band.size < 4:
        return 0.0
    geo = float(np.exp(np.mean(np.log(band))))
    arith = float(np.mean(band))
    return geo / arith if arith > 0 else 0.0

_HD_CSV = os.getenv("HD_CSV", "/var/log/wt-hd.csv")


def append_hd_csv(port: int, frames: int, sprach: float, hoch: float,
                  diff: float, flach: float, path: str = "") -> None:
    """Schreibt eine Messzeile direkt in eine CSV - unabhaengig vom Log-Sink."""
    ziel = path or _HD_CSV
    neu = not os.path.exists(ziel)
    try:
        with open(ziel, "a", encoding="utf-8") as fh:
            if neu:
                fh.write("ts,local_rtp_port,frames,sprach_db,hoch_db,diff_db,hf_flach\n")
            fh.write(
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},"
                f"{port},{frames},{sprach:.1f},{hoch:.1f},{diff:.1f},{flach:.3f}\n"
            )
    except OSError:
        pass
