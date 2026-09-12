# Provenienz des wiederhergestellten Live-Standes

Am 12.09.2026 wurde **lesend** nachgewiesen: Der von Bens separatem Opus wiederhergestellte Hub und Runner enthalten bereits denselben Anwendungscode und dieselben eingebetteten Dateien wie das geprüfte Peer-Artefakt. Der verbleibende Rollout umfasst deshalb Diensttopologie, unabhängige Wiederherstellung und Live-Abnahme; er benötigt keinen erneuten Binärwechsel. Die Prüfung liest ausführbare Dateien und Prozessmetadaten, keine HAPI-Datenbankinhalte oder Geheimnisse.

## Vergleichte Artefakte

Alle folgenden Binärdateien sind 145.225.856 Bytes groß. Pfade relativ zum Repository gelten unter `/home/benedict/work/hapi`.

| Artefakt | Pfad | SHA256 |
| --- | --- | --- |
| Installiert, laufender Hub und Runner | `/usr/lib/node_modules/@twsxtd/hapi/node_modules/@twsxtd/hapi-linux-x64/bin/hapi` | `7eeb173ace813a63bfcd085604daa0c6d1cd13c9456104e6843920dd8753bac7` |
| Lokaler Wiederherstellungsbuild | `cli/dist-exe/bun-linux-x64-baseline/hapi` | `7eeb173ace813a63bfcd085604daa0c6d1cd13c9456104e6843920dd8753bac7` |
| Geprüftes Ziel | `cli/dist-exe/bun-linux-x64-baseline/hapi-peer-1dabdaa2fbd39182` | `1dabdaa2fbd391820b42cf6c512ca07033ecb8499acce9f11beffea1e5061d7b` |

Die zugehörige `hapi-peer-1dabdaa2fbd39182.BUILDINFO.json` nennt Quellcommit `8b1884c2572da7210fa43644f17bd99e24175469`, Basistag `v0.29.0`, Bun `1.3.14`, Ziel `bun-linux-x64-baseline` und Schema 26. Für den Wiederherstellungsbuild liegt keine eigene Buildinfo vor; der folgende direkte Inhaltsvergleich schließt diese Beleglücke für den tatsächlich enthaltenen Code und die Dateien. Er ist keine nachträgliche Behauptung über den damaligen Buildablauf.

Bei der Prüfung liefen der eigentliche Hub als PID 658990 und der eigentliche Runner als PID 659047. Die SHA256 von `/proc/658990/exe` und `/proc/659047/exe` entsprach jeweils dem installierten Artefakt; die systemd-Hauptprozesse 658982 und 659039 sind dessen Node-Launcher. PIDs sind Momentaufnahmen und müssen bei einer Wiederholung aus der jeweiligen Service-Cgroup neu ermittelt werden.

## Ergebnis des Inhaltsvergleichs

Offsets sind nullbasiert; Endoffsets sind exklusiv.

| Bereich | Wiederherstellungsbuild | Geprüftes Ziel | Beleg |
| --- | --- | --- | --- |
| Nativer Runtimepräfix | `[0, 93581312)` | gleich | 93.581.312 identische Bytes, SHA256 `7e326c105171b97eb59bb61a75cd64408684249f222f51979baf2191c1d007bb` |
| Haupt-JavaScript | `[93581338, 102885094)` | `[93581343, 102885099)` | 9.303.756 identische Bytes, SHA256 `e0e7237b75e109751c03c34b0ffaccef60215e2766be9a112f1e122fe5934504` |
| Zusammenhängende eingebettete Nutzdaten ab JavaScript | `[93581338, 145173576)` | `[93581343, 145173581)` | 51.592.238 identische Bytes, SHA256 `1abdbc960aa91fb5b1a8e48ec4b3f3af85cc0055e464d1a4fb6d0dbc1094f42a` |
| Bun-Dateiverzeichnis | `[145173572, 145179760)` | `[145173577, 145179765)` | 119 Einträge à 52 Bytes; alle 119 Dateiinhalte identisch |

Der Unterschied ist die Verpackung: Der erste BunFS-Dateiname lautet `/$bunfs/root/hapi` beziehungsweise `/$bunfs/root/hapi-peer`. Das fünf Bytes längere Ziel verändert die nachfolgenden Dateioffsets und Längenmetadaten. Im Dateiverzeichnis unterscheiden sich ausschließlich dieser erste Name samt Länge sowie die entsprechend verschobenen Pfad-/Inhaltsoffsets; Dateiinhalte und übrige Eintragsfelder sind gleich. Darunter sind die gesamte Hub-/Runner-/CLI-Logik, Webdateien und eingebetteten Hilfsprogramme. Beispielsweise besitzt das enthaltene `tunwg` in beiden Artefakten SHA256 `c1a7e08d956d9ee1087b8bf3b651d5dc32aee80eee1052c11218189fd5635768`.

