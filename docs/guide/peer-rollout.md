# Peer-Erweiterung: Vorschau und gesperrter Live-Rollout

Die Erweiterung benötigt den neuen Hub. Der neue CLI-Aufruf stellt zusätzlich die MCP-Werkzeuge, Capability-Erneuerung und bequemes Antworten bereit. Ein reiner Companion-Plugin-Eintrag kann den fehlenden Hub-Endpunkt nicht ersetzen. Bereits laufende alte CLIs erhalten vom neuen Hub sicher eingerahmte Agentennachrichten; ihre eigenen bisherigen `ping_peer`-Aufrufe sind weiterhin menschlich authentifizierte Legacy-Aufrufe ohne gesicherte Agentenherkunft.

Auf dev-main wurde am 12.09.2026 nur lesend geprüft: `hapi-hub.service` läuft als Benutzerdienst, die Unit liegt unter `~/.config/systemd/user/hapi-hub.service`, `ExecStart` ist `/usr/bin/hapi hub`. Der Node-Launcher `/usr/bin/hapi` verweist auf `/usr/lib/node_modules/@twsxtd/hapi/bin/hapi.cjs`; sein Hub-Kindprozess verwendet `/usr/lib/node_modules/@twsxtd/hapi/node_modules/@twsxtd/hapi-linux-x64/bin/hapi`. Geöffnete Dateipfade weisen auf `~/.hapi/hapi.db` samt WAL/SHM; Datenbankinhalt und Geheimnisse wurden dabei nicht gelesen. `umgebung dienste` meldete den HAPI-Dienst erreichbar. Die folgende Vorbereitung ersetzt weder diesen Dienst noch dessen Datenbank. Vor einer tatsächlichen Umstellung den aktuellen Zustand erneut prüfen.

## 1. Release bauen und identifizieren

Bun **1.3.14**, wie in der vorhandenen CI, verwenden.

```bash
bun install --frozen-lockfile
bun typecheck
bun run test
bun run build:web
bun run hub/scripts/generate-embedded-web-assets.ts
bun run cli/scripts/build-executable.ts --with-web-assets --target bun-linux-x64-baseline --name hapi-peer
sha256sum cli/dist-exe/bun-linux-x64-baseline/hapi-peer
```

Die ausführbare Datei enthält **Hub, CLI und Weboberfläche**. Die eingebetteten Hilfsprogramme müssen zuvor unter `cli/tools/archives/` und `shared/tools/tunwg/` vorhanden sein. Der erste Build benötigte zusätzlich `tunwg` aus Release `v26.08.03+122a6d0`; dessen GitHub-SHA256 war `c1a7e08d956d9ee1087b8bf3b651d5dc32aee80eee1052c11218189fd5635768`. Für reproduzierbare Releases den konkreten Release und Hash festhalten, nicht unbemerkt `latest` wechseln.

Der v0.29.0-Ausgangsstand enthielt im Lockfile keinen Datensatz für das bereits referenzierte optionale Paket `@twsxtd/hapi-win32-x64@0.29.0`; dadurch scheiterte `--frozen-lockfile` auch mit der CI-Version. Der Patch ergänzt ausschließlich diesen generierten Datensatz mit Integritätshash, ohne Paketversionen zu ändern. Anschließend lief `bun install --frozen-lockfile` erfolgreich. Der finale Build verwendet Bun 1.3.14.

Artefakte unter einem Namen mit Prüfsumme ablegen, beispielsweise `hapi-peer-<sha256-prefix>`. Den alten ausführbaren Stand unverändert aufbewahren. `--version` bleibt upstream-kompatibel bei 0.29.0; für diese Erweiterung sind die Artefakt-Prüfsumme und `/health` mit `capabilities.peerMessages: true` maßgeblich.

## 2. Eigenen Testhub starten

```bash
python3 scripts/dev/peer-preview.py \
  --binary cli/dist-exe/bun-linux-x64-baseline/hapi-peer \
  --port 3316
```

Das Skript erstellt ein neues Verzeichnis mit Modus 0700, eine neue Datenbank und ausschließlich synthetische Zugangsdaten. Es übernimmt keine Produktivkonfiguration und hört nur auf `127.0.0.1`. Ein explizites `--directory` muss vorher fehlen. Ein vorhandenes Datenverzeichnis wird abgewiesen. Mit `--duration 3` lässt sich der Build kurz automatisch prüfen; sonst beendet Strg-C nur den vom Skript gestarteten Testhub.

