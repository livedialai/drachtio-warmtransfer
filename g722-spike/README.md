# G.722-Spike (HD-Messung)

Ziel: messen, ob der Weg Carrier -> Adapter wirklich 16 kHz liefert,
statt es nur im SDP zu verhandeln.

## Was drin ist

- `g722.py` — Decoder/Encoder (libavcodec via PyAV) + Bandenergie-Messung
  (300-3400 Hz vs 4000-7800 Hz)
- `rtp_session.py.patched` — Payload-Typ 9 wird dekodiert (16 kHz),
  Messung alle ~5 s, danach fuer die KI auf 8 kHz heruntergerechnet
- `sdp.py` — nimmt G.722 an, wenn `G722_ENABLE=1`
- `test_g722.py` — Selbsttest: sendet 8 s einen 6-kHz-Ton als G.722

## Selbsttest (bewiesen)

```
INVITE mit m=audio ... RTP/AVP 9 101   -> 200 OK, payload_type 9
G.722 dekodiert: 160 Byte -> 640 Byte PCM16 (320 Samples @ 16 kHz)
HD-Messung: sprach_db 109.9 | hoch_db 131.1 | differenz +21.2 dB
```

Ein 6-kHz-Ton liegt komplett oberhalb von 4 kHz - die +21 dB beweisen,
dass der Decoder echte 16 kHz liefert und die Messung anschlaegt.

## Feldtest

Anruf von einem Festnetzanschluss (FRITZ!Box, HD-faehig) und Vergleich mit
einem Handyanruf. Erwartung bei echtem HD: hoher Bandanteil nur wenige dB
unter dem Sprachband (statt 30-60 dB darunter).
