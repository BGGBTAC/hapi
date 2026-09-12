# Peer-Erweiterung: Review und Prüfnachweise

Stand: 12.09.2026, Basis HAPI v0.29.0. Claude Opus prüfte Architektur und Implementierung lesend; die automatisierten Tests wurden separat ausgeführt. Die abschließende Code-Nachprüfung schloss alle zehn Ursprungsbefunde und fand keinen Blocker.

| Opus-Befund | Erledigung und Beleg |
| --- | --- |
| Rekursives Entfernen legitimer `meta.peer`-Werkzeugdaten | `hub/src/store/peerMetadata.ts` bereinigt ausschließlich die vier bekannten Nachrichtenhüllen; Regression erhält verschachtelte Anwendungsdaten. |
| Voice-WebSockets akzeptieren Peer-Capabilities | `verifyOwnerJwt` verlangt `uid`/`ns` und weist Peer-Audience/Scope ab. HTTP sowie beide Bun-WebSocket-Upgrades verwenden dieselbe Prüfung; der reale Hub-Test prüft HTTP 401. |
| STDIO entfernt `localId`/`replyTo` | Beide Felder sind im STDIO-Zod-Schema; Test prüft Schema-Parsing und unveränderte Weitergabe. |
| Sitzungsende markiert wartende Peer-Nachrichten als verarbeitet | Der produktive Session-End-Sweep lässt authentifizierte Peer-Zeilen unberührt; Regression prüft anschließendes Replay. `/clear` verschiebt wartende Zeilen über `moveUninvokedMessages` samt Vertrauensflag in die Ersatzsitzung. Der zusätzlich gehärtete `markUninvokedImmediateMessages`-Helfer hat derzeit keinen produktiven Aufrufer. |
| Tiefe Kopie bei jedem Lesezugriff | Bereinigung liefert ohne reservierte Herkunft dieselbe Objektreferenz zurück; keine Rekursion über Werkzeugdaten. |
| Antwortbezug nicht auflösbar | Hub setzt `replyToSessionId`; CLI-Frame und `inspect_peer` zeigen Hub-Message-IDs. Eigentumsprüfung bindet beide Sitzungen. |
| Token ohne Zielbindung/ID/Limit | Empfänger im Capability-Mint verpflichtend, `jti`, maximal 15 Minuten, festes Fenster mit 60 Requests/Minute je Namespace/Sender und Hub-Prozess. |
| CLI behauptet „delivered“ | CLI und MCP bestätigen nur Persistenz; keine Verarbeitungs- oder Aufgabenabschlussbehauptung. |
| Leerer Mint-Body entgegen optionalem TTL | TTL bleibt optional; Empfänger ist nun ausdrücklich Pflicht. Beide Fälle getestet und im API-Vertrag beschrieben. |
| Externe Local-ID reserviert Peer-Namensraum | Externe `peer:`-IDs werden umbenannt. Der abschließend gefundene Präfix-Konflikt ist durch injektive Behandlung von `^(external:)*peer:` behoben; ein Store-Aufruf normalisiert genau einmal. |

Die abschließende Opus-Prüfung fand außerdem eine Vorwärtskompatibilitätslücke: Das CLI-Leseschema entfernte die gesamte Nachricht, sobald ein künftiger Hub zusätzliche Peer-Metadaten sendet. Der Lesepfad akzeptiert zusätzliche Felder und entfernt unbekannte Attribute; der Hub-Schreibvertrag bleibt strikt. Ein Regressionstest prüft die Zustellung samt Befehlsisolierung.

Verbleibende Grenzen sind bewusst dokumentiert: `senderName` und `senderFlavor` sind beschreibende, selbstgemeldete Sitzungsdaten; authentifiziert ist allein die Sitzungs-ID. Das bestehende `meta.sentFrom: "webapp"` bleibt ein Legacy-Transportmerkmal und darf nie als menschliche Herkunft ausgelegt werden. Das Ratenlimit gilt pro Hub-Prozess, setzt beim Neustart zurück und erlaubt an einer festen Fenstergrenze kurzfristige Bursts. Persistenz oder Konsum-ACK sind kein Beleg für Modellverarbeitung oder Aufgabenabschluss.

## Prüfungen

Bun 1.3.14 entspricht der vorhandenen CI. `bun install --frozen-lockfile` und `bun run typecheck` erfolgreich. Die vollständige Suite nach den letzten Quellcodekorrekturen: CLI 2425 bestanden, 1 übersprungen; Hub 1205 bestanden, 3 übersprungen; Web 2796 bestanden; Shared 283 bestanden; Relay 80 bestanden. Protokolle auf dev-main: `/tmp/hapi-peer-full-tests-final.log`, `/tmp/hapi-peer-typecheck-final.log`.