Die Ausgabe nennt den lokalen Link und einen absoluten Wrapperpfad. Über diesen Wrapper können neue Testsitzungen gestartet werden:

```bash
/tmp/hapi-peer-preview-.../hapi-peer-preview claude
/tmp/hapi-peer-preview-.../hapi-peer-preview codex
```

Der Wrapper legt `HAPI_HOME`, `HAPI_API_URL` und das synthetische HAPI-Testtoken fest; `/usr/bin/hapi`, der bestehende Runner und laufende Sitzungen bleiben unberührt. Die Modellprogramme selbst müssen wie bisher installiert sein. Modellaufrufe können Kontingent verbrauchen; die automatisierten Funktionstests benötigen keine echten Modelle.

Der bekannte Vorschau-Login lautet `hapi-peer-preview-synthetic-only`; hier ausschließlich Testdaten verwenden. Für einen Browser auf einem anderen Rechner reicht eine ausdrücklich eingerichtete SSH-Portweiterleitung zum Loopback-Port. Es sind weder DNS- noch Cloudflare-Änderungen erforderlich.

Die automatisierte Prüfung startet ihrerseits einen isolierten Hub, zwei Sitzungsklienten mit Claude-/Codex-Metadaten und zwei echte MCP-Server:

```bash
cd cli
HAPI_PEER_BROWSER=1 bun run test src/api/peerMessaging.test.ts
```

Playwright benötigt einen installierten Chromium. Optional kann `HAPI_PEER_BROWSER_EXECUTABLE` auf eine vorhandene ausführbare Datei zeigen. Geprüft werden Persistenz, Wiederholung ohne Doppelzustellung, gebundene Antwort, abgewiesene Voice-WebSocket-Zugriffe mit Peer-Capability, die Behandlung von `/clear` als Daten sowie der sichtbare Herkunftslink und dessen Navigation.

## 3. Live-Rollout gesperrt

Der Aktivierungsversuch vom 12.09.2026 verursachte einen mehrstündigen HAPI-Ausfall. Die frühere Anleitung und die Zusage, laufende Sitzungen zu erhalten, waren fehlerhaft. Ursache, Zeitverlauf, fehlender Sicherungspunkt und Sofortkorrektur stehen im [Incidentbericht](peer-incident-2026-09-12.md).

`scripts/dev/peer-activate.py` erlaubt ausschließlich die lesende Vorbereitung. `--execute` wird vor jedem Eingriff abgewiesen. Es gibt keinen freigegebenen Live-Aktivierungsweg in diesem Skript. Insbesondere darf der Hub nicht aus einem abhängigen Runner heraus gestoppt werden: `Requires` und `KillMode=control-group` können dabei auch den Deployer und dessen Recovery beenden.

Die Bau- und Vorschauprüfungen oben bleiben verwendbar. Ein künftiger Wartungsablauf muss Dienstabhängigkeiten und Prozessgruppen vollständig berücksichtigen und einen unabhängig beaufsichtigten Controller sowie eine externe Wiederherstellung besitzen. Die positiven Prüfungen einzelner Hilfsfunktionen ersetzen diesen Nachweis nicht.

## 4. Rückkehr zum vorherigen Stand

**Die alte Hub-Version darf nicht gegen eine bereits auf Schema 26 migrierte Datenbank gestartet werden.** Ein Zurücksetzen nur des Binärpfads reicht nicht.

Bei einer gescheiterten Abnahme keine neuen Aufgaben zulassen. Den neuen Hub stoppen, die vor dem Wechsel gesicherte komplette Datengeneration mit dem etablierten verschlüsselten Restore-Verfahren wiederherstellen und den alten Binärpfad/Unit-Stand aktivieren. Erst anschließend den alten Hub starten und seine Gesundheit/Sitzungsübersicht prüfen. Die fehlgeschlagene neue Datengeneration für eine spätere Untersuchung geschützt aufbewahren; niemals ungeprüft einzelne Tabellen zwischen den Ständen mischen.

Nach einer erfolgreichen Freigabe entstehende neue Nachrichten sind im alten Sicherungspunkt nicht enthalten. Ein später Rollback benötigt deshalb einen gesonderten Plan für diese Daten oder einen Vorwärtsfix. Das ist der Grund für einen Testhub und eine Abnahme vor Wiederaufnahme produktiver Arbeit.
