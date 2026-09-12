# HAPI-Ausfall beim Peer-Rollout am 12.09.2026

Der vom Codex-Hauptagenten verantwortete Rollout verursachte einen Ausfall von **04:20:01 bis 12:43:52 UTC**: 8 Stunden, 23 Minuten, 50 Sekunden. Die Wiederherstellung erfolgte laut Ben durch einen separat laufenden Opus. Der ursprüngliche Rollout hatte sich selbst beendet und stellte den Dienst nicht wieder her. Die spätere Aussage des Hauptagenten, es handle sich gerade um eine kurze geplante Unterbrechung, war falsch und wurde korrigiert.

## Belegte Ereignisse

Quelle: Benutzerjournal von `hapi-hub.service` und `hapi-runner.service`, am 12.09.2026 lesend ausgewertet. Es wurden ausschließlich systemd-Ereignisse und Unit-Metadaten verwendet, keine Datenbankinhalte oder Geheimnisse.

| UTC | Ereignis |
| --- | --- |
| 04:20:00.764 | `/tmp/hapi-peer-activation.log` enthält `phase: prepared`, `execute: true`; dies ist der einzige protokollierte Schritt. |
| 04:20:00.947 | systemd beginnt, den Runner zu stoppen. |
| 04:20:01.736 | Runner gestoppt. |
| 04:20:01.737 | systemd beginnt, den Hub zu stoppen. |
| 04:20:01.738 | Hub gestoppt. |
| 12:43:52.224 | Hub wieder gestartet. |
| 12:43:57.236 | Runner wieder gestartet. |

Die originalen Units haben unveränderte Änderungszeiten vom 06.09.2026. Der Runner hat `Requires=hapi-hub.service`, `After=hapi-hub.service`, `KillMode=control-group`, `Restart=always` und keinen `OnFailure`-Dienst. Der Agent läuft innerhalb der Cgroup `.../app.slice/hapi-runner.service`.

## Ursache

In Commit `8b1884c2`, `scripts/dev/peer-activate.py:563`, stoppt das Skript den Hub. Die Runner-Umstellung auf `KillMode=process` steht erst in Zeile 582, nach Sicherung und Hub-Neustart. Die Vorprüfung berücksichtigt weder die Stop-Abhängigkeit noch die eigene Cgroup.

`Requires` propagiert den expliziten Hub-Stop auf den Runner. `KillMode=control-group` beendet dessen Prozesse einschließlich der Agenten und des von ihnen gestarteten Deployers. Damit wird auch der Pythonprozess beendet, bevor er seinen Recovery-Pfad erreicht. `except BaseException` bietet keinen Schutz gegen diesen Prozessverlust. `Restart=always` hebt einen expliziten systemd-Stop nicht auf. Die systemd-Semantik ist in der [Unit-Dokumentation](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml) und [Service-Dokumentation](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml) beschrieben.

Die Sicherung sollte erst nach dem Hub-Stop entstehen. Das vorgesehene Verzeichnis enthält kein Backup; `encrypted-backup`, `hub-active`, `activated` und `failed` fehlen im Protokoll. **Der geplante Sicherungspunkt dieses Rollouts existiert nicht.** Das betrifft nicht die separat geprüften Zürcher Infisical-/Companion-Sicherungen.

Die beendeten Agenten konnten weder weiterarbeiten noch die Wiederherstellung auslösen. Eine Aussage über den Verlust ungespeicherter Arbeitsstände ist aus diesen Metadaten allein nicht möglich. Es wurden keine bestehenden HAPI-Datenbanken direkt untersucht.

## Warum Prüfungen und Review versagten

- Die 16 bisherigen Aktivierungstests prüfen Hilfsfunktionen, aber nicht `main()` mit einer vollständigen systemd-Stop-Transaktion.
- Der Recoverytest setzt einen weiterlebenden Pythonprozess voraus. Der tatsächliche Fehler beendet gerade diesen Prozess.
- Die Anleitung behauptete fälschlich, der Runner werde vor jedem Stop geschützt. Diese Aussage widersprach der Reihenfolge im Code.
- Die Prüfung der Sitzungsübernahme akzeptierte verschwundene Prozesse als `ended`; auch das genügte nicht für die zugesagte Erhaltung aller laufenden Sitzungen.
- Die positiven Opus-Betriebsreviews und die Entscheidung des Hauptagenten zur Aktivierung erkannten diese Lücken nicht. Die frühere Bewertung „kein offener Aktivierungsblocker“ ist durch den Vorfall widerlegt.

## Sofortige Korrektur

`peer-activate.py --execute` ist vollständig gesperrt. Der Aufruf endet vor Pfadauflösung, Dateischreiben, Signalen oder systemctl-Aufrufen. Ein CLI-Regressionstest ruft `main()` auf und prüft diesen Abbruch samt ausbleibender Seiteneffekte. Die übrigen 16 synthetischen Tests bleiben erhalten. Die Live-Anleitung ist zurückgezogen; Dry-run und isolierte Vorschau bleiben verfügbar. Der HAPI-PR ist wegen des ungelösten Live-Rollouts als Entwurf gekennzeichnet.

Die Incident-Aufarbeitung ändert keine aktiven Units, Abhängigkeiten, Binärpfade oder Datenbanken und startet keine Dienste neu. Beim Kontrollabruf liefen Hub und Runner; der Hub meldete `peerMessages: true`. Die laufende Binärdatei hat SHA256 `7eeb173ace813a63bfcd085604daa0c6d1cd13c9456104e6843920dd8753bac7`; sie stimmt nicht mit dem ursprünglich vorgesehenen Artefakt `1dabdaa2fbd391820b42cf6c512ca07033ecb8499acce9f11beffea1e5061d7b` überein. Daraus wird keine vollständige Abnahme des aktuellen Releases abgeleitet.

## Voraussetzung für einen künftigen Rollout

Ein neu entwickelter Ablauf benötigt einen unabhängig beaufsichtigten Controller und eine Wiederherstellung außerhalb der betroffenen Dienste und Cgroups. Er muss direkte und transitive Stop-Abhängigkeiten sowie unbekannte Topologien vor jeder Mutation prüfen, Prozessverluste als Fehler behandeln und den vollständigen Ablauf mit isolierten systemd-Testunits belegen. Ein neuer Live-Versuch ist durch die derzeitige Implementierung nicht möglich.
