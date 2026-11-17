---
name: drachtio-warm-transfer
description: "Use when building warm transfer (park, MOH, briefing, return) for a SIP AI agent."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [drachtio, warm-transfer, sip-to-ai, moh, briefing, easybell, call-control]
    category: telephony
    related_skills: [sip-to-realtime-bridge, jambonz-voice-apps, livekit-voice-agents]
---

# Warmtransfer für einen SIP-KI-Agenten (drachtio + Adapter, gebaut und getestet)

**Status: läuft** — 17.11.2026 auf einem Essential-2-VPS (2 vCPU, 2 GB) mit
easybell-Trunk, Agent auf Inworld Realtime. Repo: `drachtio-warmtransfer`
(GitHub `livedialai`, Codeberg `weserai`).

## When to Use

- Ein SIP-KI-Agent soll Warmtransfer können: Anrufer parken (Musik), Mitarbeiter
  anrufen, Briefing, bei Zustimmung durchstellen, bei Ablehnung zurück zur KI.
- Kein LiveKit, kein jambonz im Pfad — Call-Control selbst gebaut.
- Jemand will rtpengine als Medienanker einsetzen: **erst die Warnung unten lesen**.

## Architektur, die funktioniert (SDP-Passthrough)

```
Carrier ──SIP──▶ drachtio (registriert selbst, Port 5060, Admin 9022)
                   │  JS-Router (drachtio-srf), 127.0.0.1:3001
                   ▼  createUAC mit dem SDP des Anrufers, createUAS mit dem SDP der KI
                 Adapter (sip-to-ai, 212.87.214.9:6060)  ── RTP direkt zum Carrier
                   │
                   └─ Transfer im Adapter: zweites Bein über denselben Trunk,
                      MOH / Briefing / Bridge intern gemischt
```

**Kein rtpengine im Medienpfad.** drachtio macht nur Signalisierung, der Adapter
hält beide RTP-Ströme selbst. Das ist der entscheidende Unterschied zu einem
Medienanker-Setup.

## Die zwölf Fallen (alle real, alle teuer)

