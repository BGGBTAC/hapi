# Konfigurationswechsel ohne HAPI-Neustart

Der wiederhergestellte Hub und Runner enthalten bereits den geprüften Peer-Code. Der [Artefaktvergleich](peer-live-provenance.md) belegt die identischen Inhalte aller 119 eingebetteten Dateien und der nativen Laufzeit. Ein weiterer Binärwechsel oder Runner-Neustart ist für diese Abnahme nicht erforderlich.

`scripts/dev/peer-service-config.py` ersetzt ausschließlich die explizite Runner-Zeile `Requires=hapi-hub.service` durch `Wants=hapi-hub.service`. Der Rest der vollständigen Unit bleibt byteidentisch. `After`, `ExecStart`, `Restart` und der bisherige effektive Standard `KillMode=control-group` werden nicht verändert. Der Hub wird beim Runner-Start weiterhin angefordert; ein expliziter Hub-Stop propagiert danach nicht mehr auf den Runner.

Dies macht keinen späteren Runner-Neustart sitzungserhaltend. Der aktuelle Runner und seine Kinder bleiben bei dieser Konfigurationsänderung vollständig laufen. Das alte, fehlerhafte `peer-activate.py --execute` bleibt gesperrt.

## Ausgeführt am 12.09.2026

Der Konfigurationswechsel wurde um **15:37:03 UTC erfolgreich abgeschlossen, ohne Downtime und mit null Dienstneustarts**. Der unabhängige Dienst `hapi-peer-config-9e7b6156bb854481.service` schrieb `status: committed`; seine Invocation-ID ist `9827c64d92ef4af5ad8f4eb5d2abf835`.

| Nachweis | Ergebnis |
| --- | --- |
| Einzige Unit-Änderung | `Requires=hapi-hub.service` → `Wants=hapi-hub.service` |
| Unit-SHA256 vorher | `aa1e20c98c0c6273651c0b49a1b61ead0fbf9c8e252f86b90a02adb75b6fc1eb` |
| Unit-SHA256 nachher | `96d213a49a57d89d70c7cfd1108b518f3bb9291acd74046b6bdc7b6cec7e4d48` |
| Hub | Launcher PID 658982 und Hub PID 658990 mit identischer Prozessgeneration erhalten; Startzeit weiterhin 12:43:52 UTC |
| Runner | Launcher PID 659039 und Runner PID 659047 mit identischer Prozessgeneration erhalten; Startzeit weiterhin 12:43:57 UTC |
| Weitere erfasste HAPI-Prozesse | PIDs 665749, 666334 und 685485 mit identischer Prozessgeneration erhalten |
| Hub-Gesundheit | `status: ok`, `peerMessages: true`, `workGraph: true` |

Die privaten lokalen Belege liegen unter `~/.local/state/hapi-peer-config/9e7b6156bb854481/`: `committed.json` enthält `configOnly: true`, `serviceRestarts: 0` und alle sieben erhaltenen Prozessgenerationen; `result.json` bestätigt den Abschluss des beaufsichtigten Vorgangs. Die ausgeführte Applier-Kopie hat SHA256 `6a9a0a71c3d9bbc52dd54f6d82214d5fff61fbd1ae005ce8cae1e03816ed2819`.

Die sieben erhaltenen Generationen beziehen sich auf den Konfigurationswechsel. Bei der späteren Kontrolle nach der Live-Abnahme waren sechs einschließlich Hub und Runner weiterhin unverändert; der zusätzliche HAPI-Prozess 685485 war inzwischen beendet. Beide Zeitpunkte stehen getrennt im JSON-Beleg.

Es wurde keine Binärdatei ersetzt und kein Datenbankbackup angelegt.

## Funktionale Live-Abnahme

Die zusätzliche Live-Abnahme war am **12.09.2026 um 15:43:09 UTC erfolgreich**. Zwei neu angelegte Testsitzungen und zwei echte MCP-Server prüften gegen den laufenden Hub authentifizierte Herkunft, `/clear` als eingerahmte Nachrichtendaten, genau eine Zustellung bei Wiederholung und eine an den Absender gebundene Antwort. Playwright bestätigte den sichtbaren Herkunftslink und die Navigation zur Sendersitzung. Die Prüfung verwendete ausschließlich synthetische Testnachrichten; sie rief keine Modelle auf.

