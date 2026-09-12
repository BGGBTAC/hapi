# Peer-Erweiterung sicher aktivieren und zurückrollen

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

## 3. Freigegebener Wartungswechsel

Für den ausdrücklich autorisierten dev-main-Benutzerdienst liegt `scripts/dev/peer-activate.py` bereit. Ohne `--execute` ermittelt es ausschließlich Plan, Prüfsumme und bestehende Prozessgenerationen. Es verweigert root, andere Hostnamen, bereits vorhandene Peer-Drop-ins und uneindeutige Runner-Prozesse.

```bash
python3 scripts/dev/peer-activate.py \
  --binary /absoluter/pfad/zum/geprüften/hapi-peer \
  --infra-root /home/benedict/work/infra-infisical \
  --sops /tmp/infisical-provision/sops \
  --url http://127.0.0.1:3006 \
  --expected-age-recipient age1x23j75r9ha6776gxzv8v984075f4znttmjhcgy8c2pc7zuffhg0s4f6e6s
```

Erst nach erfolgreicher Review und dem Pilot mit dem finalen Artefakt denselben Aufruf mit `--execute` ausführen. Der Ablauf kopiert die Binärdatei unter `~/.local/share/hapi/releases/peer-<hash>/hapi`, stoppt den Hub kurz und sichert das vollständige `~/.hapi` samt ursprünglichen Units direkt als Tar-Stream in SOPS. Es gibt weder Klartext-Zwischenarchiv noch SQL-Zugriff. SOPS-Struktur und Ciphertext-Prüfsumme werden geprüft. Der private age-Schlüssel bleibt auf Bens Mac; eine tatsächliche Entschlüsselungs-/Restoreprobe ist auf dev-main daher nicht möglich. Diese Einschränkung muss im Aktivierungsbeleg stehen.

Die originalen npm-Dateien bleiben erhalten. Drop-ins und ein Beleg ohne Zugangsdaten landen unter `services/hapi/peer-release/` im Infra-Worktree; der verschlüsselte Sicherungspunkt bleibt außerhalb des Repos. Vorhandene `~/.local/bin/hapi`-Dateien werden nicht überschrieben; ein noch freier Pfad wird nur mit zusätzlichem `--create-path-shim` mit einem Link auf die geprüfte Version belegt. Shells, deren PATH dieses Verzeichnis nicht vor `/usr/bin` enthält, verwenden den absoluten Releasepfad.

**Runner-Sitzungen erhalten:** Der aktuelle `hapi-runner.service` hat `KillMode=control-group`; ein einfacher Neustart würde deshalb auch abgetrennte Agenten beenden. Das Aktivierungsskript setzt vor jedem Stop `KillMode=process` und vorübergehend `Restart=no`, lädt systemd neu und prüft beides. Da MainPID hier der Node-NPM-Launcher mit `execFileSync` ist, erhält ausschließlich der eindeutig ermittelte eigentliche Bun-Runner ein positives Einzel-PID-SIGTERM. Danach müssen Runner und Launcher beendet sein, während die erfassten HAPI-Sitzungs-PIDs mit unveränderter Prozessgeneration weiterleben. Der endgültige Runner-Drop-in behält `KillMode=process`, startet die neue Binärdatei direkt und setzt `HAPI_CLI_EXECUTABLE` auf denselben Pfad. Ein bloßer PATH-Wechsel erreicht den laufenden alten Runner nicht.

`hapi runner stop` wird dafür bewusst nicht benutzt: Seine Fehlerbehandlung kann nach kurzem Timeout rekursiv töten. `cleanupAndShutdown` des Runner-Prozesses schließt nur eigene API-/Control-Verbindungen, State und Lock; Sitzungen wurden mit `detached: true` gestartet. Bestehende Sitzungsprozesse bekommen dadurch keinen neuen MCP-Code. Neue Sitzungen aus dem aktualisierten Runner verwenden hingegen die neue Peer-Erweiterung.

Vor Produktivbetrieb müssen Review und konkrete Betriebsfreigabe abgeschlossen sein. Bestehende HAPI-Datenbanken werden von Entwicklungsagenten weiterhin ausschließlich über die Hub-API angesprochen; dieses Verfahren enthält kein Skript, das eine vorhandene Datenbank kopiert oder migriert.

