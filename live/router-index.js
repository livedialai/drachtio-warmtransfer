'use strict';

/**
 * drachtio + rtpengine Router für sip-to-ai mit Warmtransfer
 *  - Trunk-Register gegen voip2gsm (g.vsip.eu)
 *  - eingehender INVITE -> rtpengine -> sip-to-ai (127.0.0.1:6060)
 *  - Warmtransfer: parken + MOH, Briefing am Mitarbeiter, Rueckweg, Durchstellen
 *
 * Session-Layout (rtpengine):
 *   Session A  call-id = <Call-ID des Anrufers>   from-tag = caller, to-tag = 'uas'
 *              offer  = SDP des Anrufers, answer = jeweils aktuelles Gegenueber
 *              -> Anrufer-SDP beim Carrier bleibt unveraendert (kein re-INVITE nach draussen)
 *   Session B  call-id = <Call-ID>-agent          nur waehrend Briefing
 */

const Srf = require('drachtio-srf');
const { Client: RtpEngine } = require('rtpengine-client');
const express = require('express');

const cfg = {
  drachtio: { host: '127.0.0.1', port: 9022, secret: process.env.DRACHTIO_SECRET || 'cymru' },
  rtpengine: { host: '127.0.0.1', port: 22222 },
  ai: { uri: process.env.AI_URI || 'sip:ai@127.0.0.1:6060' },
  trunk: {
    domain: process.env.TRUNK_DOMAIN || 'g.vsip.eu',
    username: process.env.TRUNK_USER || '80848.01',
    password: process.env.TRUNK_PASS || '',
    callerId: process.env.TRUNK_CLI || '',
    expires: 300
  },
  moh: process.env.MOH_FILE || '/opt/voice-ai-stack/moh/wartemusik.wav',
  port: parseInt(process.env.ROUTER_PORT || '3001', 10),
  // Nur diese Quellen duerfen INVITEs schicken (Carrier + lokal)
  allowedPeers: (process.env.ALLOWED_PEERS || '127.0.0.1,193.31.27.79').split(',').map((s) => s.trim()).filter(Boolean)
};

const srf = new Srf();
const rtp = new RtpEngine();
const app = express();
app.use(express.json());

const calls = new Map(); // callId -> session

/* ---------- Helpers ---------- */

const P = (fn, ...args) => new Promise((resolve, reject) => {
  fn.call(rtp, ...args, (err, res) => (err ? reject(err) : resolve(res)));
});

const rg = {
  offer: (opts) => P(rtp.offer, cfg.rtpengine.port, cfg.rtpengine.host, opts),
  answer: (opts) => P(rtp.answer, cfg.rtpengine.port, cfg.rtpengine.host, opts),
  del: (opts) => P(rtp.delete, cfg.rtpengine.port, cfg.rtpengine.host, opts),
  play: (opts) => P(rtp.playMedia, cfg.rtpengine.port, cfg.rtpengine.host, opts),
  stop: (opts) => P(rtp.stopMedia, cfg.rtpengine.port, cfg.rtpengine.host, opts)
};

const log = (...a) => console.log(new Date().toISOString(), ...a);
const sessA = (s) => ({ 'call-id': s.callId, 'from-tag': s.callerTag, 'to-tag': s.aiToTag });
const sessB = (s) => ({ 'call-id': `${s.callId}-agent`, 'from-tag': 'router', 'to-tag': 'agent' });

/* ---------- Trunk-Registrierung ---------- */

function registerTrunk() {
  const aor = `sip:${cfg.trunk.username}@${cfg.trunk.domain}`;
  srf.request(`sip:${cfg.trunk.domain}`, {
    method: 'REGISTER',
    headers: {
      From: `<${aor}>`,
      To: `<${aor}>`,
      Contact: `<sip:${cfg.trunk.username}@${process.env.PUBLIC_IP || '0.0.0.0'}:5060>`,
      Expires: cfg.trunk.expires
    },
    auth: { username: cfg.trunk.username, password: cfg.trunk.password }
  }, (err, req) => {
    if (err) { log('REGISTER Fehler:', err.message); return setTimeout(registerTrunk, 30000); }
    req.on('response', (res) => {
      log(`REGISTER -> ${res.status} ${res.reason || ''}`);
      const next = (res.status === 200 ? cfg.trunk.expires - 30 : 30) * 1000;
      setTimeout(registerTrunk, next);
    });
  });
}

/* ---------- Eingehender Anruf ---------- */

srf.invite(async (req, res) => {
  const src = req.source_address;
  const callId = req.get('Call-ID');
  const fromTag = req.getParsedHeader('From').params.tag;

  if (!cfg.allowedPeers.includes(src)) {
    log(`BLOCKED ${src} Call-ID=${callId}`);
    return res.send(403);
  }
  log(`INVITE von ${src} Call-ID=${callId} -> ${req.get('To')}`);

  try {
    // SDP-Passthrough: kein Medienanker. Der Adapter bekommt das SDP des
    // Anrufers und antwortet mit seinem eigenen — RTP laeuft direkt zwischen
    // Carrier und Adapter (kein rtpengine, kein Einweg-Audio).
    const ai = await srf.createUAC(cfg.ai.uri, {
      localSdp: req.body,
      headers: { 'X-Call-ID': callId }
    });

    const uas = await srf.createUAS(req, res, { localSdp: ai.remote.sdp });
    log(`BRIDGE Call-ID=${callId} (SDP-Passthrough, kein rtpengine)`);

    const cleanup = async () => {
      if (!calls.has(callId)) return;
      calls.delete(callId);
      ai.destroy().catch(() => {});
      log(`CALL ENDE ${callId}`);
    };
    calls.set(callId, { callId, callerTag: fromTag, ai, uas, state: 'passthrough' });
    uas.on('destroy', cleanup);
    ai.on('destroy', cleanup);
  } catch (e) {
    log('Aufbau fehlgeschlagen:', e.message);
    await rg.del({ 'call-id': callId }).catch(() => {});
    res.send(503);
  }
});

