# drachtio-warmtransfer

Warmtransfer für einen SIP-KI-Agenten: eingehender Anruf → KI (Inworld Realtime)
→ auf Wunsch Transfer zu einem Mitarbeiter, mit Wartemusik, Briefing und
Rückweg zur KI, wenn der Mitarbeiter ablehnt oder auflegt.

Stand: **17.11.2026** — läuft auf einem PPH Essential 2 (2 vCPU, 2 GB), Debian 12.

## Was funktioniert

- Eingehender Anruf über easybell-SIP-Trunk (`004942143818790`)
- Begrüßung und Dialog mit der KI (Inworld Realtime, PCMA vom Carrier wird bedient)
- Transfer-Start: Anrufer hört Wartemusik, zweites Bein wird zu `+491…8398` aufgebaut
- Signalisierung des zweiten Beins: `100 → 407 → Auth → 183 → 180 → 200 OK`
- Rückweg, wenn das Agent-Bein endet (KI übernimmt wieder, Anrufer bleibt in der Leitung)

## Stand 17.11.2026 (final)

- Eingehender Anruf, Opener, Dialog, Transfer, MOH, Briefing, Durchstellen und
  Rueckweg laufen am Telefon.
- Ansage "Einen Moment bitte" ist eine feste TTS-Datei (kein Live-Dialog mehr).
- Briefing-Audio wird gepuffert (15 s) und im 20-ms-Takt gesendet, damit die
  KI-Schuebe keine Woerter verschlucken.
- Umfang eigener Code: ~1.230 Zeilen (Router 251, Transfer 447, SIP-Client 370,
  Adapter-Aenderungen 163) plus 82 Zeilen Systemd/Config.

Details und die zwoelf Fallstricke: siehe `skill/SKILL.md`.

## Historisch: was nicht funktionierte (aktualisiert 12:35)

- **Briefing-Audio**: der Mitarbeiter hörte die KI nicht. Ursache gefunden:
  der `TransferController` schickte **rohe G.711-Bytes ohne RTP-Header** an das
  Agent-Bein — der Carrier verwirft das. Jetzt wird paketiert (12-Byte-RTP-Header,
  Sequenz, Zeitstempel, SSRC) und der Payload-Typ aus dem Agent-SDP übernommen
  (PCMA bevorzugt). Zusätzlich fehlte `TRUNK_LOCAL_IP` — das Agent-SDP warb
  `0.0.0.0` als Medienadresse an, damit kann keine Seite Audio schicken.
  Jetzt: `TRUNK_LOCAL_IP=212.87.214.9`, plus Statistik-Log alle 3 s
  (`caller_frames`, `ai_frames`, `agent_frames`, `agent_rtp`, `pt`).

## Architektur

```
Carrier (easybell)
   │  SIP 5060
   ▼
drachtio  ──── registriert den Trunk selbst (Port 5060!)
   │  Admin 9022, JS-Router auf 127.0.0.1:3001
   ▼
router (Node, drachtio-srf)
   │  SDP-Passthrough: kein Medienanker, kein rtpengine
   ▼
sip-to-ai Adapter (Python, Inworld Realtime)
   │  SIP/UDP 212.87.214.9:6060, RTP direkt zum Carrier
   ▼
Inworld Realtime (wss)

Transfer: Adapter baut das zweite Bein selbst über denselben Trunk auf
(eigener kleiner SIP-Stack, Digest-Auth inkl. 407-Retry) und mischt intern:
MOH → Briefing (KI ↔ Mitarbeiter) → Bridge (Anrufer ↔ Mitarbeiter).
```

### Wichtige Erkenntnisse (teuer erkauft)

1. **rtpengine als Medienanker hat den Uplink verschluckt.** Caller→KI kam nie an
   (`frames_received: 0`), Downlink lief. Ursache nicht abschließend geklärt.
   Lösung: SDP-Passthrough — drachtio macht nur Signalisierung, RTP läuft direkt
   zwischen Carrier und Adapter.
2. **easybell schickt eingehende INVITEs an den Port, von dem aus registriert
   wurde** — nicht an den Contact. Ein separater Registrar auf einem anderen Port
   bekommt die Anrufe und der eigentliche SIP-Eingang bleibt stumm. Also: der
   SIP-Server selbst muss registrieren.
3. **easybell verlangt die Authentifizierung im ersten INVITE** (407 sofort), und
   ein Retry mit demselben SIP-`branch` wird als Dublette verworfen. Jeder Versuch
   braucht einen neuen Dialog (neue Call-ID, neuer branch).
4. **`100 Trying` ist keine finale Antwort.** Wer 1xx als Ergebnis wertet, bricht
   den Anruf ab, während er gerade erst rausgeht.
5. **Zwei Registrierungen auf dieselbe Nummer werfen sich gegenseitig raus.**

## Dienste auf der Kiste (212.87.214.9)

| Dienst | Aufgabe | Port |
|---|---|---|
| `drachtio` | SIP-Server, registriert den Trunk | 5060/udp, Admin 9022 |
| `router` | Node-Router, SDP-Passthrough, ACL | 127.0.0.1:3001 |
| `s2a` | sip-to-ai Adapter (Inworld) | 212.87.214.9:6060 |
| `rtpengine` | **nicht mehr im Pfad** (nur noch installiert) | 127.0.0.1:22222 |
| `registrar` | **deaktiviert** (siehe Erkenntnis 2) | — |

Firewall: 5060/udp und RTP 30000–40000 nur von easybell (195.185.187.16),
localhost und der eigenen IP.

## Repo-Inhalt

```
router/                     Node-Router (drachtio-srf, SDP-Passthrough)
systemd/                    Units für drachtio, router, s2a (registrar: deaktiviert)
config/drachtio.conf.xml    drachtio-Konfiguration
config/router.env.example   Umgebungsvariablen (ohne Zugangsdaten)
patches/0001-…patch         Änderungen am sip-to-ai Adapter
patches/transfer.py         TransferController (MOH, Briefing, Bridge, Rückweg)
patches/trunk.py            kleiner SIP-Client (REGISTER + INVITE mit Digest)
```

## Offene Punkte

- Briefing-Audio (Codec/Payload des Agent-Bein-SDP übernehmen)
- Rückweg testen (Agent legt auf → KI übernimmt)
- Durchstellen testen (Bridge Anrufer ↔ Mitarbeiter)
- `opener.wav` fehlt, Begrüßung läuft über TTS

## Abhaengigkeiten, gespiegelt

- Fork: https://github.com/livedialai/drachtio-server (MIT, voller Upstream-Verlauf)
- Spiegel: https://codeberg.org/weserai/drachtio-server
- Fertiges Binary: Release `cd1a1d0-x86_64` im Fork (3,2 MB, gestrippt)
- Build-Anleitung inkl. Stolpersteine (`libtool-bin`, Swap bei 2 GB, jambonz-Fork-Falle):
  `warmtransfer/BUILD.md`