Auch die Übergabe vom Zürcher Companion erreichte den tatsächlichen Zielklienten. Handoff `f496c417-e078-4295-a3ea-ed0a16513e50` wurde im ersten Versuch zugestellt; eine Wiederholung blieb idempotent. Der Empfangscallback bestätigte die passende, durch den Hub eingerahmte Local-ID und die authentifizierte Quellsitzung `bbde02cf-dd1b-4552-98d2-4734c47a2705`; Ziel war `5901ceb1-7024-4551-8b97-0ee101e7c65a`. Maschinenlesbare Belege stehen in [peer-config-validation-2026-09-12.json](peer-config-validation-2026-09-12.json).

## Ablauf

```sh
python3 scripts/dev/peer-service-config.py inspect
python3 scripts/dev/peer-service-config.py launch
```

`inspect` liest nur Unit-Metadaten, den bekannten Unitpfad, Prozessgenerationen und `/health`. Die Ausgabe enthält keine Konfigurationsinhalte oder Zugänge. Unbekannte Drop-ins, zusätzliche Stop-Abhängigkeiten und andere Ausgangsstände werden abgewiesen.

`launch` legt ein privates Verzeichnis unter `~/.local/state/hapi-peer-config/<id>` an: originale und vorbereitete Unit, Prüfsummen, Manifest und eine separate Kopie des geprüften Appliers samt SHA256. Es startet einen eigenen transienten systemd-Benutzerdienst. Dieser verifiziert seine unabhängige Cgroup, fehlende Hub-/Runner-Abhängigkeiten, Laufzeitgrenze und Wiederherstellung, bevor er die eine Datei atomar ersetzt und `daemon-reload` ausführt.

Der Helfer erlaubt ausschließlich `systemctl show` und `systemctl daemon-reload`. Er enthält keinen Stop-, Start- oder Restart-Befehl für HAPI. Danach müssen die neue effektive Topologie, beide bisherigen MainPIDs, sämtliche zuvor erfassten HAPI-Prozessgenerationen und die Hub-Gesundheit stimmen. Erst dann schreibt er `committed.json`.

`ExecStopPost` prüft den Abschluss oder stellt bei fehlendem Commit die ursprüngliche Unit wieder her und lädt systemd neu. Auch nach SIGKILL des Hauptprozesses läuft dieser systemd-beaufsichtigte Fehlerpfad. Fremde zwischenzeitliche Dateiedits werden nicht überschrieben. Das Ende steht in `result.json` (`committed` oder `restored`). Bei fehlendem Abschlussmarker die Dateiprüfsummen und effektive Konfiguration prüfen; ein abgebrochener Reload kann Datei und geladene Konfiguration zeitweise unterschiedlich lassen. Keine neue Ausführung parallel zu einem laufenden Vorgang starten.

Der Eingriff umfasst keine Datenbank- oder Geheimnisänderung. Die Original-Unit ist der konkrete Rückfallbestand für diesen Konfigurationswechsel; daraus wird kein Datenbankbackup abgeleitet.

## Nachweise vor dem Live-Eingriff

- Sechs synthetische Tests prüfen die genaue Transformation, verbotene Dienstbefehle, Supervision vor Dateischreiben, fremde Änderungen, Wiederherstellung nach Reloadfehler und den bestätigten Abschluss.
- Drei reale Tests mit UUID-eigenen systemd-Diensten reproduzieren die ursprüngliche Requires-Kaskade. Nach dem Wechsel zu Wants überleben Test-Runner und Kind Hub-Stop, -Start und -Restart mit identischen Prozessgenerationen; die Ausgabe des Kindes kommt weiterhin über dieselbe Pipe an.
- Zwei weitere reale Tests beenden ausschließlich den separaten Test-Worker mit SIGKILL. Echtes `ExecStopPost` und der produktive Recovery-Code stellen Originalbytes und Requires wieder her, während Test-Hub, Runner und Kind weiterlaufen. Der Erfolgsfall behält die bestätigte Wants-Konfiguration.
- Claude Opus5 prüfte genau diese Konfigurationsänderung und gab sie frei. Die systemd-Testläufe und der Live-Eingriff erfolgen nacheinander, da sie denselben Benutzer-Service-Manager neu laden.

Für die Tests ist zusätzlich der CI-Job `service-topology` auf einem wegwerfbaren Ubuntu-Testrechner eingerichtet. Ein erfolgreicher CI-Lauf ist bislang nicht belegt; die oben genannten Prüfergebnisse stammen aus den lokalen Läufen. Die für diesen CI-Test gestartete Benutzer-Serviceverwaltung ist ausschließlich Teil des CI-Rechners.