1. Wartungsfenster festlegen; neue Starts und automatische Aufgaben pausieren, aktive Arbeiten geordnet auslaufen lassen. Nutzer über die kurze Hub-Unterbrechung informieren. Zuerst den Testhub mit dem finalen Artefakthash prüfen.
2. Die aktuell verwendeten Daten- und Konfigurationspfade durch den Betriebsverantwortlichen bestätigen lassen. Keine `settings.json`, Prozessumgebungen oder Environment-Dateien mit echten Geheimnissen in Agentenausgaben lesen.
3. Den bestehenden Hub im freigegebenen Fenster stoppen. Mit dem etablierten verschlüsselten Infrastruktur-Backupverfahren einen konsistenten Sicherungspunkt des vollständigen HAPI-Datenverzeichnisses erstellen: Datenbank samt gegebenenfalls vorhandenen WAL/SHM-Dateien, Konfiguration und serverseitige Schlüssel. Wiederherstellbarkeit und zugehörigen alten Binärstand protokollieren. Währenddessen weiterlaufende Agenten können ihre Protokolle fortschreiben; der abgeschaltete Hub hält die Datenbank konsistent. Die bestehende VM-Sicherung allein ist kein Ersatz für einen frischen, geprüften Wechselpunkt.
4. Die geprüfte neue Binärdatei unter einem separaten Releasepfad installieren. Die bestehende Unit-Konfiguration und ihre Umgebungsquellen erhalten; nur `ExecStart` über einen in `~/infra` versionierten Drop-in auf den neuen absoluten Binärpfad umstellen. Vor Änderung muss ein rücksetzbarer Stand des bisherigen Drop-ins vorhanden sein. Nicht `/usr/bin/hapi` blind überschreiben.
5. Den neuen Hub starten. Er führt Schema 25→26 einmalig aus. `/health` und die bisherigen Sitzungslisten über die API prüfen; danach eine neue Testsitzung für `ping_peer`/Antwort/Anzeige verwenden. Erst nach erfolgreicher Prüfung neue Arbeit zulassen.
6. Neue Agentensitzungen über die neue CLI-Version starten. Der alte laufende Runner kann sonst weiterhin alte CLIs erzeugen; dessen Umstellung gehört in denselben freigegebenen Wartungsplan. Ein bereits laufender Prozess lädt durch Austausch der Binärdatei keinen neuen MCP-Code. Bestehende Sitzungen können ihre Arbeit weiterführen und später geordnet über den freigegebenen Resume-Weg auf die neue Version wechseln.

Schema 26 markiert nur neue intern authentifizierte Nachrichten. Altdaten erhalten automatisch `peer_authenticated=0`; eingeschleuste JSON-Herkunft wird beim Lesen entfernt. Die Migration löscht keine Nachrichten.

## 4. Rückkehr zum vorherigen Stand

**Die alte Hub-Version darf nicht gegen eine bereits auf Schema 26 migrierte Datenbank gestartet werden.** Ein Zurücksetzen nur des Binärpfads reicht nicht.

Bei einer gescheiterten Abnahme keine neuen Aufgaben zulassen. Den neuen Hub stoppen, die vor dem Wechsel gesicherte komplette Datengeneration mit dem etablierten verschlüsselten Restore-Verfahren wiederherstellen und den alten Binärpfad/Unit-Stand aktivieren. Erst anschließend den alten Hub starten und seine Gesundheit/Sitzungsübersicht prüfen. Die fehlgeschlagene neue Datengeneration für eine spätere Untersuchung geschützt aufbewahren; niemals ungeprüft einzelne Tabellen zwischen den Ständen mischen.

Nach einer erfolgreichen Freigabe entstehende neue Nachrichten sind im alten Sicherungspunkt nicht enthalten. Ein später Rollback benötigt deshalb einen gesonderten Plan für diese Daten oder einen Vorwärtsfix. Das ist der Grund für einen Testhub und eine Abnahme vor Wiederaufnahme produktiver Arbeit.