Das finale Linux-x64-Artefakt enthält Hub, CLI und Weboberfläche. Der isolierte Testhub verwendet eine neue Datenbank und synthetische Anmeldedaten. Mit ausdrücklich gesetztem `HAPI_PEER_BROWSER=1` prüft `scripts/dev/peer-live-check.ts` über echte Hub-API und zwei echte MCP-Server Versand, identische Wiederholung, Antwortbindung, `/clear` als Daten und den sichtbaren Herkunftslink samt Browsernavigation. Es handelt sich um einen Transporttest mit Claude-/Codex-Sitzungsmetadaten; dabei werden keine Modelle aufgerufen. Der Test mit dem finalen Binärartefakt war erfolgreich; der getrennte Beleg liegt unter `/tmp/hapi-peer-built-browser-final.log` und enthält `browserVerified: true`.

## Lokale Skill-Regel

`AGENTS.md`, Abschnitt „Pre-push self-review (agents)“, verlangt: „Before commit/push/PR: use the **`pre-push-review`** skill (`~/.cursor/skills/pre-push-review/`).“ Dieser Pfad fehlt. Die gezielte Suche unter `~/.claude/skills`, `~/.codex/skills` und dem Codex-Plugin-Cache fand ebenfalls keine entsprechende `SKILL.md`. Deshalb wurden die dort ausdrücklich genannten mechanischen Prüfungen durchgeführt, `.github/prompts/codex-pr-review.md` als lokale Major-Checkliste angewendet und die unabhängigen Opus-Reviews dokumentiert. Der nicht verfügbare Skill selbst konnte nicht ausgeführt werden.

## Bestehende Runner-Sitzungen übernehmen

Der Betriebsreview stellte fest, dass ein unveränderter alter Runner normale Web-Sitzungen beim Neustart nicht vollständig wieder in seine Kinderliste übernimmt. Der Release verwendet deshalb die bestehende Resume-Persistenz mit geprüftem PID-Startmarker. Ein kleiner Recovery-Fix hält bestätigte Wiederaufnahmen sichtbar und behandelt neue Webhooks derselben verifizierten Prozessgeneration als Übernahme. Nicht lesbare oder abweichende Marker bleiben in Quarantäne und können nie in den Orphan-Kill-Pfad fallen. Nach einer neuen bestätigten Sitzungs-ID wird auch die zwischengespeicherte Resume-Antwort aktualisiert.

`cli/src/runner/peerRecovery.test.ts` startet einen echten isolierten Runner und synthetische Warteprozesse: sichtbar nach Wiederaufnahme, unverändert lebend bei fehlendem/falschem Startmarker, neue Sitzungs-ID nach bestätigtem Webhook, aktualisierte Antwort über die reale Hub-Resume/Machine-RPC-Kette und anschließendes gezieltes Stoppen nur des verifizierten Testprozesses. Alte Prozesse werden nicht in den ungeprüften direkten PID-Stop-Pfad aufgenommen.

## Betriebsautomation

Der ergänzende Opus-Betriebsreview verlangte drei Korrekturen: zuverlässige Runner-Wiederherstellung im Fehlerpfad, eine begrenzte Tar/SOPS-Pipeline ohne blockierende Diagnose-Pipe und den exakten Abgleich mit dem erwarteten öffentlichen age-Empfänger. Das Aktivierungsskript enthält diese Korrekturen sowie Unit-/FD-/Portprüfung, Freiplatz- und Archiv-Mitgliedsbelege, natürliche Prozessenden als eigenen Zustand, stabile Runner-PID und ausdrücklich optionale PATH-Umschaltung. `scripts/dev/peer-activate.test.py` prüft 16 synthetische Fehler-/Erfolgspfade; zusätzlich wurde die echte SOPS-Binärdatei mit der Infra-Regel ausschließlich auf synthetische Tar-Daten angewandt.

Die finale Opus-Delta-Prüfung schloss alle drei Betriebsblocker sowie die relevanten Major-/Medium- und Recovery-Befunde. Zwei verbleibende kleine Hinweise wurden direkt korrigiert: ein natürliches Prozessende zwischen Markerprüfungen verhindert nicht mehr die Übernahme der übrigen Sitzungen (zusätzlicher Regressionstest), und Tar toleriert während des Archivierens entfernte temporäre Dateien. Der Stand hatte danach keinen offenen Aktivierungsblocker.