1. **rtpengine als Anker verschluckt den Uplink.** Downlink (KI→Anrufer) lief,
   Caller→KI kam nie an (`frames_received: 0`, „No caller RTP"). Ursache nie
   vollständig geklärt (to-tag, direction, Latching) — **Lösung: Anker weglassen,
   SDP durchreichen.**
2. **easybell liefert eingehende INVITEs an den Port, von dem registriert wurde**,
   nicht an den Contact. Ein separater Registrar auf 5085 bekommt die Anrufe, der
   SIP-Eingang bleibt stumm. → Der SIP-Server selbst muss registrieren.
3. **Zwei Registrierungen derselben Nummer werfen sich gegenseitig raus.**
   Nur ein Client registriert.
4. **easybell verlangt Auth im ersten INVITE** (sofort 407). Retry mit demselben
   SIP-`branch` wird als Dublette verworfen → jeder Versuch braucht einen **neuen
   Dialog** (neue Call-ID, neuer From-Tag, neuer branch) und ACK auf die 407.
5. **`100 Trying` ist kein Ergebnis.** Wer 1xx als final wertet, bricht den Anruf
   ab, während er gerade rausgeht.
6. **Fester lokaler SIP-Port für das Agent-Bein** → `[Errno 98] Address already in
   use` beim Folgeanruf, Transfer bricht still ab (kein MOH, kein Ruf).
   → ephemeren Port binden (Port 0), Adresse steht im SDP.
7. **Agent-Bein beim Anrufende nicht geschlossen** → Carrier behält den Dialog,
   nächster INVITE bekommt `491 Request Pending`. → im Call-Cleanup BYE senden.
8. **Roh-G.711 ohne RTP-Header** an das Agent-Bein geschickt → Carrier verwirft
   alles (Stille beim Mitarbeiter). → 12-Byte-RTP-Header, Sequenz, Zeitstempel
   (+160/Paket), SSRC, Payload-Typ aus dem Agent-SDP (PCMA 8 / PCMU 0).
9. **KI liefert Audio in Schüben, schneller als Echtzeit.** Kleiner Puffer wirft
   bei Überlauf die ältesten Frames weg → Wörter mitten im Satz fehlen
   („Guten Tag … [Lücke] … Reklamation"). → Puffer ≥15 s, Ausgabe im 20-ms-Takt.
10. **Zwei Takte gegeneinander**: Wartemusik einzeln im 20-ms-Rhythmus in eine
    RTP-Session einspeisen, die selbst schon 20 ms taktet → Abrisse. → in 200-ms-
    Blöcken einspeisen und die Session takten lassen.
11. **Ankündigung „Einen Moment bitte" aus dem Live-Dialog landet beim Mitarbeiter.**
    → Ansage deterministisch als Datei abspielen (TTS vorab erzeugen), KI in dem
    Moment stummschalten; ihr Prompt sagt: Transfer-Tool sofort aufrufen, nichts
    zum Anrufer sagen.
12. **Mute-Fenster (600 ms) nach dem Umschalten** verschluckt die ersten Wörter
    des Briefings. → nicht mit Zeitfenster arbeiten, sondern Puffer füllen
    (400 ms) und **erst dann** senden (`_briefing_ready`).

## Audio-Routing im Adapter (Zustände)

```
mode "ai"        Anrufer ↔ KI (Normalbetrieb)
mode "moh"       Anrufer hört Ansage + Musik; KI-Audio wird verworfen
mode "briefing"  KI ↔ Mitarbeiter; Anrufer hört weiter Musik;
                 das Anrufer-Audio wird NICHT zum Mitarbeiter gemischt
mode "bridge"    Anrufer ↔ Mitarbeiter; KI ist raus
Rückweg          Agent-Bein endet (Reject/Timeout/Hangup) → mode "ai",
                 kein SIP zum Carrier nötig
```

Wichtig: im Briefing darf das Anrufer-Audio nicht in den Agent-Stream — zwei
Ströme auf einem RTP-Port ohne gemeinsamen Takt klingen wie Abrisse und machen
die ASR des Agenten unbrauchbar.

## Prompt-Regeln (sonst wird das Briefing generisch)

- Transfer-Tool **sofort** aufrufen, ohne Ankündigung (die macht die Ansage-Datei).
- Nach dem Aufruf mit dem Mitarbeiter sprechen: „Guten Tag, hier ist <Name> vom
  <Ort>. Es geht um <konkretes Anliegen mit Details>. Können Sie das Gespräch
  übernehmen?"
- Auf die Antwort **warten**; erst bei klarer Zustimmung durchstellen.
- Bei Ablehnung/Stille abbrechen und dem Anrufer weiterhelfen.
- Tool-Feld `grund` muss die Details aus dem Gespräch enthalten (Name, Artikel,
  Mengen, Termin), nicht „Kunde möchte einen Mitarbeiter sprechen".

## Trunk-Notizen (easybell, getestet)

```
Registrar      voip.easybell.de (A-Record 195.185.187.16), UDP 5060
Auth           Digest, im ersten INVITE mitsenden
Zielformat     E.164 mit Plus: +4915…
From/Contact   Auth-User (K…), P-Preferred-Identity = eigene Nummer
REGISTER       sparsam erneuern (Expires 300, Refresh 100 s); Hämmern führt zu
               stillen Aussetzern, dann 408
Firewall       5060/udp + RTP-Bereich nur von der Carrier-IP
```

## Eigener Code (Ist-Stand)

```
router/index.js               251 Z.   drachtio-Signalisierung, SDP-Passthrough, ACL
app/bridge/transfer.py        447 Z.   MOH, Briefing, Bridge, RTP-Sender, Rückweg
app/sip_async/trunk.py        370 Z.   REGISTER + INVITE mit Digest (kleiner Stack)
Änderungen im Adapter         163 Z.   audio_bridge, tools, main, async_call, sip_server
Systemd/Config                 82 Z.
                            ≈ 1.230 Z. eigener Code
```

## Verification

```bash
# 1. Registrierung
grep REGISTER /var/log/wt-router.log | tail -1        # 200 OK
# 2. Eingehender Anruf
tail -3 /var/log/wt-router.log                        # BRIDGE Call-ID=… (Pass-Through)
# 3. Transfer-Signalisierung
grep "INVITE Antwort" /var/log/sip-to-ai.log | tail   # 183 → 180 → 200
# 4. Audio-Zähler während des Briefings
grep "transfer stats" /var/log/sip-to-ai.log | tail   # ai_frames/agent_frames steigen
# 5. Fehlerbilder
grep -E "Errno 98|491|Transfer-Start fehlgeschlagen" /var/log/sip-to-ai.log
```

## Was noch offen ist

- Qualität bleibt unter einer FreeSWITCH-Kette (jambonz Cascade): gelegentliche
  Jitter-Artefakte sind mit Python-RTP nicht ganz wegzubekommen.
- Kein Rückweg-Test mit echtem Auflegen protokolliert (Pfad ist implementiert).
- Keine Mehrfach-Transfers pro Anruf getestet.