Beide Hauptbundles enthalten denselben Store mit `SCHEMA_VERSION = 26` (`hub/src/store/index.ts:47`). Ein erfolgreich gestarteter Hub prüft die erwartete Schemageneration (`hub/src/store/index.ts:393`); es wurde dazu keine bestehende Datenbank direkt geöffnet. `/health` meldet die Peer-Fähigkeit (`hub/src/web/server.ts:253`). Die Live-Abnahme prüft deren tatsächliche Funktion zusätzlich; ein Health-Flag allein ersetzt sie nicht.

## Reproduzierbare lesende Prüfung

Der folgende Vergleich ist bewusst auf diese zwei Artefakte zugeschnitten. Er öffnet ausschließlich Binärdateien und schreibt nichts. Abweichende Größen/Hashes oder Dateiverzeichnisse lassen ihn abbrechen.

```bash
cd /home/benedict/work/hapi
python3 - <<'PY'
from pathlib import Path
from hashlib import sha256
from struct import unpack

directory = Path('cli/dist-exe/bun-linux-x64-baseline')
live = (directory / 'hapi').read_bytes()
reviewed = (directory / 'hapi-peer-1dabdaa2fbd39182').read_bytes()
installed = Path('/usr/lib/node_modules/@twsxtd/hapi/node_modules/@twsxtd/hapi-linux-x64/bin/hapi').read_bytes()
assert installed == live
assert len(live) == len(reviewed) == 145225856
assert sha256(live).hexdigest() == '7eeb173ace813a63bfcd085604daa0c6d1cd13c9456104e6843920dd8753bac7'
assert sha256(reviewed).hexdigest() == '1dabdaa2fbd391820b42cf6c512ca07033ecb8499acce9f11beffea1e5061d7b'
assert live[:93581312] == reviewed[:93581312]
assert live[93581338:145173576] == reviewed[93581343:145173581]
base, live_table, reviewed_table, record_size = 93581320, 145173572, 145173577, 52
for index in range(119):
    left = unpack('<13I', live[live_table + index * record_size:live_table + (index + 1) * record_size])
    right = unpack('<13I', reviewed[reviewed_table + index * record_size:reviewed_table + (index + 1) * record_size])
    left_name = live[base + left[0]:base + left[0] + left[1]]
    right_name = reviewed[base + right[0]:base + right[0] + right[1]]
    assert left_name == right_name or (index == 0 and left_name == b'/$bunfs/root/hapi' and right_name == b'/$bunfs/root/hapi-peer')
    assert live[base + left[2]:base + left[2] + left[3]] == reviewed[base + right[2]:base + right[2] + right[3]]
    for field, (before, after) in enumerate(zip(left, right)):
        if before != after:
            assert after == before + 5 and (field in (0, 2) or (index == 0 and field == 1))
print('OK: Runtime, Anwendungscode und alle 119 eingebetteten Dateien stimmen überein.')
PY
```

Prozessidentität separat über `systemctl --user show hapi-hub.service hapi-runner.service -p MainPID -p ControlGroup -p ExecStart` und die `cgroup.procs` der beiden Units ermitteln. Nur HAPI-Prozesse über `/proc/<pid>/exe` hashen; weder vollständige Prozessumgebungen noch Datenbanken ausgeben.

## Warum der Runner unverändert weiterläuft

`cli/src/utils/spawnHappyCLI.ts:76` bevorzugt `HAPI_CLI_EXECUTABLE`, danach den absoluten Aufrufpfad. Der laufende Runner hat keinen solchen Override und erzeugt neue CLIs über den bestehenden npm-Binärpfad. Diese neuen CLIs enthalten bereits den geprüften Code. Eine PATH-Änderung würde die Auswahl nicht verändern; eine nachträgliche Unit-Umgebungsvariable wirkt erst auf einen neuen Prozess.

Auch `HAPI_DISABLE_VERSION_HANDOFF` ist im laufenden Runner nicht gesetzt. Würde seine Binärdatei am bestehenden Pfad ersetzt, könnte die geänderte Dateizeit einen automatischen Runnerwechsel auslösen (`cli/src/runner/run.ts:1295`). Der konfigurative Restrollout lässt deshalb sowohl diese Datei als auch den laufenden Runnerprozess unverändert. Ein späterer bewusster Runnerwechsel braucht den gesondert geprüften, unabhängig beaufsichtigten Ablauf; diese Provenienzprüfung führt keinen solchen Wechsel aus.
