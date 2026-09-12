# Konfigurationswechsel ohne HAPI-Neustart

Der wiederhergestellte Hub und Runner enthalten bereits den geprüften Peer-Code. Der [Artefaktvergleich](peer-live-provenance.md) belegt die identischen Inhalte aller 119 eingebetteten Dateien und der nativen Laufzeit. Ein weiterer Binärwechsel oder Runner-Neustart ist für diese Abnahme nicht erforderlich.

`scripts/dev/peer-service-config.py` ersetzt ausschließlich die explizite Runner-Zeile `Requires=hapi-hub.service` durch `Wants=hapi-hub.service`. Der Rest der vollständigen Unit bleibt byteidentisch. `After`, `ExecStart`, `Restart` und der bisherige effektive Standard `KillMode=control-group` werden nicht verändert. Der Hub wird beim Runner-Start weiterhin angefordert; ein expliziter Hub-Stop propagiert danach nicht mehr auf den Runner.

Dies macht keinen späteren Runner-Neustart sitzungserhaltend. Der aktuelle Runner und seine Kinder bleiben bei dieser Konfigurationsänderung vollständig laufen. Das alte, fehlerhafte `peer-activate.py --execute` bleibt gesperrt.

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

Die Tests werden zusätzlich im eigenen CI-Job `service-topology` auf einem wegwerfbaren Ubuntu-Testrechner ausgeführt. Die für diesen Test gestartete Benutzer-Serviceverwaltung ist ausschließlich Teil des CI-Rechners.