/* ---------- Warmtransfer ---------- */

app.post('/transfer/start', async (req, res) => {
  const { callId, targetNumber } = req.body || {};
  const s = calls.get(callId);
  if (!s) return res.status(404).json({ error: 'Call nicht gefunden' });
  if (s.state !== 'ai') return res.status(409).json({ error: `Zustand ${s.state}` });

  s.state = 'parking';
  try {
    // Anrufer hoert Wartemusik, KI ist stumm zum Anrufer
    await rg.play({ ...sessA(s), file: cfg.moh, 'repeat-times': 30 });

    // Agent-Leg aufbauen
    const bOffer = await rg.offer({
      ...sessB(s),
      sdp: s.aiSdp,
      'replace': ['origin', 'session-connection'],
      codec: { strip: ['all'], offer: ['PCMU', 'PCMA'] },
      flags: ['port', 'trust-address']
    });

    const target = `sip:${targetNumber}@${cfg.trunk.domain}`;
    const from = `sip:${cfg.trunk.username}@${cfg.trunk.domain}`;
    const agent = await srf.createUAC(target, {
      localSdp: bOffer.sdp,
      headers: {
        From: `<${from}>`,
        'P-Preferred-Identity': `<${cfg.trunk.callerId ? 'sip:' + cfg.trunk.callerId + '@' + cfg.trunk.domain : from}>`
      },
      auth: { username: cfg.trunk.username, password: cfg.trunk.password }
    });

    await rg.answer({
      ...sessB(s),
      sdp: agent.remote.sdp,
      'replace': ['origin', 'session-connection'],
      flags: ['trust-address']
    });

    // KI spricht jetzt mit dem Mitarbeiter (Briefing)
    await s.ai.modify(bOffer.sdp);

    s.agent = agent;
    s.agentSdp = agent.remote.sdp;
    s.state = 'briefing';
    log(`TRANSFER gestartet ${callId} -> ${targetNumber}`);

    agent.on('destroy', () => {
      if (s.state === 'briefing') {
        log(`AGENT weg waehrend Briefing ${callId} -> zurueck zur KI`);
        returnPath(s).catch((e) => log('returnPath Fehler:', e.message));
      }
    });

    res.json({ status: 'briefing_started' });
  } catch (e) {
    log('Transfer-Start fehlgeschlagen:', e.message);
    await rg.stop(sessA(s)).catch(() => {});
    s.state = 'ai';
    res.status(500).json({ error: e.message });
  }
});

async function returnPath(s) {
  await rg.stop(sessA(s)).catch(() => {});
  if (s.agent) { await s.agent.destroy().catch(() => {}); s.agent = null; }
  await rg.del(sessB(s)).catch(() => {});
  s.agentSdp = null;
  s.state = 'ai';
  log(`RUECKWEG ${s.callId} -> KI uebernimmt wieder`);
}

app.post('/transfer/cancel', async (req, res) => {
  const { callId } = req.body || {};
  const s = calls.get(callId);
  if (!s) return res.status(404).json({ error: 'Call nicht gefunden' });
  try {
    await returnPath(s);
    res.json({ status: 'returned' });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/transfer/complete', async (req, res) => {
  const { callId } = req.body || {};
  const s = calls.get(callId);
  if (!s || s.state !== 'briefing') return res.status(409).json({ error: 'Kein aktives Briefing' });
  try {
    await rg.stop(sessA(s)).catch(() => {});
    // Gegenueber von Session A auf den Agenten umschalten - Anrufer-SDP bleibt unberuehrt
    await rg.answer({
      ...sessA(s),
      sdp: s.agentSdp,
      'replace': ['origin', 'session-connection'],
      'direction': ['pub', 'pub']
    });
    await rg.del(sessB(s)).catch(() => {});
    await s.ai.destroy().catch(() => {});
    s.ai = null;
    s.state = 'bridged';
    log(`DURCHGESTELLT ${callId}`);
    res.json({ status: 'transferred' });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/health', (req, res) => res.json({ ok: true, calls: calls.size }));

/* ---------- Start ---------- */

srf.connect(cfg.drachtio);
srf.on('connect', (hp) => {
  log('drachtio verbunden', hp);
  // Registrierung uebernimmt der registrar-Dienst (ein Kontakt, keine Konkurrenz)
  if ((process.env.TRUNK_REGISTER || '0') === '1') {
    registerTrunk();
  } else {
    log('REGISTER uebersprungen (macht wt-registrar)');
  }
});
srf.on('error', (e) => log('drachtio Fehler:', e.message));

app.listen(cfg.port, '127.0.0.1', () => log(`Router HTTP auf 127.0.0.1:${cfg.port}`));
